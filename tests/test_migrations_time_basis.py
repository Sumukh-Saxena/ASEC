"""Tests for 0019_source_message_time_basis (PB-2, FR-ING-02, DR-03).

The migration is additive: ``source_message.time_basis`` starts NULL for every
legacy row (no invented backfill), accepts only the two recorded bases, and a
re-apply is a no-op.
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
    connection = connect(tmp_path / "time-basis.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _versions(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [int(row["version"]) for row in rows]


def _group(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (1, 'chan-19')"
    )


def _message(
    connection: sqlite3.Connection,
    message_id: int,
    *,
    time_basis: str | None = None,
    group_id: int = 1,
) -> None:
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " event_timestamp, received_at, content, time_basis)"
        " VALUES (?, 'discord', ?, ?, '2026-01-01T00:00:00+00:00',"
        " '2026-01-01T00:00:01+00:00', 'content', ?)",
        (message_id, f"m-{message_id}", group_id, time_basis),
    )


def _basis(connection: sqlite3.Connection, message_id: int) -> str | None:
    row = connection.execute(
        "SELECT time_basis FROM source_message WHERE id = ?", (message_id,)
    ).fetchone()
    assert row is not None
    value = row["time_basis"]
    return None if value is None else str(value)


def test_migration_0019_applies_and_reapplies(conn: sqlite3.Connection) -> None:
    assert _versions(conn) == list(range(1, 26))
    assert apply_migrations(conn) == []


def test_time_basis_column_is_nullable_and_starts_null(
    conn: sqlite3.Connection,
) -> None:
    columns = {
        str(row["name"]): row
        for row in conn.execute("PRAGMA table_info(source_message)").fetchall()
    }
    assert "time_basis" in columns
    assert columns["time_basis"]["notnull"] == 0
    assert columns["time_basis"]["dflt_value"] is None

    _group(conn)
    _message(conn, 1)  # legacy shape: no basis supplied
    assert _basis(conn, 1) is None


def test_time_basis_accepts_the_two_recorded_bases(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, time_basis="EVENT")
    _message(conn, 2, time_basis="RECEIPT")
    assert _basis(conn, 1) == "EVENT"
    assert _basis(conn, 2) == "RECEIPT"


def test_time_basis_rejects_an_unknown_token(conn: sqlite3.Connection) -> None:
    _group(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _message(conn, 1, time_basis="GUESS")


def test_time_basis_keeps_group_foreign_key(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _message(conn, 1, group_id=999)


def test_time_basis_database_is_integral(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    integrity = conn.execute("PRAGMA integrity_check").fetchall()
    assert [str(row[0]) for row in integrity] == ["ok"]
