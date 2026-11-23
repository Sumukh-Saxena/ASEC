"""Migration 0026: the two system control switches (D2, owner decision C)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations


@pytest.fixture()
def connection(tmp_path: Path) -> sqlite3.Connection:
    database = tmp_path / "system-migration.db"
    conn = connect(database)
    apply_migrations(conn)
    conn.execute("INSERT INTO vertical (id, name) VALUES (1, 'Logistics')")
    conn.execute(
        "INSERT INTO person (id, name, phone, role, active, roll_number)"
        " VALUES (1, 'Admin One', 'ph-1', 'Admin', 1, 'R-ADMIN')"
    )
    yield conn
    conn.close()


def test_the_switch_table_has_a_row_per_control_and_no_seed_rows(
    connection: sqlite3.Connection,
) -> None:
    columns = {
        row["name"]: row
        for row in connection.execute("PRAGMA table_info(system_control)")
    }
    assert set(columns) == {"name", "paused", "updated_at", "updated_by"}
    assert columns["paused"]["notnull"] == 1
    assert columns["paused"]["dflt_value"] == "0"
    assert columns["updated_by"]["notnull"] == 0
    # Absent means resumed; the migration seeds no state.
    assert connection.execute(
        "SELECT COUNT(*) AS n FROM system_control"
    ).fetchone()["n"] == 0


def test_only_the_two_known_controls_are_accepted(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO system_control (name, paused, updated_at)"
            " VALUES ('EVERYTHING', 1, '2026-11-18T10:00:00+00:00')"
        )


def test_paused_is_boolean_and_updated_at_is_not_blank(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO system_control (name, paused, updated_at)"
            " VALUES ('PROCESSING', 2, '2026-11-18T10:00:00+00:00')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO system_control (name, paused, updated_at)"
            " VALUES ('DELIVERY', 1, '   ')"
        )


def test_updated_by_references_a_real_person(
    connection: sqlite3.Connection,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO system_control (name, paused, updated_at, updated_by)"
            " VALUES ('PROCESSING', 1, '2026-11-18T10:00:00+00:00', 99)"
        )
    connection.execute(
        "INSERT INTO system_control (name, paused, updated_at, updated_by)"
        " VALUES ('PROCESSING', 1, '2026-11-18T10:00:00+00:00', 1)"
    )
    assert connection.execute(
        "SELECT COUNT(*) AS n FROM system_control"
    ).fetchone()["n"] == 1
