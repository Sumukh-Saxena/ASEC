"""Schema-level tests for 0016_roster_integrity (Phase 7, PB-5).

0016 rebuilds ``person`` so that a phone number and a roll number are mandatory,
the role vocabulary is the SRS token set, and ``reports_to`` becomes a
self-referencing person id instead of free text (FR-ROS-01, FR-ROS-02, DR-01,
DR-02, DR-04).

The tests run the real migration through the runner against a database stopped
at the Phase 6 window (versions 0001-0015), so they exercise the copy rules and
the abort guards on genuine pre-window rows rather than on a synthetic schema.
A pre-window row that cannot satisfy the new constraints must abort the whole
migration and leave version 15 plus the old ``person`` shape in place: the
migration never invents a phone, a roll number, a role or a manager.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import MIGRATIONS_DIR, apply_migrations

_PRE_WINDOW = 15


def _staged_directory(tmp_path: Path, limit: int) -> Path:
    directory = tmp_path / f"migrations-{limit}"
    directory.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name[:4]) <= limit:
            (directory / path.name).write_bytes(path.read_bytes())
    return directory


@pytest.fixture()
def pre_window(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A database holding only the Phase 6 schema (0001-0015)."""
    connection = connect(tmp_path / "pre-window.db")
    apply_migrations(connection, directory=_staged_directory(tmp_path, _PRE_WINDOW))
    yield connection
    connection.close()


def _versions(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [row["version"] for row in rows]


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"]: row for row in rows}


def _old_person(
    connection: sqlite3.Connection,
    person_id: int,
    name: str,
    *,
    phone: str | None = "ph-1",
    role: str = "Volunteer",
    roll_number: str | None = "roll-1",
    reports_to: str | None = None,
    discord_user_id: str | None = None,
) -> int:
    """Insert a row in the pre-window ``person`` shape (nullable identity)."""
    connection.execute(
        "INSERT INTO person (id, name, discord_user_id, phone, role, reports_to,"
        " active, roll_number) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        (person_id, name, discord_user_id, phone, role, reports_to, roll_number),
    )
    return person_id


def _new_person(
    connection: sqlite3.Connection,
    person_id: int,
    name: str,
    *,
    phone: str,
    roll_number: str,
    role: str = "Volunteer",
    reports_to: int | None = None,
) -> int:
    connection.execute(
        "INSERT INTO person (id, name, phone, role, reports_to, active, roll_number)"
        " VALUES (?, ?, ?, ?, ?, 1, ?)",
        (person_id, name, phone, role, reports_to, roll_number),
    )
    return person_id


# --- window application -----------------------------------------------------


def test_0016_lands_in_the_window_and_reapply_is_a_noop(
    pre_window: sqlite3.Connection,
) -> None:
    """PB-5: 0016-0018 apply as one window on top of 0001-0015."""
    assert _versions(pre_window) == list(range(1, _PRE_WINDOW + 1))

    assert apply_migrations(pre_window) == [16, 17, 18]
    assert _versions(pre_window) == list(range(1, 19))
    assert apply_migrations(pre_window) == []


# --- copy rules -------------------------------------------------------------


def test_rebuild_preserves_rows_and_converts_reports_to_to_a_person_id(
    pre_window: sqlite3.Connection,
) -> None:
    """FR-ROS-02/DR-01/DR-02: no roster row is lost and reports_to becomes an id."""
    _old_person(
        pre_window,
        1,
        "Lead",
        phone="ph-1",
        role="Admin",
        roll_number="roll-1",
        discord_user_id="u-1",
    )
    _old_person(
        pre_window,
        2,
        "Deputy",
        phone="ph-2",
        role="Head",
        roll_number="roll-2",
        reports_to="1",
    )
    _old_person(
        pre_window,
        3,
        "Helper",
        phone="ph-3",
        role="Volunteer",
        roll_number="roll-3",
        reports_to=None,
    )

    apply_migrations(pre_window)

    rows = pre_window.execute("SELECT * FROM person ORDER BY id").fetchall()
    assert [row["name"] for row in rows] == ["Lead", "Deputy", "Helper"]
    assert [row["phone"] for row in rows] == ["ph-1", "ph-2", "ph-3"]
    assert [row["roll_number"] for row in rows] == ["roll-1", "roll-2", "roll-3"]
    assert [row["role"] for row in rows] == ["Admin", "Head", "Volunteer"]
    assert rows[0]["reports_to"] is None
    assert rows[1]["reports_to"] == 1
    assert isinstance(rows[1]["reports_to"], int)
    assert rows[2]["reports_to"] is None
    assert rows[0]["discord_user_id"] == "u-1"
    assert int(rows[0]["active"]) == 1

    columns = _columns(pre_window, "person")
    assert columns["reports_to"]["type"] == "INTEGER"


def test_phone_and_roll_number_become_mandatory(
    pre_window: sqlite3.Connection,
) -> None:
    """FR-ROS-01/DR-02: every person has an identity contact and a roll number."""
    apply_migrations(pre_window)

    columns = _columns(pre_window, "person")
    assert columns["phone"]["notnull"] == 1
    assert columns["roll_number"]["notnull"] == 1

    with pytest.raises(sqlite3.IntegrityError, match="person.phone"):
        pre_window.execute(
            "INSERT INTO person (id, name, role, roll_number)"
            " VALUES (1, 'Asha', 'Volunteer', 'roll-1')"
        )
    with pytest.raises(sqlite3.IntegrityError, match="person.roll_number"):
        pre_window.execute(
            "INSERT INTO person (id, name, role, phone)"
            " VALUES (2, 'Bilal', 'Volunteer', 'ph-2')"
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        pre_window.execute(
            "INSERT INTO person (id, name, role, phone, roll_number)"
            " VALUES (3, 'Chitra', 'Volunteer', '   ', 'roll-3')"
        )


def test_role_tokens_are_the_srs_vocabulary(pre_window: sqlite3.Connection) -> None:
    """FR-AUT-05: the stored role is one of the four SRS tokens."""
    apply_migrations(pre_window)

    for index, role in enumerate(("Admin", "Head", "AH", "Volunteer"), start=1):
        _new_person(
            pre_window,
            index,
            f"person-{index}",
            phone=f"ph-{index}",
            roll_number=f"roll-{index}",
            role=role,
        )

    for index, role in enumerate(("admin", "VOLUNTEER", "Coordinator", "Moderator"), start=9):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _new_person(
                pre_window,
                index,
                f"person-{index}",
                phone=f"ph-{index}",
                roll_number=f"roll-{index}",
                role=role,
            )


def test_phone_roll_number_and_discord_id_are_unique(
    pre_window: sqlite3.Connection,
) -> None:
    """FR-ROS-01/DR-02: identifiers identify exactly one person."""
    apply_migrations(pre_window)
    _new_person(
        pre_window,
        1,
        "Asha",
        phone="ph-1",
        roll_number="roll-1",
    )

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _new_person(pre_window, 2, "Bilal", phone="ph-1", roll_number="roll-2")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _new_person(pre_window, 3, "Chitra", phone="ph-3", roll_number="roll-1")

    # Several people without a Discord id stay allowed; the unique index ignores NULL.
    _new_person(pre_window, 4, "Dev", phone="ph-4", roll_number="roll-4")
    assert pre_window.execute("SELECT count(*) FROM person").fetchone()[0] == 2


def test_reports_to_must_name_an_existing_person(
    pre_window: sqlite3.Connection,
) -> None:
    """DR-01: the manager reference is a real person, enforced by the database."""
    apply_migrations(pre_window)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _new_person(
            pre_window,
            1,
            "Asha",
            phone="ph-1",
            roll_number="roll-1",
            reports_to=999,
        )


# --- abort guards on pre-window rows ---------------------------------------


def test_aborts_when_existing_row_has_no_phone(pre_window: sqlite3.Connection) -> None:
    """PB-5: a pre-window NULL phone aborts instead of inventing a value."""
    _old_person(pre_window, 1, "Asha", phone=None, roll_number="roll-1")

    with pytest.raises(sqlite3.IntegrityError, match="person_phone_required"):
        apply_migrations(pre_window)


def test_aborts_when_existing_roll_number_is_blank(
    pre_window: sqlite3.Connection,
) -> None:
    """PB-5: a blank roll number aborts instead of becoming a fake identity."""
    _old_person(pre_window, 1, "Asha", phone="ph-1", roll_number=None)

    with pytest.raises(sqlite3.IntegrityError, match="person_roll_number_required"):
        apply_migrations(pre_window)


def test_aborts_on_unknown_role(pre_window: sqlite3.Connection) -> None:
    """PB-5/FR-AUT-05: a role outside the SRS vocabulary aborts the rebuild."""
    _old_person(pre_window, 1, "Asha", role="Coordinator")

    with pytest.raises(sqlite3.IntegrityError, match="person_role_known"):
        apply_migrations(pre_window)


@pytest.mark.parametrize("reports_to", ["nobody", "1.9", "-1", "2 "])
def test_aborts_on_unresolvable_reports_to(
    pre_window: sqlite3.Connection, reports_to: str
) -> None:
    """PB-5/FR-ROS-02: free-text managers must name a person or abort."""
    _old_person(pre_window, 1, "Asha", phone="ph-1", roll_number="roll-1")
    _old_person(
        pre_window,
        2,
        "Bilal",
        phone="ph-2",
        roll_number="roll-2",
        reports_to=reports_to,
    )

    with pytest.raises(
        sqlite3.IntegrityError, match="person_reports_to_must_name_a_person"
    ):
        apply_migrations(pre_window)


def test_aborts_on_self_report(pre_window: sqlite3.Connection) -> None:
    """FR-ROS-02: a person cannot report to themselves; the rebuild aborts."""
    _old_person(pre_window, 1, "Asha", phone="ph-1", roll_number="roll-1")
    _old_person(
        pre_window,
        2,
        "Bilal",
        phone="ph-2",
        roll_number="roll-2",
        reports_to="2",
    )

    with pytest.raises(sqlite3.IntegrityError, match="person_cannot_report_to_self"):
        apply_migrations(pre_window)


def test_a_failed_rebuild_keeps_version_15_and_the_old_shape(
    pre_window: sqlite3.Connection,
) -> None:
    """Atomicity: no partial schema, no false version record, no lost rows."""
    _old_person(pre_window, 1, "Asha", phone=None, roll_number="roll-1")

    with pytest.raises(sqlite3.IntegrityError):
        apply_migrations(pre_window)

    assert _versions(pre_window) == list(range(1, _PRE_WINDOW + 1))
    columns = _columns(pre_window, "person")
    assert columns["phone"]["notnull"] == 0
    assert columns["roll_number"]["notnull"] == 0
    assert columns["reports_to"]["type"] == "TEXT"
    row = pre_window.execute("SELECT name, phone FROM person").fetchone()
    assert row is not None
    assert row["name"] == "Asha"
    assert row["phone"] is None


# --- relational integrity after the rebuild --------------------------------


def test_rebuild_keeps_child_foreign_keys_pointing_at_person(
    pre_window: sqlite3.Connection,
) -> None:
    """DR-01: dependent tables still reference ``person`` after the rebuild."""
    _old_person(pre_window, 1, "Asha", phone="ph-1", roll_number="roll-1")
    pre_window.execute("INSERT INTO vertical (id, name) VALUES (1, 'logistics')")
    pre_window.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (1, 1)"
    )
    pre_window.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (1, 'ch-1', 1)"
    )
    pre_window.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " sender_person_id, received_at)"
        " VALUES (1, 'discord', 'm-1', 1, 1, '2026-05-01T10:00:00Z')"
    )

    apply_migrations(pre_window)

    assert pre_window.execute("PRAGMA foreign_key_check").fetchall() == []
    for table in ("person_vertical", "source_message", "task", "announcement"):
        sql = pre_window.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
        ).fetchone()[0]
        assert "REFERENCES person" in sql, table

    _new_person(pre_window, 2, "Bilal", phone="ph-2", roll_number="roll-2")
    pre_window.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (2, 1)"
    )
    pre_window.execute(
        "UPDATE source_message SET sender_person_id = 2 WHERE id = 1"
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        pre_window.execute("DELETE FROM person WHERE id = 1")
