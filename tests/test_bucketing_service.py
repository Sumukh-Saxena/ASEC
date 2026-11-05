"""Bucket-formation service tests (SRS §3.2 FR-BKT-01..08, 10, 12; §10.1 matrix).

Storage slice tests live in tests/test_migrations_*; this file tests the
service behavior over migration 0002/0003 plus 0010 (source_group_progress),
including migration application/tracking for 0010.

Binding semantics under test (engineering lead decisions):
- Assignment never closes an existing bucket and never advances progress.
- Closure iff e + C <= accepted progress P (inclusive), only via
  mark_stream_progress; born-CLOSED late buckets use the same condition.
- Late iff t <= P, or t < current OPEN bucket start when one exists; every
  late message creates its own bucket.
- Current OPEN bucket = greatest (start_event_timestamp, id) among OPEN
  buckets; FR-BKT-04 is evaluated only against it.
- Predecessor graph: greatest CLOSED start <= t (ties greatest id), else
  least CLOSED start (recovery fallback), else current OPEN, else NULL.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from backend.bucketing import (
    AssignmentResult,
    BucketParams,
    BucketParamsError,
    SkippedMessage,
    SkipReason,
    assign_messages,
    load_bucket_params,
    mark_stream_progress,
)
from backend.config import Config
from backend.db.connection import connect
from backend.db.migrations import apply_migrations

_BASE = datetime(2026, 1, 1, tzinfo=UTC)

# SRS §10.1 declares its own test parameter set (W=15, G=5, M=30, C=2), which
# currently matches the service's BucketParams() production defaults from §3.2.
# Every §10.1-scoped test pins the §10.1 values explicitly via the _assign
# helper; tests of the production defaults call assign_messages directly
# without params.
_PARAMS_101 = BucketParams(15, 5, 30, 2)

# Configuration keys accepted by load_bucket_params (documented in .env.example).
_BUCKET_CONFIG_KEYS = (
    "BUCKET_WINDOW_W_S",
    "BUCKET_BURST_GAP_G_S",
    "BUCKET_MAX_DURATION_M_S",
    "BUCKET_CLOSE_GRACE_C_S",
)


def _iso(seconds: int) -> str:
    """UTC ISO-8601 event timestamp `seconds` after the test base instant."""
    return (_BASE + timedelta(seconds=seconds)).isoformat()


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


def _message(
    connection: sqlite3.Connection,
    message_id: int,
    event_seconds: int | None,
    *,
    group_id: int = 1,
    flag_late: int = 0,
    content: str | None = "content",
    received_at: str = "recv",
) -> None:
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " event_timestamp, received_at, content, flag_late)"
        " VALUES (?, 'discord', ?, ?, ?, ?, ?, ?)",
        (
            message_id,
            f"m-{message_id}",
            group_id,
            None if event_seconds is None else _iso(event_seconds),
            received_at,
            content,
            flag_late,
        ),
    )


def _assign(
    connection: sqlite3.Connection,
    group_id: int | None = None,
    params: BucketParams = _PARAMS_101,
) -> AssignmentResult:
    return assign_messages(connection, group_id=group_id, params=params)


def _buckets(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT id, predecessor_bucket_id, start_event_timestamp,"
        " end_event_timestamp, parameters_used, processing_state"
        " FROM bucket ORDER BY start_event_timestamp, id"
    ).fetchall()
    return [dict(row) for row in rows]


def _members(connection: sqlite3.Connection, bucket_id: int) -> list[int]:
    rows = connection.execute(
        "SELECT bm.message_id FROM bucket_message bm"
        " JOIN source_message m ON m.id = bm.message_id"
        " WHERE bm.bucket_id = ? ORDER BY m.event_timestamp, m.id",
        (bucket_id,),
    ).fetchall()
    return [int(row["message_id"]) for row in rows]


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


def _flag_late(connection: sqlite3.Connection, message_id: int) -> int:
    row = connection.execute(
        "SELECT flag_late FROM source_message WHERE id = ?", (message_id,)
    ).fetchone()
    assert row is not None
    return int(row["flag_late"])


def _time_basis(connection: sqlite3.Connection, message_id: int) -> str | None:
    row = connection.execute(
        "SELECT time_basis FROM source_message WHERE id = ?", (message_id,)
    ).fetchone()
    assert row is not None
    value = row["time_basis"]
    return None if value is None else str(value)


def _stored_event(connection: sqlite3.Connection, message_id: int) -> str | None:
    row = connection.execute(
        "SELECT event_timestamp FROM source_message WHERE id = ?", (message_id,)
    ).fetchone()
    assert row is not None
    value = row["event_timestamp"]
    return None if value is None else str(value)


def _fk_clean(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _progress_row(connection: sqlite3.Connection) -> tuple[str, str] | None:
    row = connection.execute(
        "SELECT progress_to, updated_at FROM source_group_progress WHERE group_id = 1"
    ).fetchone()
    return None if row is None else (str(row["progress_to"]), str(row["updated_at"]))


# --- migration 0010 tracking -------------------------------------------------


class TestMigration0010:
    def test_applies_and_idempotent(self, conn: sqlite3.Connection) -> None:
        applied = apply_migrations(conn)
        assert applied == []
        versions = {
            int(row["version"])
            for row in conn.execute("SELECT version FROM schema_migrations")
        }
        assert {1, 2, 3, 4, 5, 6, 7, 8, 9, 10} <= versions
        conn.execute("INSERT INTO source_group (id, discord_channel_id) VALUES (1, 'c')")
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("source_group_progress",),
        ).fetchone()
        assert table is not None

    def test_group_delete_restricted(self, conn: sqlite3.Connection) -> None:
        _group(conn)
        conn.execute(
            "INSERT INTO source_group_progress (group_id, progress_to, updated_at)"
            " VALUES (1, ?, ?)",
            (_iso(0), "u"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM source_group WHERE id = 1")


# --- SRS §10.1 acceptance matrix (composition only) --------------------------


def _composition(connection: sqlite3.Connection) -> list[list[int]]:
    """Bucket compositions as event-time seconds (SRS §10.1 rows are in seconds)."""
    compositions: list[list[int]] = []
    for bucket in _buckets(connection):
        rows = connection.execute(
            "SELECT m.event_timestamp FROM bucket_message bm"
            " JOIN source_message m ON m.id = bm.message_id"
            " WHERE bm.bucket_id = ? ORDER BY m.event_timestamp, m.id",
            (bucket["id"],),
        ).fetchall()
        compositions.append(
            [int((parse(row["event_timestamp"]) - _BASE).total_seconds()) for row in rows]
        )
    return compositions


def _pred(connection: sqlite3.Connection, bucket_id: int) -> int | None:
    row = connection.execute(
        "SELECT predecessor_bucket_id FROM bucket WHERE id = ?", (bucket_id,)
    ).fetchone()
    assert row is not None
    value = row["predecessor_bucket_id"]
    return None if value is None else int(value)


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


@pytest.mark.parametrize(
    ("times", "expected"),
    [
        ([0, 3, 8], [[0, 3, 8]]),
        ([0, 10], [[0, 10]]),
        ([0, 12], [[0, 12]]),
        ([0, 4, 8, 12, 16], [[0, 4, 8, 12, 16]]),  # 16 >= W joins on burst
        ([0, 14, 19], [[0, 14], [19]]),
        (
            [0, 4, 8, 12, 16, 20, 24, 28, 30],
            [[0, 4, 8, 12, 16, 20, 24, 28], [30]],
        ),
        (
            list(range(0, 61, 4)),
            [list(range(0, 29, 4)), list(range(32, 61, 4))],
        ),
        ([0], [[0]]),
        ([0, 4, 9, 13], [[0, 4, 9, 13]]),
        ([0], [[0]]),
        ([5], [[5]]),
        ([0], [[0]]),
    ],
)
def test_srs_101_composition_rows(
    conn: sqlite3.Connection, times: list[int], expected: list[list[int]]
) -> None:
    _group(conn)
    mid = 0
    for seconds in times:
        mid += 1
        _message(conn, mid, seconds)
    _assign(conn)
    assert _composition(conn) == expected
    for bucket in _buckets(conn):
        assert bucket["processing_state"] == "OPEN"
        assert bucket["end_event_timestamp"] is None
    _fk_clean(conn)


def test_srs_101_row5_0_17_two_buckets(conn: sqlite3.Connection) -> None:
    """SRS 10.1 row 5: times 0 and 17 form two buckets (17 >= W, gap 17 >= G)."""
    _group(conn)
    _message(conn, 1, 0)
    _message(conn, 2, 17)
    _assign(conn)
    assert _composition(conn) == [[0], [17]]
    assert len(_buckets(conn)) == 2
    _fk_clean(conn)


def test_srs_101_two_groups_interleaved(conn: sqlite3.Connection) -> None:
    _group(conn, 1)
    _group(conn, 2)
    _message(conn, 1, 0, group_id=1)
    _message(conn, 2, 1, group_id=2)
    _message(conn, 3, 3, group_id=1)
    _message(conn, 4, 5, group_id=2)
    _message(conn, 5, 8, group_id=1)
    _message(conn, 6, 9, group_id=2)
    _assign(conn)
    assert _members(conn, 1) == [1, 3, 5]
    assert _members(conn, 2) == [2, 4, 6]
    _fk_clean(conn)


def test_srs_101_duplicate_delivery_is_one_record(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _assign(conn)
    _assign(conn)
    assert _composition(conn) == [[0]]
    assert len(_buckets(conn)) == 1


def test_srs_101_message_after_closure(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _assign(conn)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(17))
    assert _state(conn, 1) == "CLOSED"
    _message(conn, 2, 5)
    result = _assign(conn)
    assert result.late_message_ids == (2,)
    assert _flag_late(conn, 2) == 1
    composition = _composition(conn)
    assert composition == [[0], [5]]
    bucket = _buckets(conn)[1]
    # PB-7: the closed bucket's end (15) is after t=5, so no closed bucket
    # qualifies as the one-hop predecessor; the old graph link is gone.
    assert bucket["predecessor_bucket_id"] is None
    assert _state(conn, 2) == "OPEN"
    _fk_clean(conn)


# --- closure through explicit progress ---------------------------------------


def test_closure_inclusive_boundary_before_at_after(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _assign(conn)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(16))
    assert _state(conn, 1) == "OPEN"
    mark_stream_progress(conn, group_id=1, event_progress=_iso(17))
    assert _state(conn, 1) == "CLOSED"
    assert _end(conn, 1) == _iso(15)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(18))
    assert _state(conn, 1) == "CLOSED"


def test_closure_row11_threshold(conn: sqlite3.Connection) -> None:
    """SRS 10.1 row 11: 0,4,8,12,16 -> end 21, closes at 23 (inclusive)."""
    _group(conn)
    for mid, seconds in enumerate([0, 4, 8, 12, 16], start=1):
        _message(conn, mid, seconds)
    _assign(conn)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(22))
    assert _state(conn, 1) == "OPEN"
    mark_stream_progress(conn, group_id=1, event_progress=_iso(23))
    assert _state(conn, 1) == "CLOSED"
    assert _end(conn, 1) == _iso(21)


def test_progress_fails_clearly_on_unusable_persisted_params(
    conn: sqlite3.Connection,
) -> None:
    """M1 follow-up: a bucket whose parameters_used is missing or malformed
    cannot be closed by guessing — the call fails loudly and the whole
    transaction (progress write included) rolls back."""
    from backend.bucketing import BucketParamsError

    _group(conn)
    _message(conn, 1, 0)
    _assign(conn)
    conn.execute("UPDATE bucket SET parameters_used = NULL WHERE id = 1")
    with pytest.raises(BucketParamsError):
        mark_stream_progress(conn, group_id=1, event_progress=_iso(50))
    assert _state(conn, 1) == "OPEN"  # nothing closed
    assert _progress_row(conn) is None  # progress write rolled back too
    conn.execute("UPDATE bucket SET parameters_used = 'not-json' WHERE id = 1")
    with pytest.raises(BucketParamsError):
        mark_stream_progress(conn, group_id=1, event_progress=_iso(50))
    assert _state(conn, 1) == "OPEN"
    assert _progress_row(conn) is None
    _fk_clean(conn)


def test_closure_uses_bucket_persisted_params(conn: sqlite3.Connection) -> None:
    """Review fix M1: closure thresholds come from each bucket's recorded
    parameters_used (§4.1), never from the progress call's parameters."""
    import json

    _group(conn)
    _message(conn, 1, 5)
    _assign(conn, params=BucketParams(window_w=20, close_grace_c=3))  # bucket A: e = 5+20 = 25
    _message(conn, 2, 0)  # earlier than A's start -> late; born L(0) OPEN, e = 15
    _assign(conn)
    buckets = _buckets(conn)
    bucket_late = buckets[0]  # start 0, recorded default parameters
    bucket_custom = buckets[1]  # start 5, recorded window_w=20
    assert json.loads(bucket_custom["parameters_used"])["window_w"] == 20
    mark_stream_progress(conn, group_id=1, event_progress=_iso(12))
    assert _state(conn, bucket_late["id"]) == "OPEN"  # own e=15, 15+2=17 > 12
    assert _state(conn, bucket_custom["id"]) == "OPEN"
    result17 = mark_stream_progress(conn, group_id=1, event_progress=_iso(17))
    assert sorted(result17.closed_bucket_ids) == [bucket_late["id"]]
    assert _state(conn, bucket_late["id"]) == "CLOSED"  # 17 <= 17 inclusive
    # 27 is at/behind the caller-default threshold 15+2=17 for bucket A, so a
    # closure computed from the progress call's parameters would close it here.
    mark_stream_progress(conn, group_id=1, event_progress=_iso(27))
    assert _state(conn, bucket_custom["id"]) == "OPEN"  # own 25+3=28 > 27
    mark_stream_progress(conn, group_id=1, event_progress=_iso(28))
    assert _state(conn, bucket_custom["id"]) == "CLOSED"  # 28 <= 28 inclusive
    assert _end(conn, bucket_custom["id"]) == _iso(25)


# --- FR-BKT-04 strict boundaries ---------------------------------------------


@pytest.mark.parametrize(
    ("times", "joins"),
    [
        ([0, 14], True),  # 14 < W joins
        ([0, 15], False),  # exactly W never joins (SRS 10.1 row 3)
        ([20, 24], True),  # 4 < W joins
        ([20, 25], True),  # exactly G inside the base window: W branch joins
        ([0, 12, 17], False),  # exactly G outside the base window never joins
        # Exactly M after the start never joins (30 - 0 == M), even though
        # 30 - 28 = 1 < G: the M condition is not part of the OR.
        ([0, 4, 8, 12, 16, 20, 24, 28, 30], False),
    ],
)
def test_join_boundaries_strict(
    conn: sqlite3.Connection, times: list[int], joins: bool
) -> None:
    _group(conn)
    for mid, seconds in enumerate(times, start=1):
        _message(conn, mid, seconds)
    _assign(conn)
    assert len(_buckets(conn)) == (1 if joins else 2)


# --- late classification and current selection -------------------------------


def test_out_of_order_without_progress_is_late(conn: sqlite3.Connection) -> None:
    """Binding decision 2: late iff t < current OPEN bucket start when one
    exists, even without accepted progress (cross-call order sensitivity,
    pending amendment P5). Mirror image of the §10.1/P5 counterexample."""
    _group(conn)
    _message(conn, 1, 10)
    _assign(conn)
    _message(conn, 2, 5)
    result = _assign(conn)
    assert result.late_message_ids == (2,)
    assert _flag_late(conn, 2) == 1


def test_progress_at_boundary_is_late(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, 20)
    _assign(conn)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(20))
    _message(conn, 2, 20)
    result = _assign(conn)
    assert result.late_message_ids == (2,)


def test_blocker1_scenario_late_open_then_normal(conn: sqlite3.Connection) -> None:
    """Existing OPEN bucket B(18); progress closes A; late 12 creates OPEN L;
    normal 25 evaluates against B (current), not L; progress 29 closes L."""
    _group(conn)
    for mid, seconds in enumerate([0, 4, 9, 13, 18], start=1):
        _message(conn, mid, seconds)
    _assign(conn)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(21))
    buckets = _buckets(conn)
    assert _state(conn, buckets[0]["id"]) == "CLOSED"  # A(0..13), e=18, 18+2<=21
    assert _state(conn, buckets[1]["id"]) == "OPEN"  # B(18)
    _message(conn, 6, 12)
    result = _assign(conn)
    late_bucket_id = result.assigned[0][1]
    assert result.late_message_ids == (6,)
    assert _state(conn, late_bucket_id) == "OPEN"  # e=27, 27+2=29 > 21
    _message(conn, 7, 25)
    result2 = _assign(conn)
    normal_bucket_id = result2.assigned[0][1]
    assert normal_bucket_id != late_bucket_id  # joins B(18), not L(12)
    assert _members(conn, normal_bucket_id) == [5, 7]
    mark_stream_progress(conn, group_id=1, event_progress=_iso(29))
    assert _state(conn, late_bucket_id) == "CLOSED"
    assert _state(conn, normal_bucket_id) == "OPEN"  # e=33, 33+2=35 > 29


def test_equal_start_tie_current_replacement(conn: sqlite3.Connection) -> None:
    """Late bucket with the same start as the current OPEN bucket: greater id
    wins the tie and the new bucket becomes current while OPEN."""
    _group(conn)
    _message(conn, 1, 10)
    _assign(conn)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(12))
    original = _buckets(conn)[0]["id"]
    assert _state(conn, original) == "OPEN"  # e=25, e+C=27 > 12
    _message(conn, 2, 10)
    result = _assign(conn)
    late_bucket = result.assigned[0][1]
    assert result.late_message_ids == (2,)
    assert _flag_late(conn, 2) == 1
    assert _state(conn, late_bucket) == "OPEN"  # born e=25, 27 > 12
    assert late_bucket != original
    _message(conn, 3, 13)
    result2 = _assign(conn)
    normal_bucket = result2.assigned[0][1]
    assert normal_bucket == late_bucket  # tie winner (start 10, greater id)
    assert _members(conn, normal_bucket) == [2, 3]
    assert _members(conn, original) == [1]  # displaced bucket receives nothing
    _fk_clean(conn)


def test_born_closed_late_bucket(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, 10)
    _assign(conn)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(27))
    assert _state(conn, 1) == "CLOSED"  # e=25, 25+2<=27
    _message(conn, 2, 10)
    result = _assign(conn)
    born_id = result.assigned[0][1]
    assert born_id in result.born_closed_bucket_ids
    assert _state(conn, born_id) == "CLOSED"
    assert _end(conn, born_id) == _iso(25)
    _message(conn, 3, 30)  # normal (30 > 27), cannot join closed buckets
    _assign(conn)
    assert _composition(conn) == [[10], [10], [30]]
    _fk_clean(conn)


def test_born_open_late_bucket_absorbs_normal(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, 5)
    _assign(conn)  # bucket X(5) OPEN, e=20, closes at 22
    mark_stream_progress(conn, group_id=1, event_progress=_iso(15))
    assert _state(conn, 1) == "OPEN"
    _message(conn, 2, 9)  # 9 <= 15 -> late; e=24, 24+2=26 > 15 -> born OPEN, displaces
    _assign(conn)
    _message(conn, 3, 16)  # 16 > 15 normal; 16-9=7 < 15 -> joins the late bucket
    result = _assign(conn)
    bucket_id = result.assigned[0][1]
    assert _members(conn, bucket_id) == [2, 3]
    _fk_clean(conn)


# --- order independence -------------------------------------------------------


@pytest.mark.parametrize(
    "times",
    [[0, 3, 8], [0, 10], [0, 12], [0, 4, 9, 13], [0, 9, 14], list(range(0, 61, 4))],
)
def test_within_call_arrival_permutations(
    conn: sqlite3.Connection, times: list[int]
) -> None:
    _group(conn)
    import random

    rng = random.Random(7)
    mid_to_seconds = {mid + 1: seconds for mid, seconds in enumerate(times)}
    for message_id, seconds in mid_to_seconds.items():
        _message(conn, message_id, seconds)
    _assign(conn)
    baseline = _composition(conn)
    connection2 = connect(":memory:")
    apply_migrations(connection2)
    _group(connection2)
    shuffled = list(mid_to_seconds.items())
    rng.shuffle(shuffled)
    for message_id, seconds in shuffled:
        _message(connection2, message_id, seconds)
    _assign(connection2)
    assert _composition(connection2) == baseline
    connection2.close()


def test_cross_call_out_of_order_counterexample(conn: sqlite3.Connection) -> None:
    """Documented P5 exception: {15} then {10} composes differently from the
    same set assigned in one call."""
    _group(conn)
    _message(conn, 1, 15)
    _assign(conn)
    _message(conn, 2, 10)
    result = _assign(conn)
    assert result.late_message_ids == (2,)
    assert _composition(conn) == [[10], [15]]
    assert _flag_late(conn, 2) == 1

    connection2 = connect(":memory:")
    apply_migrations(connection2)
    _group(connection2)
    _message(connection2, 1, 10)
    _message(connection2, 2, 15)
    _assign(connection2)
    assert _composition(connection2) == [[10, 15]]
    connection2.close()


def test_cross_call_monotone_matches_single_call(conn: sqlite3.Connection) -> None:
    """Monotone cross-call arrival composes like one call (tier-2 guarantee)."""
    _group(conn)
    for mid, seconds in enumerate([0, 4], start=1):
        _message(conn, mid, seconds)
    _assign(conn)
    for mid, seconds in enumerate([9, 13], start=3):
        _message(conn, mid, seconds)
    result = _assign(conn)
    assert result.late_message_ids == ()
    assert _composition(conn) == [[0, 4, 9, 13]]

    connection2 = connect(":memory:")
    apply_migrations(connection2)
    _group(connection2)
    for mid, seconds in enumerate([0, 4, 9, 13], start=1):
        _message(connection2, mid, seconds)
    _assign(connection2)
    assert _composition(connection2) == [[0, 4, 9, 13]]
    connection2.close()


# --- predecessors -------------------------------------------------------------


def test_late_predecessor_is_one_hop_to_the_last_closed_bucket(
    conn: sqlite3.Connection,
) -> None:
    """PB-7: only a late bucket gets a link, and it is one hop to the latest
    CLOSED bucket whose end is at or before the message time."""
    _group(conn)
    _message(conn, 1, 10)
    _assign(conn)
    _message(conn, 2, 26)
    _assign(conn)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(43))
    # {10}: e=25 (closes 27); {26}: e=41 (closes 43); both closed at 43.
    buckets = _buckets(conn)
    bucket_a, bucket_b = buckets[0], buckets[1]
    assert [_state(conn, b["id"]) for b in buckets] == ["CLOSED", "CLOSED"]
    assert _pred(conn, bucket_a["id"]) is None
    assert _pred(conn, bucket_b["id"]) is None  # normal bucket: no link
    _message(conn, 3, 25)  # late; latest closed end <= 25 is A (end 20)
    result = _assign(conn)
    late25 = result.assigned[0][1]
    assert _pred(conn, late25) == bucket_a["id"]
    _message(conn, 4, 14)  # late; no closed end <= 14 -> NULL
    result2 = _assign(conn)
    late14 = result2.assigned[0][1]
    assert _pred(conn, late14) is None
    # Every recorded link is one hop and points at an earlier bucket.
    assert late14 > late25 > bucket_b["id"] > bucket_a["id"]
    _fk_clean(conn)


def test_normal_buckets_have_no_predecessor(conn: sqlite3.Connection) -> None:
    """PB-7: a bucket that is not late has no link (buckets are not a chain)."""
    _group(conn)
    seconds = [0, 15, 30, 45, 60, 90]
    for mid, s in enumerate(seconds, start=1):
        _message(conn, mid, s)
    _assign(conn)
    assert [bucket["predecessor_bucket_id"] for bucket in _buckets(conn)] == [
        None,
        None,
        None,
        None,
        None,
        None,
    ]
    _fk_clean(conn)


def test_late_message_before_every_closed_end_links_null(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, 10)
    _assign(conn)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(23))
    _message(conn, 2, 5)  # earlier than every closed end -> no link
    result = _assign(conn)
    late_bucket = result.assigned[0][1]
    assert result.late_message_ids == (2,)
    # PB-7 removed the recovery fallback chain: no qualified closed bucket
    # means NULL, never a multi-hop substitute.
    assert _pred(conn, late_bucket) is None
    assert late_bucket > 1
    _fk_clean(conn)


# --- progress semantics -------------------------------------------------------


def test_progress_regression_is_noop(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _assign(conn)
    first = mark_stream_progress(conn, group_id=1, event_progress=_iso(30))
    assert first.accepted_progress == _iso(30)
    row_before = _progress_row(conn)
    second = mark_stream_progress(conn, group_id=1, event_progress=_iso(29))
    assert second.accepted is False
    assert second.regressed is True
    assert _progress_row(conn) == row_before
    equal = mark_stream_progress(conn, group_id=1, event_progress=_iso(30))
    assert equal.accepted is False


def test_progress_invalid_raises(conn: sqlite3.Connection) -> None:
    _group(conn)
    with pytest.raises(ValueError):
        mark_stream_progress(conn, group_id=1, event_progress="not-a-time")
    with pytest.raises(ValueError):
        mark_stream_progress(conn, group_id=1, event_progress="2026-01-01T00:00:00")
    assert _progress_row(conn) is None


def test_progress_restart_persistence(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, 22)
    _assign(conn)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(33))
    second_connection = connect(":memory:")
    apply_migrations(second_connection)
    # fresh in-memory db has no data; exercise persistence through a file db
    second_connection.close()


def test_progress_restart_persistence_file_db(
    tmp_path: Any, monkeypatch: Any
) -> None:
    db_path = tmp_path / "bucketing.db"
    first = connect(db_path)
    apply_migrations(first)
    _group(first)
    _message(first, 1, 22)
    _assign(first)
    mark_stream_progress(first, group_id=1, event_progress=_iso(33))
    first.close()
    second = connect(db_path)  # reopen: persisted progress must gate lateness
    _message(second, 2, 33)  # t == P -> late
    result = _assign(second)
    assert result.late_message_ids == (2,)
    second.close()


def test_progress_group_isolation(conn: sqlite3.Connection) -> None:
    _group(conn, 1)
    _group(conn, 2)
    _message(conn, 1, 0, group_id=1)
    _message(conn, 2, 0, group_id=2)
    _assign(conn)
    mark_stream_progress(conn, group_id=1, event_progress=_iso(17))
    assert _state(conn, 1) == "CLOSED"
    assert _state(conn, 2) == "OPEN"


# --- idempotence, duplicates, params -----------------------------------------


def test_repeat_assignment_idempotent(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _message(conn, 2, 20)
    _assign(conn)
    result = _assign(conn)
    assert result.assigned == ()
    assert result.skipped == ()
    assert len(_buckets(conn)) == 2


def test_parameters_used_recorded(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _assign(conn)
    import json

    stored = json.loads(_buckets(conn)[0]["parameters_used"])
    assert stored == {
        "window_w": 15,
        "burst_gap_g": 5,
        "max_duration_m": 30,
        "close_grace_c": 2,
    }


def test_custom_params(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _message(conn, 2, 10)
    _assign(conn, params=BucketParams(window_w=15, close_grace_c=3))
    assert len(_buckets(conn)) == 1  # 10 < 15 joins with the widened window


# --- skipped messages ---------------------------------------------------------


def test_invalid_timestamps_isolated(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, None)  # missing
    _message(conn, 2, 0)  # corrupted below -> malformed
    conn.execute(
        "UPDATE source_message SET event_timestamp = 'nonsense' WHERE id = 2"
    )
    _message(conn, 3, None)
    conn.execute(
        "UPDATE source_message SET event_timestamp = '2026-01-01T00:00:09' WHERE id = 3"
    )  # timezone-naive
    _message(conn, 4, 12)
    result = _assign(conn)
    reasons = {skipped.message_id: skipped.reason for skipped in result.skipped}
    assert reasons == {
        1: SkipReason.MISSING_EVENT_TIME,
        2: SkipReason.MALFORMED_EVENT_TIME,
        3: SkipReason.NAIVE_EVENT_TIME,
    }
    assert sorted(mid for mid, _ in result.assigned) == [4]


def test_skipped_message_source_untouched(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, None, content="keep me")
    _assign(conn)
    row = conn.execute(
        "SELECT content, flag_late FROM source_message WHERE id = 1"
    ).fetchone()
    assert row["content"] == "keep me"
    assert row["flag_late"] == 0


# --- PB-2 effective time basis (FR-ING-02, FR-BKT-01, 0019) -------------------


def test_event_time_records_event_basis(conn: sqlite3.Connection) -> None:
    """A usable event time is the bucket time and is recorded, never rewritten."""
    _group(conn)
    _message(conn, 1, 12)
    result = _assign(conn)
    assert result.skipped == ()
    assert [message_id for message_id, _ in result.assigned] == [1]
    assert _time_basis(conn, 1) == "EVENT"
    assert _stored_event(conn, 1) == _iso(12)
    assert _buckets(conn)[0]["start_event_timestamp"] == _iso(12)


@pytest.mark.parametrize(
    "stored_event",
    [
        None,  # missing event time
        "nonsense",  # malformed event time
        "2026-01-01T00:00:09",  # timezone-naive event time
    ],
)
def test_unusable_event_time_uses_receipt_time(
    conn: sqlite3.Connection, stored_event: str | None
) -> None:
    """PB-2: receipt fallback places the message, marks RECEIPT and reports it.

    The stored event timestamp is preserved exactly as supplied (FR-ING-02,
    DR-03) and the message is not silently skipped (FR-BKT-01).
    """
    _group(conn)
    _message(conn, 1, 0, received_at=_iso(7))
    conn.execute(
        "UPDATE source_message SET event_timestamp = ? WHERE id = 1", (stored_event,)
    )
    result = _assign(conn)
    assert [message_id for message_id, _ in result.assigned] == [1]
    assert _time_basis(conn, 1) == "RECEIPT"
    assert _stored_event(conn, 1) == stored_event
    assert result.skipped == (
        SkippedMessage(message_id=1, reason=SkipReason.EVENT_TIME_UNUSABLE),
    )
    assert _buckets(conn)[0]["start_event_timestamp"] == _iso(7)


def test_unusable_event_and_receipt_stays_unassigned(
    conn: sqlite3.Connection,
) -> None:
    """A message with neither usable time is skipped, visible and unassigned."""
    _group(conn)
    _message(conn, 1, None, received_at="not-a-time")
    result = _assign(conn)
    assert result.assigned == ()
    assert result.skipped == (
        SkippedMessage(message_id=1, reason=SkipReason.MISSING_EVENT_TIME),
    )
    assert _time_basis(conn, 1) is None


def test_receipt_fallback_orders_by_effective_time(
    conn: sqlite3.Connection,
) -> None:
    """FR-BKT-03 is applied to the selected effective time, not the raw column."""
    _group(conn)
    _message(conn, 1, 20)  # EVENT time 20
    _message(conn, 2, None, received_at=_iso(0))  # RECEIPT time 0
    _assign(conn)
    starts = [bucket["start_event_timestamp"] for bucket in _buckets(conn)]
    assert starts == [_iso(0), _iso(20)]
    assert _members(conn, _buckets(conn)[0]["id"]) == [2]
    assert _members(conn, _buckets(conn)[1]["id"]) == [1]


def test_unusable_receipt_does_not_affect_a_usable_event(
    conn: sqlite3.Connection,
) -> None:
    """The receipt column is only consulted when the event time is unusable."""
    _group(conn)
    _message(conn, 1, 3, received_at="not-a-time")
    result = _assign(conn)
    assert result.skipped == ()
    assert _time_basis(conn, 1) == "EVENT"
    assert _buckets(conn)[0]["start_event_timestamp"] == _iso(3)


# --- concurrency and rollback --------------------------------------------------


class _FailProxy:
    """Connection proxy that raises a scheduled IntegrityError mid-transaction."""

    def __init__(self, target: sqlite3.Connection, sql_prefix: str, occurrence: int):
        self._target = target
        self._prefix = sql_prefix
        self._occurrence = occurrence
        self._seen = 0

    def execute(self, sql: str, parameters: Any = ()) -> Any:
        if sql.strip().startswith(self._prefix):
            self._seen += 1
            if self._seen >= self._occurrence:
                raise sqlite3.IntegrityError("injected failure")
        return self._target.execute(sql, parameters)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


def test_injected_failure_mid_transaction_rolls_back(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _message(conn, 2, 20)
    conn.execute("UPDATE source_message SET event_timestamp = ? WHERE id = 1", (_iso(0),))
    proxy = _FailProxy(conn, "INSERT INTO bucket (", 2)
    # 0 and 20 form two buckets under the §10.1 parameter set; the failure is
    # injected on the second bucket insert.
    with pytest.raises(sqlite3.IntegrityError):
        assign_messages(proxy, group_id=1, params=_PARAMS_101)
    # nothing committed: no buckets, no memberships, no flags, no progress
    assert _buckets(conn) == []
    assert _composition(conn) == []
    assert _flag_late(conn, 2) == 0
    assert _progress_row(conn) is None
    _fk_clean(conn)
    # a clean retry succeeds fully
    _assign(conn)
    assert len(_buckets(conn)) == 2


def test_injected_failure_progress_rolls_back(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _assign(conn)
    proxy = _FailProxy(conn, "UPDATE bucket", 1)
    with pytest.raises(sqlite3.IntegrityError):
        mark_stream_progress(proxy, group_id=1, event_progress=_iso(50))
    assert _state(conn, 1) == "OPEN"
    assert _progress_row(conn) is None
    retry = mark_stream_progress(conn, group_id=1, event_progress=_iso(50))
    assert retry.accepted is True
    assert _state(conn, 1) == "CLOSED"


def test_real_lock_contention_assign_blocked(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """Two connections on one file database: a held write transaction blocks
    assignment with a real SQLite lock error; retry succeeds after release."""
    monkeypatch.setattr("backend.db.connection._BUSY_TIMEOUT_MS", 50)
    db_path = tmp_path / "lock.db"
    holder = connect(db_path)
    apply_migrations(holder)
    _group(holder)
    _message(holder, 1, 0)
    _message(holder, 2, 20)
    holder.execute("BEGIN IMMEDIATE")  # holds the single write lock
    holder.execute("INSERT INTO source_group (id, discord_channel_id) VALUES (9, 'h')")
    worker = connect(db_path)
    with pytest.raises(sqlite3.OperationalError):
        _assign(worker)
    holder.execute("ROLLBACK")
    holder.close()
    result = _assign(worker)
    assert result.late_message_ids == ()
    assert _composition(worker) == [[0], [20]]
    _fk_clean(worker)
    worker.close()
    holder.close()


def test_progress_and_assignment_concurrent_group_isolation(
    conn: sqlite3.Connection,
) -> None:
    _group(conn, 1)
    _group(conn, 2)
    _message(conn, 1, 0, group_id=1)
    _message(conn, 2, 0, group_id=2)
    _assign(conn)
    # sequential calls on separate connections stay consistent; the
    # transaction boundaries serialize writers.
    result = mark_stream_progress(conn, group_id=2, event_progress=_iso(17))
    assert result.accepted
    assert result.closed_bucket_ids == (2,)  # g2 bucket: e=15, e+C=17 <= P=17
    _message(conn, 3, 15, group_id=2)
    late = _assign(conn, group_id=2)
    assert late.late_message_ids == (3,)
    assert _pred(conn, late.assigned[0][1]) == 2  # closed end 15 <= t=15
    assert _state(conn, 2) == "CLOSED"  # inclusive e + C <= P closure stands
    assert _state(conn, 1) == "OPEN"  # group 1's bucket unaffected throughout
    _fk_clean(conn)


# --- SRS §3.2 production defaults and configuration (FR-BKT §3.2, NFR-MNT-01) ---


def test_bucket_params_defaults_are_srs_31() -> None:
    """§3.2: W=15, G=5, M=30, C=2 (production), not the §10.1 test set."""
    params = BucketParams()
    assert params.window_w == 15
    assert params.burst_gap_g == 5
    assert params.max_duration_m == 30
    assert params.close_grace_c == 2
    import json

    assert json.loads(params.as_json()) == {
        "window_w": 15,
        "burst_gap_g": 5,
        "max_duration_m": 30,
        "close_grace_c": 2,
    }


def test_production_defaults_single_message_at_0_closes_at_17(
    conn: sqlite3.Connection,
) -> None:
    """§3.2 defaults: single message at 0 -> end 15, closes at 17 (inclusive)."""
    _group(conn)
    _message(conn, 1, 0)
    result = assign_messages(conn, group_id=1)  # real §3.2 defaults
    assert result.skipped == ()
    # For a normal bucket the computed end is written only at closure
    # (FR-BKT-05); assignment does not store it.
    assert _end(conn, 1) is None
    mark_stream_progress(conn, group_id=1, event_progress=_iso(16))
    assert _state(conn, 1) == "OPEN"  # 15 + 2 = 17 > 16
    closed = mark_stream_progress(conn, group_id=1, event_progress=_iso(17))
    assert _state(conn, 1) == "CLOSED"  # 17 <= 17 inclusive
    assert _end(conn, 1) == _iso(15)
    assert closed.closed_bucket_ids == (1,)
    _fk_clean(conn)


def test_load_bucket_params_defaults_when_unset() -> None:
    assert load_bucket_params(Config({})) == BucketParams(15, 5, 30, 2)


def test_load_bucket_params_reads_overrides() -> None:
    config = Config(
        {
            "BUCKET_WINDOW_W_S": "20",
            "BUCKET_BURST_GAP_G_S": "7",
            "BUCKET_MAX_DURATION_M_S": "60",
            "BUCKET_CLOSE_GRACE_C_S": "4",
        }
    )
    assert load_bucket_params(config) == BucketParams(20, 7, 60, 4)


def test_load_bucket_params_partial_overrides_keep_defaults() -> None:
    config = Config({"BUCKET_CLOSE_GRACE_C_S": "9"})
    assert load_bucket_params(config) == BucketParams(15, 5, 30, 9)


def test_load_bucket_params_blank_counts_as_unset() -> None:
    config = Config({"BUCKET_WINDOW_W_S": "   "})
    assert load_bucket_params(config) == BucketParams(15, 5, 30, 2)


@pytest.mark.parametrize("raw", ["abc", "3.5", "0", "-2", "15;20"])
def test_load_bucket_params_invalid_values_fail_loudly(raw: str) -> None:
    config = Config({"BUCKET_WINDOW_W_S": raw})
    with pytest.raises(BucketParamsError):
        load_bucket_params(config)


def test_bucket_config_keys_documented_in_env_example() -> None:
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    text = env_example.read_text(encoding="utf-8")
    for key in _BUCKET_CONFIG_KEYS:
        assert key in text, f"{key} must be documented in .env.example"
