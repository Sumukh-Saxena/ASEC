"""Bucket lifecycle service tests (Phase 3; SRS FR-BKT-12, FR-REL-01,
NFR-REL-01/03/05, AC-13).

Storage vocabulary comes verbatim from migration 0003; formation/closure is
already covered by tests/test_bucketing_service.py. This file pins the
service-level transition table the SRS leaves to later processing slices:

- CLOSED → PROCESSED / FAILED / AWAITING_RECOVERY
- FAILED → AWAITING_RECOVERY (NFR-REL-03 manual re-queue)
- AWAITING_RECOVERY → PROCESSED / FAILED (NFR-REL-05 / AC-13 resume)
- same state → itself: idempotent no-op (retry safety, NFR-REL-01)
- everything else: rejected loud, nothing written; PROCESSED is terminal,
  no edge ever restores OPEN or calls OPEN → CLOSED (that closure belongs
  to accepted event-time progress only, backend.bucketing — FR-BKT-05).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.bucket_lifecycle import (
    BucketState,
    InvalidBucketTransitionError,
    UnknownBucketError,
    bucket_processing_state,
    transition_bucket_processing_state,
)
from backend.db.connection import connect
from backend.db.migrations import apply_migrations


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    apply_migrations(connection)
    yield connection
    connection.close()


def _group(connection: sqlite3.Connection, group_id: int = 1) -> None:
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (?, ?)",
        (group_id, f"chan-{group_id}"),
    )


def _bucket(
    connection: sqlite3.Connection,
    state: BucketState = BucketState.CLOSED,
    group_id: int = 1,
) -> int:
    """Insert a minimal bucket row directly; returns its id."""
    cursor = connection.execute(
        "INSERT INTO bucket (group_id, start_event_timestamp,"
        " end_event_timestamp, parameters_used, processing_state)"
        " VALUES (?, '2026-01-01T10:00:00+00:00',"
        " '2026-01-01T10:00:10+00:00', '{}', ?)",
        (group_id, state.value),
    )
    return int(cursor.lastrowid or 0)


def _to_state(
    connection: sqlite3.Connection, bucket_id: int, target: BucketState
) -> bool:
    return transition_bucket_processing_state(connection, bucket_id, target)


def _assert_state(
    connection: sqlite3.Connection, bucket_id: int, expected: BucketState
) -> None:
    state = bucket_processing_state(connection, bucket_id)
    assert state is expected


def test_state_getter_roundtrip_and_unknown_bucket(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    bucket_id = _bucket(conn, BucketState.OPEN)
    assert bucket_processing_state(conn, bucket_id) is BucketState.OPEN
    with pytest.raises(UnknownBucketError):
        bucket_processing_state(conn, 10_000)


def test_closed_to_processed_applies(conn: sqlite3.Connection) -> None:
    _group(conn)
    bucket_id = _bucket(conn)
    assert _to_state(conn, bucket_id, BucketState.PROCESSED) is True
    _assert_state(conn, bucket_id, BucketState.PROCESSED)


def test_closed_to_failed_then_manual_requeue(conn: sqlite3.Connection) -> None:
    _group(conn)
    bucket_id = _bucket(conn)
    # Permanent failure, then the NFR-REL-03 manual re-queue edge.
    assert _to_state(conn, bucket_id, BucketState.FAILED) is True
    _assert_state(conn, bucket_id, BucketState.FAILED)
    assert _to_state(conn, bucket_id, BucketState.AWAITING_RECOVERY) is True
    _assert_state(conn, bucket_id, BucketState.AWAITING_RECOVERY)


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        (BucketState.AWAITING_RECOVERY, BucketState.PROCESSED),
        (BucketState.AWAITING_RECOVERY, BucketState.FAILED),
    ],
)
def test_recovery_resume_transitions(
    conn: sqlite3.Connection, initial: BucketState, target: BucketState
) -> None:
    """AC-13/NFR-REL-05: processing resumes after recovery and either
    completes (PROCESSED) or fails permanently again."""
    _group(conn)
    bucket_id = _bucket(conn, initial)
    assert _to_state(conn, bucket_id, target) is True
    _assert_state(conn, bucket_id, target)


@pytest.mark.parametrize("stable", list(BucketState))
def test_same_state_is_idempotent_noop(
    conn: sqlite3.Connection, stable: BucketState
) -> None:
    """NFR-REL-01: a retried call reports False and changes nothing."""
    _group(conn)
    bucket_id = _bucket(conn, stable)
    assert _to_state(conn, bucket_id, stable) is False
    _assert_state(conn, bucket_id, stable)


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        # Closure and opening are progress-owned (FR-BKT-05), never service.
        (BucketState.OPEN, BucketState.CLOSED),
        # Processing cannot touch an OPEN bucket (FR-REL-01: closed only).
        (BucketState.OPEN, BucketState.PROCESSED),
        (BucketState.OPEN, BucketState.FAILED),
        (BucketState.OPEN, BucketState.AWAITING_RECOVERY),
        # PROCESSED is terminal.
        (BucketState.PROCESSED, BucketState.CLOSED),
        (BucketState.PROCESSED, BucketState.OPEN),
        (BucketState.PROCESSED, BucketState.FAILED),
        (BucketState.PROCESSED, BucketState.AWAITING_RECOVERY),
        # No edge ever restores a queue state.
        (BucketState.AWAITING_RECOVERY, BucketState.CLOSED),
        (BucketState.AWAITING_RECOVERY, BucketState.OPEN),
        (BucketState.FAILED, BucketState.CLOSED),
        (BucketState.FAILED, BucketState.OPEN),
        (BucketState.FAILED, BucketState.PROCESSED),
        # Outer edges.
        (BucketState.CLOSED, BucketState.OPEN),
    ],
)
def test_illegal_edges_rejected_and_state_untouched(
    conn: sqlite3.Connection, initial: BucketState, target: BucketState
) -> None:
    _group(conn)
    bucket_id = _bucket(conn, initial)
    with pytest.raises(InvalidBucketTransitionError):
        _to_state(conn, bucket_id, target)
    _assert_state(conn, bucket_id, initial)


@pytest.mark.parametrize("raw", [None, "PROCESSED", 7])
def test_non_enum_target_rejected(
    conn: sqlite3.Connection, raw: object
) -> None:
    """The FR-BKT-12 token never travels as a bare string at the API."""
    _group(conn)
    bucket_id = _bucket(conn)
    with pytest.raises(InvalidBucketTransitionError):
        transition_bucket_processing_state(conn, bucket_id, raw)  # type: ignore[arg-type]
    _assert_state(conn, bucket_id, BucketState.CLOSED)


def test_transition_unknown_bucket_fails_loud(conn: sqlite3.Connection) -> None:
    _group(conn)
    with pytest.raises(UnknownBucketError):
        _to_state(conn, 4_242, BucketState.PROCESSED)


def test_competing_calls_see_committed_state_only(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """FR-BKT-07 discipline across connections: the first writer wins and
    the second sees the committed result — the retry is an idempotent
    no-op and an edge against the new state is rejected."""
    db_path = tmp_path / "lifecycle.db"
    writer = connect(db_path)
    apply_migrations(writer)
    watcher = connect(db_path)
    watcher.execute("PRAGMA foreign_keys = ON")
    _group(writer)
    bucket_id = _bucket(writer)

    assert transition_bucket_processing_state(
        writer, bucket_id, BucketState.PROCESSED
    ) is True
    writer.commit()
    # A stale retry lands on committed PROCESSED → idempotent no-op.
    assert transition_bucket_processing_state(
        watcher, bucket_id, BucketState.PROCESSED
    ) is False
    # Claiming CLOSED from PROCESSED is illegal in any connection.
    with pytest.raises(InvalidBucketTransitionError):
        transition_bucket_processing_state(watcher, bucket_id, BucketState.FAILED)
    _assert_state(watcher, bucket_id, BucketState.PROCESSED)
    writer.close()
    watcher.close()


def test_workload_leaves_foreign_keys_clean(conn: sqlite3.Connection) -> None:
    _group(conn)
    failed = _bucket(conn)
    recovered = _bucket(conn, BucketState.AWAITING_RECOVERY)
    assert _to_state(conn, failed, BucketState.FAILED) is True
    assert _to_state(conn, failed, BucketState.AWAITING_RECOVERY) is True
    assert _to_state(conn, failed, BucketState.FAILED) is True
    assert _to_state(conn, recovered, BucketState.PROCESSED) is True
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
