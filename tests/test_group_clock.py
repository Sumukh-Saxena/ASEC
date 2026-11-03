"""Per-group clock tests (PB-1, migration 0020; FR-BKT-05/07, NFR-REL-04).

The clock is the durable authority for settle-then-assign and closure: it never
moves backwards, its mode is locked on first write (O9-5: no seed), it survives
a restart, and closure happens only here, at ``e + C <= clock``, in ``(e, id)``
order.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.bucketing import BucketParams, assign_messages
from backend.clock import (
    ClockError,
    ClockModeError,
    ClockResult,
    advance_group_clock,
    read_group_clock,
)
from backend.db.connection import connect
from backend.db.migrations import apply_migrations

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_PARAMS = BucketParams(10, 5, 30, 3)  # SRS §10.1 test set


def _iso(seconds: int) -> str:
    return (_BASE + timedelta(seconds=seconds)).isoformat()


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "clock.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _group(connection: sqlite3.Connection, group_id: int = 1) -> None:
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (?, ?)",
        (group_id, f"chan-{group_id}"),
    )


def _message(connection: sqlite3.Connection, message_id: int, seconds: int) -> None:
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " event_timestamp, received_at, content)"
        " VALUES (?, 'discord', ?, 1, ?, ?, 'content')",
        (message_id, f"m-{message_id}", _iso(seconds), _iso(seconds)),
    )


def _advance(
    connection: sqlite3.Connection, seconds: int, mode: str = "LIVE"
) -> ClockResult:
    return advance_group_clock(connection, 1, now=_iso(seconds), mode=mode)


def _clock_row(connection: sqlite3.Connection) -> tuple[str, str] | None:
    row = connection.execute(
        "SELECT clock_at, mode FROM source_group_clock WHERE group_id = 1"
    ).fetchone()
    return None if row is None else (str(row["clock_at"]), str(row["mode"]))


def _state(connection: sqlite3.Connection, bucket_id: int) -> str:
    row = connection.execute(
        "SELECT processing_state FROM bucket WHERE id = ?", (bucket_id,)
    ).fetchone()
    assert row is not None
    return str(row["processing_state"])


def _end(connection: sqlite3.Connection, bucket_id: int) -> str | None:
    row = connection.execute(
        "SELECT end_event_timestamp FROM bucket WHERE id = ?", (bucket_id,)
    ).fetchone()
    assert row is not None
    value = row["end_event_timestamp"]
    return None if value is None else str(value)


def _progress(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT progress_to FROM source_group_progress WHERE group_id = 1"
    ).fetchone()
    return None if row is None else str(row["progress_to"])


def test_migration_0020_creates_an_unseeded_clock_table(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    assert conn.execute("SELECT COUNT(*) FROM source_group_clock").fetchone()[0] == 0


def test_first_call_creates_the_row_and_sets_the_mode(conn: sqlite3.Connection) -> None:
    _group(conn)
    result = _advance(conn, 10)
    assert (result.clock_at, result.advanced, result.closed_bucket_ids) == (
        _iso(10),
        True,
        (),
    )
    assert _clock_row(conn) == (_iso(10), "LIVE")


def test_clock_never_moves_backwards(conn: sqlite3.Connection) -> None:
    _group(conn)
    _advance(conn, 10)
    backwards = _advance(conn, 4)
    assert backwards.advanced is False
    assert backwards.clock_at == _iso(10)
    assert _clock_row(conn) == (_iso(10), "LIVE")
    forwards = _advance(conn, 12)
    assert forwards.advanced is True
    assert forwards.clock_at == _iso(12)


def test_equal_instant_is_a_noop_but_closure_still_runs(
    conn: sqlite3.Connection,
) -> None:
    """A bucket left OPEN by an earlier assignment closes on the next call."""
    _group(conn)
    _advance(conn, 20)
    _message(conn, 1, 0)
    assign_messages(conn, group_id=1, params=_PARAMS)
    assert _state(conn, 1) == "OPEN"  # assignment never closes
    result = _advance(conn, 20)
    assert result.advanced is False
    assert result.clock_at == _iso(20)
    assert result.closed_bucket_ids == (1,)
    assert _state(conn, 1) == "CLOSED"
    assert _end(conn, 1) == _iso(10)
    again = _advance(conn, 20)
    assert again.closed_bucket_ids == ()
    assert _end(conn, 1) == _iso(10)


def test_mode_is_locked_after_the_first_write(conn: sqlite3.Connection) -> None:
    _group(conn)
    _advance(conn, 10, mode="LIVE")
    with pytest.raises(ClockModeError):
        _advance(conn, 11, mode="REPLAY")
    assert _clock_row(conn) == (_iso(10), "LIVE")
    with pytest.raises(ClockModeError):
        advance_group_clock(conn, 1, now=_iso(12), mode="LIVE ")
    assert _clock_row(conn) == (_iso(10), "LIVE")


def test_replay_mode_is_locked_the_same_way(conn: sqlite3.Connection) -> None:
    _group(conn)
    _advance(conn, 10, mode="REPLAY")
    with pytest.raises(ClockModeError):
        _advance(conn, 11, mode="LIVE")
    assert _clock_row(conn) == (_iso(10), "REPLAY")


def test_invalid_mode_is_rejected_without_a_write(conn: sqlite3.Connection) -> None:
    _group(conn)
    with pytest.raises(ClockModeError):
        advance_group_clock(conn, 1, now=_iso(10), mode="MIXED")
    assert _clock_row(conn) is None
    assert _progress(conn) is None


@pytest.mark.parametrize("now", ["nonsense", "2026-01-01T00:00:10", ""])
def test_unusable_instant_is_rejected_without_a_write(
    conn: sqlite3.Connection, now: str
) -> None:
    _group(conn)
    with pytest.raises(ClockError):
        advance_group_clock(conn, 1, now=now, mode="LIVE")
    assert _clock_row(conn) is None
    assert _progress(conn) is None


def test_unknown_group_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        advance_group_clock(conn, 99, now=_iso(10), mode="LIVE")
    assert conn.execute("SELECT COUNT(*) FROM source_group_clock").fetchone()[0] == 0


def test_read_group_clock_reports_none_before_the_first_call(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    assert read_group_clock(conn, 1) is None
    _advance(conn, 10)
    stored = read_group_clock(conn, 1)
    assert stored is not None
    assert (stored.clock_at, stored.mode) == (_iso(10), "LIVE")


def test_closure_boundary_is_inclusive_at_e_plus_c(conn: sqlite3.Connection) -> None:
    _group(conn)
    _advance(conn, 3)
    _message(conn, 1, 0)
    assign_messages(conn, group_id=1, params=_PARAMS)
    assert _end(conn, 1) is None
    just_before = _advance(conn, 12)  # e=10, e+C=13
    assert just_before.closed_bucket_ids == ()
    assert _state(conn, 1) == "OPEN"
    at_boundary = _advance(conn, 13)
    assert at_boundary.closed_bucket_ids == (1,)
    assert _state(conn, 1) == "CLOSED"
    assert _end(conn, 1) == _iso(10)


def test_closure_order_is_e_then_bucket_id(conn: sqlite3.Connection) -> None:
    _group(conn)
    _advance(conn, 15)  # t=12 + C settles
    _message(conn, 1, 0)  # bucket 1, e=10
    _message(conn, 2, 12)  # bucket 2, e=22 (12-0 >= W)
    assign_messages(conn, group_id=1, params=_PARAMS)
    result = _advance(conn, 25)  # 10+3 <= 25 and 22+3 <= 25
    assert result.closed_bucket_ids == (1, 2)


def test_closure_uses_the_bucket_persisted_parameters(
    conn: sqlite3.Connection,
) -> None:
    """Closure follows the bucket's own parameters_used, never a later default."""
    _group(conn)
    custom = BucketParams(20, 5, 30, 4)  # e = 20, close at 24
    _advance(conn, 4)  # t=0 + C settles
    _message(conn, 1, 0)
    assign_messages(conn, group_id=1, params=custom)
    assert _advance(conn, 23).closed_bucket_ids == ()
    assert _advance(conn, 24).closed_bucket_ids == (1,)
    assert _end(conn, 1) == _iso(20)


def test_clock_writes_the_progress_boundary(conn: sqlite3.Connection) -> None:
    _group(conn)
    _advance(conn, 10)
    assert _progress(conn) == _iso(10)
    _advance(conn, 15)
    assert _progress(conn) == _iso(15)
    _advance(conn, 12)  # backwards: nothing changes
    assert _progress(conn) == _iso(15)


def test_clock_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "durable-clock.db"
    first = connect(path)
    apply_migrations(first)
    _group(first)
    _advance(first, 10)
    first.close()

    second = connect(path)
    try:
        stored = read_group_clock(second, 1)
        assert stored is not None
        assert (stored.clock_at, stored.mode) == (_iso(10), "LIVE")
        assert advance_group_clock(
            second, 1, now=_iso(9), mode="LIVE"
        ).advanced is False
        assert advance_group_clock(
            second, 1, now=_iso(11), mode="LIVE"
        ).advanced is True
    finally:
        second.close()


def test_clock_rows_are_per_group(conn: sqlite3.Connection) -> None:
    _group(conn, 1)
    _group(conn, 2)
    advance_group_clock(conn, 1, now=_iso(10), mode="LIVE")
    advance_group_clock(conn, 2, now=_iso(5), mode="REPLAY")
    rows = conn.execute(
        "SELECT group_id, clock_at, mode FROM source_group_clock ORDER BY group_id"
    ).fetchall()
    assert [(row["group_id"], row["clock_at"], row["mode"]) for row in rows] == [
        (1, _iso(10), "LIVE"),
        (2, _iso(5), "REPLAY"),
    ]


def test_database_stays_integral(conn: sqlite3.Connection) -> None:
    _group(conn)
    _advance(conn, 10)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert [str(row[0]) for row in conn.execute("PRAGMA integrity_check")] == ["ok"]
