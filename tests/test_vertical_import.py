"""Owner-run vertical seeding: ``python -m backend vertical-load``.

``roster-load`` refuses a roster row whose vertical id does not exist, and no
migration seeds the real vertical list (only ``seed-fake`` creates fake ones).
The owner therefore needs one small command that reads ``id,name`` from a CSV
outside the repository, reports every problem by CSV row number and issue code
only, writes nothing by default, and writes every row in one transaction with
``--commit``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.__main__ import main
from backend.config import Config
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.verticals import (
    VERTICAL_COLUMNS,
    apply_verticals,
    validate_verticals_text,
)


def _database(
    tmp_path: Path,
    verticals: tuple[tuple[int, str], ...] = (),
) -> Path:
    path = tmp_path / "verticals.db"
    connection = connect(path)
    try:
        apply_migrations(connection)
        for vertical_id, name in verticals:
            connection.execute(
                "INSERT INTO vertical (id, name) VALUES (?, ?)",
                (vertical_id, name),
            )
    finally:
        connection.close()
    return path


def _csv(
    tmp_path: Path,
    *rows: str,
    name: str = "verticals.csv",
    header: str = "id,name",
) -> Path:
    path = tmp_path / name
    path.write_text("\n".join((header, *rows)) + "\n", encoding="utf-8")
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


def _rows(database: Path) -> list[tuple[int, str]]:
    connection = connect(database)
    try:
        return [
            (int(row["id"]), str(row["name"]))
            for row in connection.execute(
                "SELECT id, name FROM vertical ORDER BY id"
            ).fetchall()
        ]
    finally:
        connection.close()


# --- dry-run and commit -----------------------------------------------------


def test_dry_run_prints_counts_only_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    csv_path = _csv(tmp_path, "1,Logistics", "2,Decor")

    code, out, err = _run(
        capsys, ["vertical-load", str(csv_path), "--db", str(database)]
    )

    assert code == 0
    assert err == ""
    assert "rows=2 issues=0" in out
    assert "mode=dry-run loaded=0" in out
    # Counts only: no name from the file is echoed.
    assert "Logistics" not in out + err
    assert "Decor" not in out + err
    assert _rows(database) == []


def test_commit_inserts_every_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    csv_path = _csv(tmp_path, "1,Logistics", "2,Decor")

    code, out, err = _run(
        capsys,
        ["vertical-load", str(csv_path), "--db", str(database), "--commit"],
    )

    assert code == 0
    assert err == ""
    assert "mode=committed loaded=2" in out
    assert "Logistics" not in out + err
    assert _rows(database) == [(1, "Logistics"), (2, "Decor")]


def test_commit_creates_a_missing_database_like_roster_load(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "fresh.db"
    csv_path = _csv(tmp_path, "1,Logistics")

    code, out, err = _run(
        capsys,
        ["vertical-load", str(csv_path), "--db", str(database), "--commit"],
    )

    assert code == 0
    assert err == ""
    assert "mode=committed loaded=1" in out
    assert _rows(database) == [(1, "Logistics")]


def test_dry_run_refuses_a_missing_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "absent.db"
    csv_path = _csv(tmp_path, "1,Logistics")

    code, _out, err = _run(
        capsys, ["vertical-load", str(csv_path), "--db", str(database)]
    )

    assert code == 2
    assert "does not exist" in err
    assert not database.exists()


def test_a_second_commit_of_the_same_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    csv_path = _csv(tmp_path, "1,Logistics")
    assert (
        _run(
            capsys,
            ["vertical-load", str(csv_path), "--db", str(database), "--commit"],
        )[0]
        == 0
    )

    code, out, err = _run(
        capsys,
        ["vertical-load", str(csv_path), "--db", str(database), "--commit"],
    )

    assert code == 2
    assert "row 2: EXISTING_ID" in out
    assert "Logistics" not in out + err
    assert _rows(database) == [(1, "Logistics")]


# --- repository boundary ----------------------------------------------------


def test_a_csv_path_inside_the_repository_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    in_repo = Path(__file__).resolve()

    code, _out, err = _run(
        capsys, ["vertical-load", str(in_repo), "--db", str(database)]
    )

    assert code == 2
    assert "outside the repository" in err
    assert _rows(database) == []


def test_a_database_path_inside_the_repository_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv_path = _csv(tmp_path, "1,Logistics")
    in_repo = Path(__file__).resolve().parent.parent / "docs" / "verticals-never.db"

    code, _out, err = _run(
        capsys,
        ["vertical-load", str(csv_path), "--db", str(in_repo), "--commit"],
    )

    assert code == 2
    assert "outside the repository" in err
    assert not in_repo.exists()


# --- validation codes -------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "body_rows", "expected"),
    [
        pytest.param("", (), (1, "BAD_HEADER"), id="empty-file"),
        pytest.param(
            "id,vertical_name",
            ("1,Logistics",),
            (1, "BAD_HEADER"),
            id="bad-header",
        ),
        pytest.param(
            "id,name",
            ("1",),
            (2, "BAD_COLUMNS"),
            id="bad-columns",
        ),
        pytest.param(
            "id,name",
            ("abc,Logistics",),
            (2, "BAD_ID"),
            id="non-integer-id",
        ),
        pytest.param(
            "id,name",
            ("0,Logistics",),
            (2, "BAD_ID"),
            id="zero-id",
        ),
        pytest.param(
            "id,name",
            ("-1,Logistics",),
            (2, "BAD_ID"),
            id="negative-id",
        ),
        pytest.param(
            "id,name",
            ("1,",),
            (2, "EMPTY_NAME"),
            id="empty-name",
        ),
        pytest.param(
            "id,name",
            ("1,   ",),
            (2, "EMPTY_NAME"),
            id="blank-name",
        ),
        pytest.param(
            "id,name",
            ("1,Logistics", "1,Decor"),
            (3, "DUPLICATE_ID"),
            id="duplicate-id",
        ),
        pytest.param(
            "id,name",
            ("1,Logistics", "01,Decor"),
            (3, "DUPLICATE_ID"),
            id="duplicate-id-after-normalisation",
        ),
        pytest.param(
            "id,name",
            ("1,Logistics", "2,Logistics"),
            (3, "DUPLICATE_NAME"),
            id="duplicate-name",
        ),
    ],
)
def test_each_validation_code_is_reported_on_its_row_and_writes_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    header: str,
    body_rows: tuple[str, ...],
    expected: tuple[int, str],
) -> None:
    database = _database(tmp_path)
    csv_path = _csv(tmp_path, *body_rows, header=header)

    code, out, err = _run(
        capsys,
        ["vertical-load", str(csv_path), "--db", str(database), "--commit"],
    )

    assert code == 2
    assert f"row {expected[0]}: {expected[1]}" in out
    assert "Logistics" not in out + err
    assert "Decor" not in out + err
    assert _rows(database) == []


def test_all_problems_are_reported_in_one_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    csv_path = _csv(
        tmp_path,
        "abc,Logistics",
        "2,",
        "3,Decor",
        "3,Food",
    )

    code, out, err = _run(
        capsys, ["vertical-load", str(csv_path), "--db", str(database)]
    )

    assert code == 2
    for expected in (
        "row 2: BAD_ID",
        "row 3: EMPTY_NAME",
        "row 5: DUPLICATE_ID",
    ):
        assert expected in out
    assert "issues=3" in out
    assert "Logistics" not in out + err
    assert "Decor" not in out + err


def test_ids_and_names_already_in_the_database_are_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path, ((1, "Logistics"),))
    csv_path = _csv(tmp_path, "1,Decor", "2,Logistics")

    code, out, err = _run(
        capsys, ["vertical-load", str(csv_path), "--db", str(database)]
    )

    assert code == 2
    assert "row 2: EXISTING_ID" in out
    assert "row 3: EXISTING_NAME" in out
    assert "Logistics" not in out + err
    assert "Decor" not in out + err
    assert _rows(database) == [(1, "Logistics")]


def test_a_name_is_stored_trimmed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    csv_path = _csv(tmp_path, " 1 , Logistics ")

    code, _out, err = _run(
        capsys,
        ["vertical-load", str(csv_path), "--db", str(database), "--commit"],
    )

    assert code == 0
    assert err == ""
    assert _rows(database) == [(1, "Logistics")]


# --- rollback and usage -----------------------------------------------------


def test_a_failure_mid_load_rolls_everything_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    connection = connect(database)
    try:
        connection.execute(
            "CREATE TRIGGER vertical_forced_failure BEFORE INSERT ON vertical"
            " WHEN NEW.id = 99 BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
        )
    finally:
        connection.close()
    csv_path = _csv(tmp_path, "1,Logistics", "99,Decor")

    code, out, err = _run(
        capsys,
        ["vertical-load", str(csv_path), "--db", str(database), "--commit"],
    )

    assert code == 2
    assert "vertical-load failed" in err
    assert "Logistics" not in out + err
    assert "Decor" not in out + err
    # All-or-nothing: the valid first row was rolled back too.
    assert _rows(database) == []


def test_a_missing_database_argument_prints_the_usage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv_path = _csv(tmp_path, "1,Logistics")

    code, out, err = _run(capsys, ["vertical-load", str(csv_path)])

    assert code == 2
    assert out == ""
    assert "usage: python -m backend vertical-load" in err


# --- direct contract checks -------------------------------------------------


def test_the_header_must_be_exactly_id_and_name() -> None:
    report = validate_verticals_text("id,vertical_name\n1,Logistics\n")

    assert not report.ok
    assert [(issue.row, issue.code) for issue in report.issues] == [
        (1, "BAD_HEADER")
    ]
    assert VERTICAL_COLUMNS == ("id", "name")


def test_a_missing_file_is_reported_without_a_path(tmp_path: Path) -> None:
    from backend.verticals import VerticalError, validate_verticals

    with pytest.raises(VerticalError) as error:
        validate_verticals(tmp_path / "absent.csv")

    assert str(error.value) == "the file could not be read"
    assert "absent" not in str(error.value)


def test_apply_verticals_refuses_an_empty_report(tmp_path: Path) -> None:
    from backend.verticals import VerticalError

    database = _database(tmp_path)
    connection = connect(database)
    try:
        with pytest.raises(VerticalError):
            apply_verticals(connection, ())
    finally:
        connection.close()
    assert _rows(database) == []
