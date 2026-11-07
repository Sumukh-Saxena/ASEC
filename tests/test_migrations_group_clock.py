"""Tests for 0020_group_clock (PB-1, FR-BKT-05/07).

The table is created unseeded (O9-5: no seed, the first caller sets the mode),
enforces one row per group, both modes and non-blank timestamps, and rejects a
group that does not exist.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "group-clock.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _versions(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [int(row["version"]) for row in rows]


def _group(connection: sqlite3.Connection, group_id: int = 1) -> None:
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (?, ?)",
        (group_id, f"chan-{group_id}"),
    )


def _clock(
    connection: sqlite3.Connection,
    group_id: int = 1,
    clock_at: str = "2026-01-01T00:00:10+00:00",
    mode: str = "LIVE",
    updated_at: str = "2026-01-01T00:00:10+00:00",
) -> None:
    connection.execute(
        "INSERT INTO source_group_clock (group_id, clock_at, mode, updated_at)"
        " VALUES (?, ?, ?, ?)",
        (group_id, clock_at, mode, updated_at),
    )


def test_migration_0020_applies_and_reapplies(conn: sqlite3.Connection) -> None:
    assert _versions(conn) == list(range(1, 23))
    assert apply_migrations(conn) == []


def test_clock_table_is_created_unseeded(conn: sqlite3.Connection) -> None:
    assert conn.execute("SELECT COUNT(*) FROM source_group_clock").fetchone()[0] == 0


def test_clock_accepts_both_modes(conn: sqlite3.Connection) -> None:
    _group(conn, 1)
    _group(conn, 2)
    _clock(conn, 1, mode="LIVE")
    _clock(conn, 2, mode="REPLAY")
    rows = conn.execute(
        "SELECT mode FROM source_group_clock ORDER BY group_id"
    ).fetchall()
    assert [str(row["mode"]) for row in rows] == ["LIVE", "REPLAY"]


def test_clock_rejects_an_unknown_mode(conn: sqlite3.Connection) -> None:
    _group(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _clock(conn, mode="MIXED")


@pytest.mark.parametrize(
    ("clock_at", "updated_at"),
    [("", "2026-01-01T00:00:10+00:00"), ("   ", "2026-01-01T00:00:10+00:00"),
     ("2026-01-01T00:00:10+00:00", ""), ("2026-01-01T00:00:10+00:00", "  ")],
)
def test_clock_rejects_blank_timestamps(
    conn: sqlite3.Connection, clock_at: str, updated_at: str
) -> None:
    _group(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _clock(conn, clock_at=clock_at, updated_at=updated_at)


def test_clock_is_one_row_per_group(conn: sqlite3.Connection) -> None:
    _group(conn)
    _clock(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _clock(conn, clock_at="2026-01-01T00:00:11+00:00")


def test_clock_rejects_a_missing_group(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _clock(conn, group_id=99)


def test_clock_group_is_restrict_deleted(conn: sqlite3.Connection) -> None:
    _group(conn)
    _clock(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM source_group WHERE id = 1")


def test_clock_database_is_integral(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert [str(row[0]) for row in conn.execute("PRAGMA integrity_check")] == ["ok"]
