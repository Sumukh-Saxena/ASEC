"""Pipeline orchestration and visibility tests (Phase 3).

Covers ingestion → formation → closure → lifecycle → backlog/queue
coherence: SRS FR-BKT-01..05, FR-BKT-12, FR-REL-01, NFR-REL-01/05/06,
AC-13, FR-UI-05 (data availability, not UI), FR-BKT-02 (per-group
independence), and the D-1 unmapped-channel integration from Phase 2
ingestion.

Event-time progress remains the only closure authority: no test in this
file expects a pass, a backlog read or a queue read to close anything.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from backend.bucket_lifecycle import (
    BucketState,
    transition_bucket_processing_state,
)
from backend.bucketing import BucketParams, mark_stream_progress
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.ingest import MessageEvent, ingest_message
from backend.orchestration import (
    pipeline_backlog,
    processing_queue,
    run_bucketing_pass,
)

_PARAMSED = BucketParams(10, 5, 30, 3)  # SRS §10.1 test parameter set
_BASE = datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC)


def _iso(seconds: int) -> str:
    return (_BASE + timedelta(seconds=seconds)).isoformat()


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "orchestration.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _group(
    connection: sqlite3.Connection, group_id: int = 1, channel: str = "chan-A"
) -> None:
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (?, ?)",
        (group_id, channel),
    )


def _event(
    source_message_id: str = "101",
    channel: str = "DMZ-1",
    event_timestamp: str | None = None,
    **overrides: Any,
) -> MessageEvent:
    fields: dict[str, Any] = {
        "source": "discord",
        "source_message_id": source_message_id,
        "discord_channel_id": channel,
        "sender_discord_user_id": "u-1",
        "event_timestamp": event_timestamp,
        "received_at": "2026-05-01T10:00:01+00:00",
        "content": "Standup at noon",
        "reply_reference": None,
        "raw_metadata": None,
        "mentions": (),
        "reactions": (),
    }
    fields.update(overrides)
    return MessageEvent(**fields)


def _ingest(
    connection: sqlite3.Connection, event: MessageEvent
) -> int:
    return ingest_message(connection, event).message_id


def _members(
    connection: sqlite3.Connection,
) -> dict[int, list[int]]:
    rows = connection.execute(
        "SELECT bucket_id, message_id FROM bucket_message"
        " ORDER BY bucket_id, message_id"
    ).fetchall()
    buckets: dict[int, list[int]] = {}
    for row in rows:
        buckets.setdefault(int(row["bucket_id"]), []).append(
            int(row["message_id"])
        )
    return buckets


def _states(
    connection: sqlite3.Connection,
) -> dict[int, str]:
    rows = connection.execute(
        "SELECT id, processing_state FROM bucket ORDER BY id"
    ).fetchall()
    return {int(row["id"]): str(row["processing_state"]) for row in rows}


# --- Orchestration pass -------------------------------------------------


def test_pass_assigns_ingested_messages_per_group(
    conn: sqlite3.Connection,
) -> None:
    """FR-BKT-01/02: every ingested message lands in exactly one bucket,
    and separate channels form their buckets independently."""
    m1 = _ingest(conn, _event("1", "chan-A", _iso(0)))
    m2 = _ingest(conn, _event("2", "chan-A", _iso(5)))
    m3 = _ingest(conn, _event("7", "chan-B", _iso(120)))

    result = run_bucketing_pass(conn, params=_PARAMSED)

    assert set(result.assigned) == {(m1, 1), (m2, 1), (m3, 2)}
    assert result.skipped == ()
    members = _members(conn)
    assert sorted(members[1]) == sorted([m1, m2])
    assert members[2] == [m3]
    assert _states(conn) == {1: "OPEN", 2: "OPEN"}


def test_pass_is_retry_safe(conn: sqlite3.Connection) -> None:
    """NFR-REL-01 at the pass level: rerunning changes nothing."""
    _ingest(conn, _event("1", "chan-A", _iso(0)))
    first = run_bucketing_pass(conn, params=_PARAMSED)
    assert first.assigned

    second = run_bucketing_pass(conn, params=_PARAMSED)

    assert second.assigned == ()
    assert second.skipped == ()
    assert len(_states(conn)) == 1
    assert len(_members(conn)[1]) == 1


def test_pass_never_closes_and_never_writes_progress(
    conn: sqlite3.Connection,
) -> None:
    """FR-BKT-05: formation closure stays with accepted progress; the
    orchestrated pass is formation only."""
    _ingest(conn, _event("1", "chan-A", _iso(0)))
    run_bucketing_pass(conn, params=_PARAMSED)

    assert _states(conn) == {1: "OPEN"}
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM source_group_progress"
    ).fetchone()["n"] == 0
    # sanity: an accepted mark closes it, unchanged from the owned module
    closed = mark_stream_progress(conn, group_id=1, event_progress=_iso(47))
    assert closed.accepted is True
    assert closed.closed_bucket_ids == (1,)


def test_restart_persists_everything_and_pass_recovers(
    conn: sqlite3.Connection,
) -> None:
    """NFR-REL-05 / AC-13 half in-repo: reopening the store shows the same
    committed state; a fresh pass loses and duplicates nothing."""
    _ingest(conn, _event("1", "chan-A", _iso(0)))
    _ingest(conn, _event("2", "chan-A", _iso(15)))
    run_bucketing_pass(conn, params=_PARAMSED)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(20))
    states_before = _states(conn)
    reopened = connect(conn.execute("PRAGMA database_list").fetchone()[2])
    apply_migrations(reopened)  # tracking table already satisfied
    assert _states(reopened) == states_before
    assert run_bucketing_pass(reopened, params=_PARAMSED).assigned == ()
    assert _states(reopened) == states_before
    reopened.close()


# --- Backlog visibility (NFR-REL-06, FR-UI-05) --------------------------


def test_backlog_honors_lifecycle_states_and_reports_oldest(
    conn: sqlite3.Connection,
) -> None:
    """Unprocessed = everything not PROCESSED; the oldest anchor and its
    event-time age against accepted progress are visible."""
    _group(conn, 1, "chan-G1")
    conn.execute(
        "INSERT INTO bucket (group_id, start_event_timestamp,"
        " end_event_timestamp, parameters_used, processing_state)"
        " VALUES (1, ?, ?, '{}', 'CLOSED')",
        (_iso(0), _iso(10)),
    )
    conn.execute(
        "UPDATE bucket SET processing_state = 'PROCESSED' WHERE id = 1"
    )
    conn.execute(
        "INSERT INTO bucket (group_id, start_event_timestamp,"
        " end_event_timestamp, parameters_used, processing_state)"
        " VALUES (1, ?, ?, '{}', 'CLOSED')",
        (_iso(12), _iso(20)),
    )
    conn.execute(
        "INSERT INTO bucket (group_id, start_event_timestamp,"
        " end_event_timestamp, parameters_used, processing_state)"
        " VALUES (1, ?, ?, '{}', 'FAILED')",
        (_iso(0), _iso(12)),
    )
    mark_stream_progress(conn, group_id=1, event_progress=_iso(30))

    report = pipeline_backlog(conn)
    backlog = report.groups[0]

    assert backlog.group_id == 1
    assert backlog.buckets_by_state == {
        "OPEN": 0,
        "CLOSED": 1,
        "PROCESSED": 1,
        "FAILED": 1,
        "AWAITING_RECOVERY": 0,
    }
    assert backlog.unprocessed_buckets == 2
    # smallest anchor (end else start) among unprocessed: FAILED end 10:00:12
    assert backlog.oldest_unprocessed_anchor == _iso(12)
    assert backlog.age_event_seconds == 18
    assert backlog.progress_to == _iso(30)
    assert report.unprocessed_buckets == 2


def test_backlog_includes_groups_with_no_buckets_and_counts_skipped(
    conn: sqlite3.Connection,
) -> None:
    """NFR-REL-06: an empty group shows zero backlog, and messages the
    formation pass must skip (missing event time) stay visible as
    unassigned backlog — never silent loss."""
    _ingest(conn, _event("1", "chan-A", None))  # missing event timestamp
    _ingest(conn, _event("2", "chan-B", _iso(0)))  # chan-B: no buckets yet

    result = run_bucketing_pass(conn, params=_PARAMSED)
    assert len(result.skipped) == 1

    report = pipeline_backlog(conn)
    report_by_group = {group.group_id: group for group in report.groups}
    skipped_holder = report_by_group[1]
    assert skipped_holder.unassigned_messages == 1
    assert skipped_holder.unprocessed_buckets == 0
    assert skipped_holder.oldest_unprocessed_anchor is None
    assert skipped_holder.age_event_seconds is None
    assigned_holder = report_by_group[2]
    assert assigned_holder.unassigned_messages == 0
    assert assigned_holder.buckets_by_state["OPEN"] == 1
    assert assigned_holder.progress_to is None
    assert assigned_holder.age_event_seconds is None
    assert report.unassigned_messages == 1


def test_backlog_after_lifecycle_processing_drains(
    conn: sqlite3.Connection,
) -> None:
    _ingest(conn, _event("1", "chan-A", _iso(0)))
    run_bucketing_pass(conn, params=_PARAMSED)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(47))
    bucket_id = 1

    assert pipeline_backlog(conn).groups[0].unprocessed_buckets == 1
    assert (
        transition_bucket_processing_state(conn, bucket_id, BucketState.PROCESSED)
        is True
    )
    done = pipeline_backlog(conn).groups[0]
    assert done.unprocessed_buckets == 0
    assert done.buckets_by_state["PROCESSED"] == 1
    assert done.oldest_unprocessed_bucket_id is None
    assert done.age_event_seconds is None


def test_undatable_anchor_is_oldest_unprocessed(
    conn: sqlite3.Connection,
) -> None:
    """An unprocessed bucket with a missing or unparsable anchor cannot be
    dated; it is the oldest unprocessed item and gets no derived age."""
    _group(conn, 1, "chan-A")
    conn.execute(
        "INSERT INTO bucket (group_id, start_event_timestamp,"
        " end_event_timestamp, parameters_used, processing_state)"
        " VALUES (1, ?, ?, '{}', 'CLOSED')",
        (_iso(0), _iso(10)),
    )
    conn.execute(
        "INSERT INTO bucket (group_id, parameters_used, processing_state)"
        " VALUES (1, '{}', 'CLOSED')"
    )
    mark_stream_progress(conn, group_id=1, event_progress=_iso(30))

    backlog = pipeline_backlog(conn).groups[0]

    assert backlog.unprocessed_buckets == 2
    assert backlog.oldest_unprocessed_bucket_id == 2
    assert backlog.oldest_unprocessed_anchor is None
    assert backlog.age_event_seconds is None


def test_progress_regression_is_noop_in_backlog(
    conn: sqlite3.Connection,
) -> None:
    _group(conn, 1, "chan-A")
    mark_stream_progress(conn, group_id=1, event_progress=_iso(20))

    regression = mark_stream_progress(conn, group_id=1, event_progress=_iso(15))

    assert regression.accepted is False
    assert regression.regressed is True
    report = pipeline_backlog(conn).groups[0]
    assert report.progress_to == _iso(20)


def test_backlog_age_zero_is_valid_and_boundary_beyond_progress_unavailable(
    conn: sqlite3.Connection,
) -> None:
    """S2-1 correction (2026-10-21): a valid non-negative age is preserved
    (zero included), while an unprocessed bucket whose event-time boundary
    exceeds accepted progress reports no age — never a negative one. Bucket
    formation, closure and progress ownership are untouched."""
    _group(conn, 1, "chan-A")
    _group(conn, 2, "chan-B")
    _params_json = (
        '{"window_w": 10, "burst_gap_g": 5, "max_duration_m": 30, "close_grace_c": 3}'
    )
    # group 1: the unprocessed bucket's boundary (end) is ahead of progress.
    conn.execute(
        "INSERT INTO bucket (group_id, start_event_timestamp,"
        " end_event_timestamp, parameters_used, processing_state)"
        " VALUES (1, ?, ?, ?, 'CLOSED')",
        (_iso(0), _iso(50), _params_json),
    )
    # group 2: the boundary is exactly at accepted progress → age 0 is valid.
    conn.execute(
        "INSERT INTO bucket (group_id, start_event_timestamp,"
        " end_event_timestamp, parameters_used, processing_state)"
        " VALUES (2, ?, ?, ?, 'CLOSED')",
        (_iso(20), _iso(20), _params_json),
    )
    mark_stream_progress(conn, group_id=1, event_progress=_iso(20))
    mark_stream_progress(conn, group_id=2, event_progress=_iso(20))

    backlog = pipeline_backlog(conn)
    beyond = backlog.groups[0]
    exact = backlog.groups[1]

    assert beyond.unprocessed_buckets == 1
    assert beyond.oldest_unprocessed_anchor == _iso(50)
    assert beyond.age_event_seconds is None
    assert exact.unprocessed_buckets == 1
    assert exact.oldest_unprocessed_anchor == _iso(20)
    assert exact.age_event_seconds == 0


# --- Processing queue (§4.1, FR-REL-01, NFR-REL-03/05) ------------------


def _attempt(
    connection: sqlite3.Connection, bucket_id: int, outcome: str = "SUCCEEDED"
) -> None:
    connection.execute(
        "INSERT INTO processing_attempt (stage, bucket_id, started_at,"
        " finished_at, outcome) VALUES ('relevance', ?,"
        " '2026-05-01T11:00:00+00:00', '2026-05-01T11:00:02+00:00', ?)",
        (bucket_id, outcome),
    )


def test_queue_never_started_drains_when_attempt_exists(
    conn: sqlite3.Connection,
) -> None:
    _ingest(conn, _event("1", "chan-A", _iso(0)))
    run_bucketing_pass(conn, params=_PARAMSED)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(47))

    queue = processing_queue(conn)
    assert queue.never_started == (1,)
    assert queue.awaiting_recovery == ()
    assert queue.failed == ()

    _attempt(conn, 1)
    queue_after = processing_queue(conn)
    assert queue_after.never_started == ()


def test_queue_holds_recovery_and_failed_states(
    conn: sqlite3.Connection,
) -> None:
    """NFR-REL-05: outage-paused buckets wait in AWAITING_RECOVERY until an
    explicit lifecycle change moves them; FAILED buckets wait for the
    NFR-REL-03 manual re-queue."""
    _ingest(conn, _event("1", "chan-A", _iso(0)))
    run_bucketing_pass(conn, params=_PARAMSED)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(47))

    assert transition_bucket_processing_state(
        conn, 1, BucketState.AWAITING_RECOVERY
    ) is True
    assert processing_queue(conn).awaiting_recovery == (1,)

    assert transition_bucket_processing_state(conn, 1, BucketState.FAILED) is True
    queue = processing_queue(conn)
    assert queue.failed == (1,)
    assert queue.awaiting_recovery == ()
    assert queue.never_started == ()

    assert transition_bucket_processing_state(
        conn, 1, BucketState.AWAITING_RECOVERY
    ) is True
    recovered = processing_queue(conn)
    assert recovered.awaiting_recovery == (1,)
    assert recovered.failed == ()


# --- Phase 2 integration ------------------------------------------------


def test_unmapped_auto_registered_channel_flows_through(
    conn: sqlite3.Connection,
) -> None:
    """D-1 integration: a message from an unknown channel auto-registers
    its group (vertical_id NULL, UNMAPPED flag) and still forms and
    processes as a full bucket — Phase 2 ingestion feeding Phase 3."""
    _ingest(conn, _event("9", "brand-new-channel", _iso(0)))
    row = conn.execute(
        "SELECT s.id, s.vertical_id, m.flag_unmapped"
        " FROM source_group s JOIN source_message m ON m.group_id = s.id"
    ).fetchone()
    group_id = int(row["id"])
    assert row["vertical_id"] is None
    assert row["flag_unmapped"] == 1

    run_bucketing_pass(conn, params=_PARAMSED)
    mark_stream_progress(conn, group_id=group_id, event_progress=_iso(47))
    assert transition_bucket_processing_state(
        conn, 1, BucketState.PROCESSED
    ) is True

    queue = processing_queue(conn)
    assert queue.never_started == ()
    assert pipeline_backlog(conn).groups[0].unprocessed_buckets == 0
