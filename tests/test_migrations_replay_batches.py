"""Tests for 0021_replay_batches (FR-ING-08, O9-4 amendment).

The replay batch row is provenance, not an owner of messages: no foreign keys,
one row per (file hash, time zone), free-text outcome and a JSON count record.
It can record a completed and a failed batch.
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
    connection = connect(tmp_path / "replay-batches.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _versions(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [int(row["version"]) for row in rows]


def _batch(
    connection: sqlite3.Connection,
    *,
    sha: str = "a" * 64,
    time_zone: str = "Asia/Kolkata",
    outcome: str = "COMPLETED",
    finished_at: str | None = "2026-01-01T00:01:00+00:00",
    counts: str = '{"inserted": 1}',
) -> int:
    cursor = connection.execute(
        "INSERT INTO replay_batch (source, file_sha256, time_zone, started_at,"
        " finished_at, outcome, counts_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "replay",
            sha,
            time_zone,
            "2026-01-01T00:00:00+00:00",
            finished_at,
            outcome,
            counts,
        ),
    )
    return int(cursor.lastrowid or 0)


def test_migration_0021_applies_and_reapplies(conn: sqlite3.Connection) -> None:
    assert _versions(conn) == list(range(1, 25))
    assert apply_migrations(conn) == []


def test_replay_batch_table_is_created_empty(conn: sqlite3.Connection) -> None:
    assert conn.execute("SELECT COUNT(*) FROM replay_batch").fetchone()[0] == 0


def test_completed_and_failed_batches_are_recorded(conn: sqlite3.Connection) -> None:
    completed = _batch(conn, sha="a" * 64, outcome="COMPLETED")
    failed = _batch(
        conn,
        sha="b" * 64,
        outcome="FAILED",
        finished_at=None,
        counts='{"inserted": 0, "error": "storage failure"}',
    )
    rows = conn.execute(
        "SELECT id, outcome, finished_at, counts_json FROM replay_batch ORDER BY id"
    ).fetchall()
    assert [(row["id"], row["outcome"]) for row in rows] == [
        (completed, "COMPLETED"),
        (failed, "FAILED"),
    ]
    assert rows[1]["finished_at"] is None
    assert '"error"' in str(rows[1]["counts_json"])


def test_one_row_per_file_hash_and_time_zone(conn: sqlite3.Connection) -> None:
    _batch(conn, sha="c" * 64, time_zone="Asia/Kolkata")
    with pytest.raises(sqlite3.IntegrityError):
        _batch(conn, sha="c" * 64, time_zone="Asia/Kolkata")
    _batch(conn, sha="c" * 64, time_zone="UTC")


@pytest.mark.parametrize(
    "column",
    ["source", "file_sha256", "time_zone", "started_at", "outcome", "counts_json"],
)
def test_blank_required_values_are_rejected(
    conn: sqlite3.Connection, column: str
) -> None:
    values = {
        "source": "replay",
        "file_sha256": "d" * 64,
        "time_zone": "Asia/Kolkata",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": None,
        "outcome": "COMPLETED",
        "counts_json": "{}",
    }
    values[column] = "   "
    placeholders = ", ".join("?" for _ in values)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO replay_batch ({', '.join(values)}) VALUES ({placeholders})",
            tuple(values.values()),
        )


def test_database_stays_integral(conn: sqlite3.Connection) -> None:
    _batch(conn)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert [str(row[0]) for row in conn.execute("PRAGMA integrity_check")] == ["ok"]
