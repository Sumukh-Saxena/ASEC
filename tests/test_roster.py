"""C10/C6: validated roster loader and deterministic fake-roster seed.

The roster is loaded by script before the festival (FR-ROS-01/02, O12-4). A
wrong role, Discord id, phone or manager breaks sign-in or authority, so the
loader validates every row, reports every problem by row number and issue code,
and writes nothing unless the whole file is valid and ``--commit`` is given.

The seed command builds a temporary database with an obviously fake roster so
the Android app can sign in against the real API during development. No output
in the failing paths carries a roll number, name, phone or Discord id.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

from backend.__main__ import main
from backend.config import Config
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.notification_delivery import (
    ConsoleOtpSender,
    OtpSenderNotConfigured,
)
from backend.roster import (
    FAKE_DISCORD_PREFIX,
    RosterError,
    apply_roster,
    validate_roster,
)
from backend.tasks import visible_tasks

HEADER = (
    "roll_number,name,discord_user_id,phone,role,"
    "reports_to_roll_number,verticals,active"
)

#: The full set of synthetic values used by the tests. None of them may appear
#: in a validation or load message.
PERSONAL = (
    "FAKE-ADMIN-01",
    "FAKE-HEAD-01",
    "FAKE-AH-01",
    "FAKE-VOL-01",
    "FAKE-VOL-02",
    "FAKE-INACTIVE-01",
    "Fake Admin",
    "Fake Head 01",
    "Fake AH 01",
    "Fake Volunteer 01",
    "Fake Volunteer 02",
    "Fake Inactive 01",
    "900000000000000001",
    "900000000000000002",
    "900000000000000003",
    "900000000000000004",
    "900000000000000005",
    "900000000000000006",
    "+15550100001",
    "+15550100002",
    "+15550100003",
    "+15550100004",
    "+15550100005",
    "+15550100006",
)

VALID_ROWS = (
    "FAKE-ADMIN-01,Fake Admin,900000000000000001,+15550100001,Admin,,1;2,1",
    "FAKE-HEAD-01,Fake Head 01,900000000000000002,+15550100002,Head,"
    "FAKE-ADMIN-01,1,1",
    "FAKE-AH-01,Fake AH 01,900000000000000003,+15550100003,AH,"
    "FAKE-HEAD-01,1,1",
    "FAKE-VOL-01,Fake Volunteer 01,900000000000000004,+15550100004,Volunteer,"
    "FAKE-AH-01,1,1",
    "FAKE-VOL-02,Fake Volunteer 02,900000000000000005,+15550100005,Volunteer,"
    "FAKE-AH-01,1,0",
)


class _Recorder:
    """The injected server runner: records the app and returns immediately."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, str, int]] = []

    def __call__(self, app: object, host: str, port: int) -> None:
        self.calls.append((app, host, port))


def _csv_body(*rows: str) -> str:
    return "\n".join((HEADER, *rows)) + "\n"


def _csv(tmp_path: Path, *rows: str, name: str = "roster.csv") -> Path:
    path = tmp_path / name
    path.write_text(_csv_body(*rows), encoding="utf-8")
    return path


def _database(tmp_path: Path, verticals: tuple[int, ...] = (1, 2)) -> Path:
    path = tmp_path / "roster.db"
    connection = connect(path)
    try:
        apply_migrations(connection)
        for vertical_id in verticals:
            connection.execute(
                "INSERT INTO vertical (id, name) VALUES (?, ?)",
                (vertical_id, f"Fake Vertical {vertical_id}"),
            )
    finally:
        connection.close()
    return path


def _run(
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    *,
    config: Config | None = None,
) -> tuple[int, str, str]:
    code = main(args, config=Config({}) if config is None else config)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _assert_no_personal(text: str) -> None:
    for value in PERSONAL:
        assert value not in text, f"personal value leaked: {value!r}"


def _count_people(database: Path) -> int:
    connection = connect(database)
    try:
        row = connection.execute("SELECT COUNT(*) AS n FROM person").fetchone()
        return int(row["n"])
    finally:
        connection.close()


def _people(database: Path) -> dict[str, dict[str, object]]:
    connection = connect(database)
    try:
        rows = connection.execute(
            "SELECT p.roll_number, p.name, p.role, p.active, p.phone,"
            " p.discord_user_id, m.roll_number AS manager_roll"
            " FROM person AS p LEFT JOIN person AS m ON m.id = p.reports_to"
            " ORDER BY p.roll_number"
        ).fetchall()
        verticals: dict[str, list[int]] = {}
        for row in connection.execute(
            "SELECT p.roll_number, pv.vertical_id FROM person AS p"
            " JOIN person_vertical AS pv ON pv.person_id = p.id"
            " ORDER BY p.roll_number, pv.vertical_id"
        ).fetchall():
            verticals.setdefault(str(row["roll_number"]), []).append(
                int(row["vertical_id"])
            )
        return {
            str(row["roll_number"]): {
                "name": row["name"],
                "role": row["role"],
                "active": int(row["active"]),
                "phone": row["phone"],
                "discord_user_id": row["discord_user_id"],
                "manager_roll": row["manager_roll"],
                "vertical_ids": verticals.get(str(row["roll_number"]), []),
            }
            for row in rows
        }
    finally:
        connection.close()


# --- valid load and preview -------------------------------------------------


def test_a_valid_fake_csv_loads_with_the_expected_rows(tmp_path: Path) -> None:
    database = _database(tmp_path)
    csv_path = _csv(tmp_path, *VALID_ROWS)

    connection = connect(database)
    try:
        report = validate_roster(csv_path, known_vertical_ids=frozenset({1, 2}))
        assert report.ok
        loaded = apply_roster(connection, report.rows)
    finally:
        connection.close()

    assert loaded == 5
    people = _people(database)
    assert set(people) == {
        "FAKE-ADMIN-01",
        "FAKE-HEAD-01",
        "FAKE-AH-01",
        "FAKE-VOL-01",
        "FAKE-VOL-02",
    }
    assert people["FAKE-ADMIN-01"]["role"] == "Admin"
    assert people["FAKE-ADMIN-01"]["manager_roll"] is None
    assert people["FAKE-ADMIN-01"]["vertical_ids"] == [1, 2]
    assert people["FAKE-HEAD-01"]["manager_roll"] == "FAKE-ADMIN-01"
    assert people["FAKE-AH-01"]["manager_roll"] == "FAKE-HEAD-01"
    assert people["FAKE-VOL-01"]["manager_roll"] == "FAKE-AH-01"
    assert people["FAKE-VOL-02"]["active"] == 0
    assert people["FAKE-VOL-02"]["vertical_ids"] == [1]


def test_roster_check_prints_counts_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    csv_path = _csv(tmp_path, *VALID_ROWS)

    code, out, err = _run(
        capsys, ["roster-check", str(csv_path), "--db", str(database)]
    )

    assert code == 0
    assert err == ""
    assert "Admin=1" in out
    assert "Head=1" in out
    assert "AH=1" in out
    assert "Volunteer=2" in out
    assert "without_manager=1" in out
    assert "max_depth=3" in out
    assert "issues=0" in out
    _assert_no_personal(out + err)
    assert _count_people(database) == 0


def test_roster_check_previews_manager_role_to_report_role_pairs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pair counts are shown for the owner to review; they are not a rule."""
    database = _database(tmp_path)
    csv_path = _csv(tmp_path, *VALID_ROWS)

    code, out, err = _run(
        capsys, ["roster-check", str(csv_path), "--db", str(database)]
    )

    assert code == 0
    assert err == ""
    assert "role_pairs" in out
    assert "Admin->Head=1" in out
    assert "Head->AH=1" in out
    assert "AH->Volunteer=2" in out
    _assert_no_personal(out + err)


def test_an_unusual_role_pair_is_shown_but_never_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No role chain is enforced: the SRS does not define one (ROSTER.md §7)."""
    database = _database(tmp_path)
    csv_path = _csv(
        tmp_path,
        "FAKE-VOL-01,Fake Volunteer 01,900000000000000004,"
        "+15550100004,Volunteer,,1,1",
        "FAKE-ADMIN-01,Fake Admin,900000000000000001,"
        "+15550100001,Admin,FAKE-VOL-01,1,1",
    )

    code, out, err = _run(
        capsys, ["roster-check", str(csv_path), "--db", str(database)]
    )

    assert code == 0
    assert "issues=0" in out
    assert "Volunteer->Admin=1" in out
    assert "ROLE_CHAIN" not in out + err
    _assert_no_personal(out + err)


# --- every validation code --------------------------------------------------


@pytest.mark.parametrize(
    ("body_rows", "expected_code", "expected_row"),
    [
        pytest.param(
            ("FAKE-ADMIN-01,,900000000000000001,+15550100001,Admin,,1,1",),
            "EMPTY_FIELD",
            2,
            id="empty-field",
        ),
        pytest.param(
            ("FAKE-ADMIN-01,Fake Admin,900000000000000001,"
             "+15550100001,Teacher,,1,1",),
            "BAD_ROLE",
            2,
            id="bad-role",
        ),
        pytest.param(
            (
                "FAKE-ADMIN-01,Fake Admin,900000000000000001,"
                "+15550100001,Admin,,1,1",
                "FAKE-ADMIN-01,Fake Admin Two,900000000000000002,"
                "+15550100002,Admin,,1,1",
            ),
            "DUPLICATE_ROLL",
            3,
            id="duplicate-roll",
        ),
        pytest.param(
            (
                "FAKE-ADMIN-01,Fake Admin,900000000000000001,"
                "+15550100001,Admin,,1,1",
                "FAKE-HEAD-01,Fake Head 01,900000000000000002,"
                "+15550100001,Head,,1,1",
            ),
            "DUPLICATE_PHONE",
            3,
            id="duplicate-phone",
        ),
        pytest.param(
            (
                "FAKE-ADMIN-01,Fake Admin,900000000000000001,"
                "+15550100001,Admin,,1,1",
                "FAKE-HEAD-01,Fake Head 01,900000000000000001,"
                "+15550100002,Head,,1,1",
            ),
            "DUPLICATE_DISCORD",
            3,
            id="duplicate-discord",
        ),
        pytest.param(
            ("FAKE-ADMIN-01,Fake Admin,,+15550100001,Admin,,1,1",),
            "MISSING_DISCORD",
            2,
            id="missing-discord",
        ),
        pytest.param(
            ("FAKE-ADMIN-01,Fake Admin,abc123,+15550100001,Admin,,1,1",),
            "BAD_DISCORD_ID",
            2,
            id="bad-discord-id",
        ),
        pytest.param(
            ("FAKE-ADMIN-01,Fake Admin,900000000000000001,12,Admin,,1,1",),
            "BAD_PHONE",
            2,
            id="bad-phone",
        ),
        pytest.param(
            ("FAKE-ADMIN-01,Fake Admin,900000000000000001,"
             "+15550100001,Admin,FAKE-NOBODY,1,1",),
            "UNKNOWN_MANAGER",
            2,
            id="unknown-manager",
        ),
        pytest.param(
            ("FAKE-ADMIN-01,Fake Admin,900000000000000001,"
             "+15550100001,Admin,FAKE-ADMIN-01,1,1",),
            "SELF_MANAGER",
            2,
            id="self-manager",
        ),
        pytest.param(
            (
                "FAKE-ADMIN-01,Fake Admin,900000000000000001,"
                "+15550100001,Admin,FAKE-HEAD-01,1,1",
                "FAKE-HEAD-01,Fake Head 01,900000000000000002,"
                "+15550100002,Head,FAKE-ADMIN-01,1,1",
            ),
            "REPORT_CYCLE",
            2,
            id="report-cycle",
        ),
        pytest.param(
            ("FAKE-ADMIN-01,Fake Admin,900000000000000001,"
             "+15550100001,Admin,,99,1",),
            "BAD_VERTICAL",
            2,
            id="bad-vertical",
        ),
        pytest.param(
            ("FAKE-ADMIN-01,Fake Admin,900000000000000001,"
             "+15550100001,Admin,,1,yes",),
            "BAD_ACTIVE",
            2,
            id="bad-active",
        ),
    ],
)
def test_each_validation_code_is_reported_on_its_row_and_writes_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    body_rows: tuple[str, ...],
    expected_code: str,
    expected_row: int,
) -> None:
    database = _database(tmp_path)
    csv_path = _csv(tmp_path, *body_rows)

    code, out, err = _run(
        capsys, ["roster-check", str(csv_path), "--db", str(database)]
    )

    assert code == 2
    assert f"row {expected_row}: {expected_code}" in out
    _assert_no_personal(out + err)
    assert _count_people(database) == 0


def test_all_problems_are_reported_in_one_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    csv_path = _csv(
        tmp_path,
        "FAKE-ADMIN-01,Fake Admin,900000000000000001,"
        "+15550100001,Teacher,,1,1",
        "FAKE-HEAD-01,Fake Head 01,,+15550100002,Head,,1,1",
        "FAKE-AH-01,Fake AH 01,900000000000000003,12,AH,,1,1",
        "FAKE-VOL-01,Fake Volunteer 01,900000000000000004,"
        "+15550100004,Volunteer,,1,2",
    )

    code, out, err = _run(
        capsys, ["roster-check", str(csv_path), "--db", str(database)]
    )

    assert code == 2
    for expected in (
        "row 2: BAD_ROLE",
        "row 3: MISSING_DISCORD",
        "row 4: BAD_PHONE",
        "row 5: BAD_ACTIVE",
    ):
        assert expected in out
    assert "issues=" in out
    _assert_no_personal(out + err)


# --- role normalisation -----------------------------------------------------


def test_role_case_is_normalised(tmp_path: Path) -> None:
    database = _database(tmp_path)
    csv_path = _csv(
        tmp_path,
        "FAKE-ADMIN-01,Fake Admin,900000000000000001,+15550100001,admin,,1,1",
        "FAKE-HEAD-01,Fake Head 01,900000000000000002,"
        "+15550100002,HEAD,FAKE-ADMIN-01,1,1",
        "FAKE-AH-01,Fake AH 01,900000000000000003,+15550100003, ah ,"
        "FAKE-HEAD-01,1,1",
        "FAKE-VOL-01,Fake Volunteer 01,900000000000000004,"
        "+15550100004, volunteer ,FAKE-AH-01,1,1",
    )

    connection = connect(database)
    try:
        report = validate_roster(csv_path, known_vertical_ids=frozenset({1, 2}))
        assert report.ok
        apply_roster(connection, report.rows)
    finally:
        connection.close()

    roles = {roll: person["role"] for roll, person in _people(database).items()}
    assert roles == {
        "FAKE-ADMIN-01": "Admin",
        "FAKE-HEAD-01": "Head",
        "FAKE-AH-01": "AH",
        "FAKE-VOL-01": "Volunteer",
    }


def test_an_unknown_role_is_rejected(tmp_path: Path) -> None:
    csv_path = _csv(
        tmp_path,
        "FAKE-ADMIN-01,Fake Admin,900000000000000001,"
        "+15550100001,Teacher,,1,1",
    )

    report = validate_roster(csv_path, known_vertical_ids=frozenset({1}))

    assert not report.ok
    assert [(issue.row, issue.code) for issue in report.issues] == [
        (2, "BAD_ROLE")
    ]


# --- dry-run, commit, rollback and load-once --------------------------------


def test_dry_run_writes_nothing_and_commit_writes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    csv_path = _csv(tmp_path, *VALID_ROWS)

    code, out, err = _run(
        capsys, ["roster-load", str(csv_path), "--db", str(database)]
    )

    assert code == 0
    assert "mode=dry-run" in out
    assert "loaded=0" in out
    _assert_no_personal(out + err)
    assert _count_people(database) == 0

    code, out, err = _run(
        capsys,
        ["roster-load", str(csv_path), "--db", str(database), "--commit"],
    )

    assert code == 0
    assert "mode=committed" in out
    assert "loaded=5" in out
    _assert_no_personal(out + err)
    assert _count_people(database) == 5


def test_a_failure_mid_load_rolls_everything_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    connection = connect(database)
    try:
        connection.execute(
            "CREATE TRIGGER roster_forced_failure BEFORE INSERT ON person"
            " WHEN NEW.roll_number = 'FAKE-FAIL-01'"
            " BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
        )
    finally:
        connection.close()
    csv_path = _csv(
        tmp_path,
        "FAKE-ADMIN-01,Fake Admin,900000000000000001,"
        "+15550100001,Admin,,1,1",
        "FAKE-FAIL-01,Fake Failure,900000000000000002,"
        "+15550100002,Volunteer,FAKE-ADMIN-01,1,1",
    )

    code, out, err = _run(
        capsys,
        ["roster-load", str(csv_path), "--db", str(database), "--commit"],
    )

    assert code == 2
    assert "roster-load failed" in err
    _assert_no_personal(out + err)
    assert _count_people(database) == 0


def test_loading_into_a_non_empty_person_table_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    connection = connect(database)
    try:
        connection.execute(
            "INSERT INTO person (name, discord_user_id, phone, role, active,"
            " roll_number) VALUES ('Existing Person', NULL, 'ph-existing',"
            " 'Admin', 1, 'EXISTING-01')"
        )
    finally:
        connection.close()
    csv_path = _csv(tmp_path, *VALID_ROWS)

    code, out, err = _run(
        capsys,
        ["roster-load", str(csv_path), "--db", str(database), "--commit"],
    )

    assert code == 2
    assert "not empty" in err
    _assert_no_personal(out + err)
    assert _count_people(database) == 1


def test_loading_the_same_file_twice_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    csv_path = _csv(tmp_path, *VALID_ROWS)
    assert (
        _run(
            capsys,
            ["roster-load", str(csv_path), "--db", str(database), "--commit"],
        )[0]
        == 0
    )

    code, _out, err = _run(
        capsys,
        ["roster-load", str(csv_path), "--db", str(database), "--commit"],
    )

    assert code == 2
    assert "not empty" in err
    assert _count_people(database) == 5


def test_a_csv_path_inside_the_repository_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    in_repo = Path(__file__).resolve()

    code, _out, err = _run(
        capsys, ["roster-check", str(in_repo), "--db", str(database)]
    )

    assert code == 2
    assert "outside the repository" in err


def test_a_database_path_inside_the_repository_is_refused_for_a_load(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv_path = _csv(tmp_path, *VALID_ROWS)
    in_repo = Path(__file__).resolve().parent.parent / "docs" / "roster-never.db"

    code, _out, err = _run(
        capsys, ["roster-load", str(csv_path), "--db", str(in_repo), "--commit"]
    )

    assert code == 2
    assert "outside the repository" in err
    assert not in_repo.exists()


# --- failing output never carries personal data -----------------------------


def test_failing_outputs_never_contain_personal_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    cases = (
        (
            "FAKE-ADMIN-01,Fake Admin,900000000000000001,"
            "+15550100001,Teacher,,1,1",
        ),
        (
            "FAKE-HEAD-01,Fake Head 01,,+15550100002,Head,,1,1",
        ),
        (
            "FAKE-AH-01,Fake AH 01,900000000000000003,12,AH,,1,1",
        ),
        (
            "FAKE-VOL-01,Fake Volunteer 01,900000000000000004,"
            "+15550100004,Volunteer,FAKE-NOBODY,1,1",
        ),
        (
            "FAKE-INACTIVE-01,Fake Inactive 01,900000000000000006,"
            "+15550100006,Volunteer,,99,2",
        ),
    )
    for index, rows in enumerate(cases):
        csv_path = _csv(tmp_path, *rows, name=f"bad-{index}.csv")
        code, out, err = _run(
            capsys, ["roster-check", str(csv_path), "--db", str(database)]
        )
        assert code == 2
        _assert_no_personal(out + err)

    # A direct validation result must not carry a roster value either.
    csv_path = _csv(
        tmp_path,
        "FAKE-ADMIN-01,Fake Admin,900000000000000001,"
        "+15550100001,Admin,FAKE-ADMIN-01,1,1",
    )
    report = validate_roster(csv_path, known_vertical_ids=frozenset({1}))
    assert not report.ok
    for issue in report.issues:
        _assert_no_personal(f"row {issue.row}: {issue.code}")


# --- seed-fake --------------------------------------------------------------


def _seed(tmp_path: Path, name: str = "seed.db", *extra: str) -> tuple[int, str, str]:
    buffer_out = io.StringIO()
    buffer_err = io.StringIO()
    args = ["seed-fake", "--db", str(tmp_path / name), *extra]
    with contextlib.redirect_stdout(buffer_out), contextlib.redirect_stderr(
        buffer_err
    ):
        code = main(args, config=Config({}))
    return code, buffer_out.getvalue(), buffer_err.getvalue()


def _dump(database: Path) -> tuple[list[tuple[object, ...]], ...]:
    connection = connect(database)
    try:
        people = [
            tuple(row)
            for row in connection.execute(
                "SELECT roll_number, name, discord_user_id, phone, role, active"
                " FROM person ORDER BY roll_number"
            ).fetchall()
        ]
        managers = [
            tuple(row)
            for row in connection.execute(
                "SELECT p.roll_number, m.roll_number FROM person AS p"
                " LEFT JOIN person AS m ON m.id = p.reports_to"
                " ORDER BY p.roll_number"
            ).fetchall()
        ]
        verticals = [
            tuple(row)
            for row in connection.execute(
                "SELECT p.roll_number, pv.vertical_id FROM person AS p"
                " JOIN person_vertical AS pv ON pv.person_id = p.id"
                " ORDER BY p.roll_number, pv.vertical_id"
            ).fetchall()
        ]
        return (people, managers, verticals)
    finally:
        connection.close()


def test_seed_fake_is_deterministic(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()

    assert _seed(first_dir, "one.db")[0] == 0
    assert _seed(second_dir, "two.db")[0] == 0

    assert _dump(first_dir / "one.db") == _dump(second_dir / "two.db")


def test_seed_fake_refuses_a_path_inside_the_repository() -> None:
    repo = Path(__file__).resolve().parent.parent
    target = repo / "docs" / "seed-should-never-exist.db"

    code, _out, err = _seed(repo / "docs", "seed-should-never-exist.db")

    assert code == 2
    assert "outside the repository" in err
    assert not target.exists()


def test_seed_fake_refuses_to_overwrite_an_existing_database(
    tmp_path: Path,
) -> None:
    target = tmp_path / "existing.db"
    target.write_text("not a database", encoding="utf-8")

    code, _out, err = _seed(tmp_path, "existing.db")

    assert code == 2
    assert "already exists" in err
    assert target.read_text(encoding="utf-8") == "not a database"


def test_seed_fake_prints_roll_numbers_by_role_and_has_the_expected_shape(
    tmp_path: Path,
) -> None:
    code, out, err = _seed(tmp_path, "shape.db", "--people", "12")

    assert code == 0
    assert err == ""
    assert "Admin: FAKE-" in out
    assert "Head: FAKE-" in out
    assert "AH: FAKE-" in out
    assert "Volunteer: FAKE-" in out
    assert "Inactive: FAKE-" in out

    connection = connect(tmp_path / "shape.db")
    try:
        roles = {
            str(row["role"]): int(row["n"])
            for row in connection.execute(
                "SELECT role, COUNT(*) AS n FROM person GROUP BY role"
            ).fetchall()
        }
        inactive = connection.execute(
            "SELECT COUNT(*) AS n FROM person WHERE active = 0"
        ).fetchone()
        fakes = connection.execute(
            "SELECT COUNT(*) AS n FROM person WHERE discord_user_id NOT LIKE ?",
            (f"{FAKE_DISCORD_PREFIX}%",),
        ).fetchone()
    finally:
        connection.close()
    assert roles == {"Admin": 1, "Head": 2, "AH": 2, "Volunteer": 7}
    assert int(inactive["n"]) == 1
    assert int(fakes["n"]) == 0


def test_seed_fake_refuses_an_impossible_people_count(tmp_path: Path) -> None:
    code, _out, err = _seed(tmp_path, "small.db", "--people", "2")

    assert code == 2
    assert "people" in err.lower()
    assert not (tmp_path / "small.db").exists()


# --- console OTP sender -----------------------------------------------------


def test_console_otp_sender_refuses_production() -> None:
    with pytest.raises(OtpSenderNotConfigured):
        ConsoleOtpSender(production=True, people=[])


def test_console_otp_sender_refuses_a_non_fake_discord_id() -> None:
    with pytest.raises(OtpSenderNotConfigured):
        ConsoleOtpSender(
            production=False,
            people=[
                ("FAKE-ADMIN-01", "fake-0001"),
                ("FAKE-REAL-01", "900000000000000001"),
            ],
        )


def test_console_otp_sender_prints_the_roll_number_and_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sender = ConsoleOtpSender(
        production=False, people=[("FAKE-ADMIN-01", "fake-0001")]
    )

    sender.send(discord_id="fake-0001", code="123456")

    captured = capsys.readouterr()
    assert "FAKE-ADMIN-01 -> 123456" in captured.out
    assert sender.sent == (("FAKE-ADMIN-01", "123456"),)


def test_console_otp_sender_refuses_an_unknown_discord_id() -> None:
    sender = ConsoleOtpSender(
        production=False, people=[("FAKE-ADMIN-01", "fake-0001")]
    )

    with pytest.raises(OtpSenderNotConfigured):
        sender.send(discord_id="fake-9999", code="123456")


# --- serve-api --dev-otp ----------------------------------------------------


def _serve(
    database: Path,
    *,
    production: bool = False,
    dev_otp: bool = True,
) -> tuple[int, _Recorder, str, str]:
    recorder = _Recorder()
    values = {"ASEC_DB_PATH": str(database)}
    if production:
        values["ASEC_PRODUCTION"] = "true"
    args = ["serve-api"]
    if dev_otp:
        args.append("--dev-otp")
    buffer_out = io.StringIO()
    buffer_err = io.StringIO()
    with contextlib.redirect_stdout(buffer_out), contextlib.redirect_stderr(
        buffer_err
    ):
        code = main(args, config=Config(values), serve_api=recorder)
    return code, recorder, buffer_out.getvalue(), buffer_err.getvalue()


def test_dev_otp_signs_in_against_a_seeded_database_and_me_returns_the_person(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "signin.db"
    assert _seed(tmp_path, "signin.db")[0] == 0
    connection = connect(database)
    try:
        row = connection.execute(
            "SELECT p.roll_number, p.role, pv.vertical_id FROM person AS p"
            " JOIN person_vertical AS pv ON pv.person_id = p.id"
            " WHERE p.role = 'Volunteer' AND p.active = 1"
            " ORDER BY p.id, pv.vertical_id LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    roll_number = str(row["roll_number"])
    expected_role = str(row["role"])
    expected_vertical = int(row["vertical_id"])

    code, recorder, _out, err = _serve(database)

    assert code == 0
    assert err == ""
    assert len(recorder.calls) == 1
    app = recorder.calls[0][0]
    from tests.api_fixtures import call

    accepted = call(
        app, "POST", "/auth/otp", json_body={"roll_number": roll_number}
    )
    assert accepted.status == 202
    # The sender printed to the real stdout, which capsys now sees.
    console = capsys.readouterr().out
    assert roll_number in console
    match = re.search(re.escape(roll_number) + r" -> (\d{6})", console)
    assert match is not None
    otp = match.group(1)

    verified = call(
        app,
        "POST",
        "/auth/verify",
        json_body={"roll_number": roll_number, "code": otp},
    )
    assert verified.status == 200
    token = str(verified.json()["token"])

    me = call(app, "GET", "/me", headers={"authorization": f"Bearer {token}"})
    assert me.status == 200
    person = me.json()["person"]
    assert person["role"] == expected_role
    assert expected_vertical in person["vertical_ids"]


def test_dev_otp_refuses_a_database_with_a_real_discord_id(
    tmp_path: Path,
) -> None:
    from tests.api_fixtures import seed_database

    database = tmp_path / "real.db"
    seed_database(database)

    code, recorder, _out, err = _serve(database)

    assert code == 2
    assert recorder.calls == []
    assert "fake" in err.lower()


def test_dev_otp_refuses_in_production_mode(tmp_path: Path) -> None:
    assert _seed(tmp_path, "prod.db")[0] == 0

    code, recorder, _out, err = _serve(
        tmp_path / "prod.db", production=True
    )

    assert code == 2
    assert recorder.calls == []
    assert "production" in err.lower()


def test_serve_api_without_dev_otp_keeps_the_existing_behaviour(
    tmp_path: Path,
) -> None:
    from tests.api_fixtures import seed_database

    database = tmp_path / "normal.db"
    seed_database(database)

    code, recorder, _out, err = _serve(database, dev_otp=False)

    assert code == 0
    assert len(recorder.calls) == 1
    assert "no OTP sender configured" in err


# --- hierarchy visibility smoke test ---------------------------------------


def test_seeded_roster_respects_the_existing_task_visibility_rules(
    tmp_path: Path,
) -> None:
    from tests.api_fixtures import make_task, seed_world

    database = tmp_path / "visibility.db"
    assert _seed(tmp_path, "visibility.db")[0] == 0
    seed_world(database)
    connection = connect(database)
    try:
        def person_id(role: str, vertical_id: int) -> int:
            row = connection.execute(
                "SELECT p.id FROM person AS p"
                " JOIN person_vertical AS pv ON pv.person_id = p.id"
                " WHERE p.role = ? AND pv.vertical_id = ? AND p.active = 1"
                " ORDER BY p.id LIMIT 1",
                (role, vertical_id),
            ).fetchone()
            assert row is not None
            return int(row["id"])

        ah_one = person_id("AH", 1)
        ah_two = person_id("AH", 2)
        volunteer_two = person_id("Volunteer", 2)
    finally:
        connection.close()

    task_id = make_task(
        database,
        creator_id=ah_one,
        source_vertical_id=1,
        responsible_vertical_id=1,
    )

    connection = connect(database)
    try:
        assert task_id in {
            task.id for task in visible_tasks(connection, viewer_person_id=ah_one)
        }
        assert task_id not in {
            task.id for task in visible_tasks(connection, viewer_person_id=ah_two)
        }
        assert task_id not in {
            task.id
            for task in visible_tasks(connection, viewer_person_id=volunteer_two)
        }
    finally:
        connection.close()


# --- a couple of direct contract checks ------------------------------------


def test_a_missing_database_is_refused_by_a_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv_path = _csv(tmp_path, *VALID_ROWS)

    code, _out, err = _run(
        capsys,
        ["roster-load", str(csv_path), "--db", str(tmp_path / "absent.db")],
    )

    assert code == 2
    assert "does not exist" in err
    assert not (tmp_path / "absent.db").exists()


def test_the_header_must_be_exact(tmp_path: Path) -> None:
    path = tmp_path / "header.csv"
    path.write_text(
        "roll,name,discord_user_id,phone,role,"
        "reports_to_roll_number,verticals,active\n",
        encoding="utf-8",
    )

    report = validate_roster(path)

    assert not report.ok
    assert [(issue.row, issue.code) for issue in report.issues] == [
        (1, "BAD_HEADER")
    ]


def test_a_row_with_the_wrong_number_of_columns_is_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "columns.csv"
    path.write_text(
        _csv_body(
            "FAKE-ADMIN-01,Fake Admin,900000000000000001,"
            "+15550100001,Admin,,1"
        ),
        encoding="utf-8",
    )

    report = validate_roster(path)

    assert not report.ok
    assert [(issue.row, issue.code) for issue in report.issues] == [
        (2, "BAD_COLUMNS")
    ]


def test_a_missing_file_is_reported_without_a_path(
    tmp_path: Path,
) -> None:
    with pytest.raises(RosterError) as error:
        validate_roster(tmp_path / "absent.csv")

    assert str(error.value) == "the file could not be read"
    assert "absent" not in str(error.value)


def test_roster_check_without_a_database_skips_vertical_existence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The preview command is useful before a database exists; existence needs --db."""
    csv_path = _csv(
        tmp_path,
        "FAKE-ADMIN-01,Fake Admin,900000000000000001,"
        "+15550100001,Admin,,99,1",
    )

    code, out, err = _run(capsys, ["roster-check", str(csv_path)])

    assert code == 0
    assert "issues=0" in out
    assert err == ""
