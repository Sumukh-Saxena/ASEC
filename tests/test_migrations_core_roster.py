"""Tests for the 0001_core_roster migration (Slice 2.2 rosters and org entities).

Covers SRS FR-ROS-01, FR-AUT-05, FR-AUT-07, FR-ING-09, FR-ING-10, FR-PRP-10,
DR-01, DR-02, DR-04 and the §9 relational-store constraint.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.db.connection import connect
from backend.db.migrations import MIGRATIONS_DIR, apply_migrations


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def _versions(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [row["version"] for row in rows]


def _applied_at(connection: sqlite3.Connection, version: int) -> str:
    row = connection.execute(
        "SELECT applied_at FROM schema_migrations WHERE version = ?", (version,)
    ).fetchone()
    assert row is not None
    return str(row["applied_at"])


def _add_vertical(connection: sqlite3.Connection, name: str, vertical_id: int | None = None) -> int:
    if vertical_id is None:
        cursor = connection.execute("INSERT INTO vertical (name) VALUES (?)", (name,))
    else:
        cursor = connection.execute(
            "INSERT INTO vertical (id, name) VALUES (?, ?)", (vertical_id, name)
        )
    return int(cursor.lastrowid or vertical_id or 0)


def _add_group(
    connection: sqlite3.Connection,
    channel: str,
    vertical_id: int | None,
    group_id: int | None = None,
) -> int:
    if group_id is None:
        cursor = connection.execute(
            "INSERT INTO source_group (discord_channel_id, vertical_id) VALUES (?, ?)",
            (channel, vertical_id),
        )
    else:
        cursor = connection.execute(
            "INSERT INTO source_group (id, discord_channel_id, vertical_id) VALUES (?, ?, ?)",
            (group_id, channel, vertical_id),
        )
    return int(cursor.lastrowid or group_id or 0)


def _add_person(
    connection: sqlite3.Connection,
    name: str = "Asha",
    role: str = "AH",
    person_id: int | None = None,
    **fields: Any,
) -> int:
    columns = ["name", "role"]
    values: list[Any] = [name, role]
    if person_id is not None:
        columns.insert(0, "id")
        values.insert(0, person_id)
    for key, value in fields.items():
        columns.append(key)
        values.append(value)
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO person ({', '.join(columns)}) VALUES ({placeholders})", values
    )
    return int(cursor.lastrowid or person_id or 0)


def test_default_migration_applies_and_creates_tables(conn: sqlite3.Connection) -> None:
    assert _versions(conn) == [1]
    assert {"vertical", "source_group", "person", "person_vertical"} <= _table_names(conn)


def test_reapplying_the_migration_is_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "test.db")
    try:
        first = apply_migrations(connection)
        assert first == [1]
        applied_at = _applied_at(connection, 1)

        second = apply_migrations(connection)
        assert second == []
        assert _versions(connection) == [1]
        assert _applied_at(connection, 1) == applied_at
    finally:
        connection.close()


def test_group_membership_and_vertical_resolve(conn: sqlite3.Connection) -> None:
    vertical_id = _add_vertical(conn, "PR", 1)
    group_id = _add_group(conn, "chan-pr", vertical_id, 1)
    person_id = _add_person(conn, name="Asha", role="AH", person_id=1)
    conn.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)",
        (person_id, vertical_id),
    )

    row = conn.execute(
        "SELECT person.name AS person_name, "
        "vertical.name AS vertical_name, "
        "source_group.discord_channel_id AS channel "
        "FROM person_vertical "
        "JOIN person ON person.id = person_vertical.person_id "
        "JOIN vertical ON vertical.id = person_vertical.vertical_id "
        "JOIN source_group ON source_group.vertical_id = vertical.id "
        "WHERE source_group.id = ?",
        (group_id,),
    ).fetchone()
    assert row is not None
    assert row["person_name"] == "Asha"
    assert row["vertical_name"] == "PR"
    assert row["channel"] == "chan-pr"


def test_person_can_belong_to_several_verticals(conn: sqlite3.Connection) -> None:
    pr = _add_vertical(conn, "PR", 1)
    transport = _add_vertical(conn, "Transport", 2)
    first = _add_person(conn, name="Asha", person_id=1)
    second = _add_person(conn, name="Bilal", person_id=2)

    conn.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)", (first, pr)
    )
    conn.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)",
        (first, transport),
    )
    conn.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)", (second, pr)
    )

    memberships = conn.execute(
        "SELECT vertical_id FROM person_vertical WHERE person_id = ? ORDER BY vertical_id",
        (first,),
    ).fetchall()
    assert [row["vertical_id"] for row in memberships] == [1, 2]
    count = conn.execute(
        "SELECT COUNT(*) FROM person_vertical WHERE vertical_id = ?", (pr,)
    ).fetchone()
    assert count[0] == 2


def test_duplicate_membership_is_rejected(conn: sqlite3.Connection) -> None:
    vertical_id = _add_vertical(conn, "PR", 1)
    person_id = _add_person(conn, person_id=1)
    conn.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)",
        (person_id, vertical_id),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)",
            (person_id, vertical_id),
        )


def test_group_with_unknown_vertical_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _add_group(conn, "chan-unknown", 999)


def test_group_without_vertical_is_allowed(conn: sqlite3.Connection) -> None:
    group_id = _add_group(conn, "chan-unmapped", None)
    row = conn.execute(
        "SELECT vertical_id FROM source_group WHERE id = ?", (group_id,)
    ).fetchone()
    assert row is not None
    assert row["vertical_id"] is None


def test_membership_requires_existing_person_and_vertical(conn: sqlite3.Connection) -> None:
    vertical_id = _add_vertical(conn, "PR", 1)
    person_id = _add_person(conn, person_id=1)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)",
            (999, vertical_id),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)",
            (person_id, 999),
        )


def test_duplicate_vertical_name_is_rejected(conn: sqlite3.Connection) -> None:
    _add_vertical(conn, "PR", 1)
    with pytest.raises(sqlite3.IntegrityError):
        _add_vertical(conn, "PR", 2)


def test_duplicate_channel_id_is_rejected(conn: sqlite3.Connection) -> None:
    _add_group(conn, "chan-1", None, 1)
    with pytest.raises(sqlite3.IntegrityError):
        _add_group(conn, "chan-1", None, 2)


def test_duplicate_roll_number_is_rejected(conn: sqlite3.Connection) -> None:
    _add_person(conn, name="Asha", person_id=1, roll_number="B25CS1044")
    with pytest.raises(sqlite3.IntegrityError):
        _add_person(conn, name="Bilal", person_id=2, roll_number="B25CS1044")


def test_duplicate_discord_user_id_is_rejected(conn: sqlite3.Connection) -> None:
    _add_person(conn, name="Asha", person_id=1, discord_user_id="111")
    with pytest.raises(sqlite3.IntegrityError):
        _add_person(conn, name="Bilal", person_id=2, discord_user_id="111")


def test_duplicate_phone_is_rejected(conn: sqlite3.Connection) -> None:
    _add_person(conn, name="Asha", person_id=1, phone="9876543210")
    with pytest.raises(sqlite3.IntegrityError):
        _add_person(conn, name="Bilal", person_id=2, phone="9876543210")


def test_multiple_null_identifiers_are_allowed(conn: sqlite3.Connection) -> None:
    _add_person(conn, name="Asha", person_id=1)
    _add_person(conn, name="Bilal", person_id=2)
    _add_person(conn, name="Chen", person_id=3)

    rows = conn.execute(
        "SELECT roll_number, discord_user_id, phone FROM person ORDER BY id"
    ).fetchall()
    assert len(rows) == 3
    assert all(row["roll_number"] is None for row in rows)
    assert all(row["discord_user_id"] is None for row in rows)
    assert all(row["phone"] is None for row in rows)


def test_vertical_name_must_be_trimmed(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _add_vertical(conn, " PR ")


def test_role_must_be_trimmed_and_non_empty(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _add_person(conn, name="Asha", role=" AH ")
    with pytest.raises(sqlite3.IntegrityError):
        _add_person(conn, name="Bilal", role="   ")


def test_empty_text_fields_are_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _add_person(conn, name="   ", person_id=1)
    with pytest.raises(sqlite3.IntegrityError):
        _add_group(conn, "   ", None, 1)
    with pytest.raises(sqlite3.IntegrityError):
        _add_person(conn, name="Asha", person_id=2, discord_user_id=" ")
    with pytest.raises(sqlite3.IntegrityError):
        _add_person(conn, name="Asha", person_id=3, phone="")
    with pytest.raises(sqlite3.IntegrityError):
        _add_person(conn, name="Asha", person_id=4, reports_to="")
    with pytest.raises(sqlite3.IntegrityError):
        _add_person(conn, name="Asha", person_id=5, roll_number="")


def test_active_must_be_zero_or_one(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _add_person(conn, name="Asha", person_id=1, active=2)
    with pytest.raises(sqlite3.IntegrityError):
        _add_person(conn, name="Bilal", person_id=2, active=None)


def test_active_defaults_to_one(conn: sqlite3.Connection) -> None:
    person_id = _add_person(conn, name="Asha", person_id=1)
    row = conn.execute("SELECT active FROM person WHERE id = ?", (person_id,)).fetchone()
    assert row is not None
    assert row["active"] == 1


def test_deleting_referenced_vertical_is_restricted(conn: sqlite3.Connection) -> None:
    vertical_id = _add_vertical(conn, "PR", 1)
    _add_group(conn, "chan-pr", vertical_id, 1)
    person_id = _add_person(conn, person_id=1)
    conn.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)",
        (person_id, vertical_id),
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM vertical WHERE id = ?", (vertical_id,))

    assert conn.execute("SELECT COUNT(*) FROM source_group").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM person_vertical").fetchone()[0] == 1


def test_person_deletion_is_restricted_but_deactivation_is_allowed(
    conn: sqlite3.Connection,
) -> None:
    vertical_id = _add_vertical(conn, "PR", 1)
    person_id = _add_person(conn, person_id=1)
    conn.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)",
        (person_id, vertical_id),
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM person WHERE id = ?", (person_id,))

    conn.execute("UPDATE person SET active = 0 WHERE id = ?", (person_id,))
    row = conn.execute("SELECT active FROM person WHERE id = ?", (person_id,)).fetchone()
    assert row is not None
    assert row["active"] == 0
    assert conn.execute("SELECT COUNT(*) FROM person_vertical").fetchone()[0] == 1


def test_failed_later_migration_preserves_earlier_migration(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_ok.sql").write_text(
        "CREATE TABLE alpha (id INTEGER PRIMARY KEY);\nINSERT INTO alpha (id) VALUES (1);\n",
        encoding="utf-8",
    )
    (directory / "0002_broken.sql").write_text(
        "CREATE TABLE beta (id INTEGER PRIMARY KEY);\nCREATE TABLE beta (id INTEGER);\n",
        encoding="utf-8",
    )

    connection = connect(tmp_path / "test.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection, directory)

        assert "alpha" in _table_names(connection)
        assert "beta" not in _table_names(connection)
        assert _versions(connection) == [1]
        assert _applied_at(connection, 1)
        assert connection.execute("SELECT COUNT(*) FROM alpha").fetchone()[0] == 1
    finally:
        connection.close()


def test_default_migration_directory_contains_core_roster_migration() -> None:
    files = {path.name for path in Path(MIGRATIONS_DIR).glob("*.sql")}
    assert "0001_core_roster.sql" in files
