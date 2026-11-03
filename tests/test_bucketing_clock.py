"""Clock-driven assignment tests (PB-1/PB-7; FR-BKT-04/05/06/08, BR-05).

Covers settle-then-assign at the ``t + C <= clock`` boundary, arrival-order
independence inside the settle window, closure remaining a clock-only action,
and the one-hop-or-NULL predecessor rule for late buckets.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.bucketing import AssignmentResult, BucketParams, assign_messages
from backend.clock import advance_group_clock
from backend.db.connection import connect
from backend.db.migrations import apply_migrations

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_PARAMS = BucketParams(10, 5, 30, 3)  # SRS §10.1 test set: C=3


def _iso(seconds: int) -> str:
    return (_BASE + timedelta(seconds=seconds)).isoformat()


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "bucketing-clock.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _group(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (1, 'chan-1')"
    )


def _message(connection: sqlite3.Connection, message_id: int, seconds: int) -> None:
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " event_timestamp, received_at, content)"
        " VALUES (?, 'discord', ?, 1, ?, ?, 'content')",
        (message_id, f"m-{message_id}", _iso(seconds), _iso(seconds)),
    )


def _advance(connection: sqlite3.Connection, seconds: int) -> None:
    advance_group_clock(connection, 1, now=_iso(seconds), mode="LIVE")


def _assign(connection: sqlite3.Connection) -> AssignmentResult:
    return assign_messages(connection, group_id=1, params=_PARAMS)


def _buckets(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT id, predecessor_bucket_id, start_event_timestamp,"
        " end_event_timestamp, processing_state FROM bucket"
        " ORDER BY start_event_timestamp, id"
    ).fetchall()


def _composition(connection: sqlite3.Connection) -> list[list[int]]:
    buckets = _buckets(connection)
    result: list[list[int]] = []
    for bucket in buckets:
        rows = connection.execute(
            "SELECT message_id FROM bucket_message WHERE bucket_id = ?"
            " ORDER BY message_id",
            (bucket["id"],),
        ).fetchall()
        result.append([int(row["message_id"]) for row in rows])
    return result


def _state(connection: sqlite3.Connection, bucket_id: int) -> str:
    row = connection.execute(
        "SELECT processing_state FROM bucket WHERE id = ?", (bucket_id,)
    ).fetchone()
    assert row is not None
    return str(row["processing_state"])


def _pred(connection: sqlite3.Connection, bucket_id: int) -> int | None:
    row = connection.execute(
        "SELECT predecessor_bucket_id FROM bucket WHERE id = ?", (bucket_id,)
    ).fetchone()
    assert row is not None
    value = row["predecessor_bucket_id"]
    return None if value is None else int(value)


def _book(connection: sqlite3.Connection) -> dict[int, int]:
    """Map each message id to the id of the bucket that holds it."""
    rows = connection.execute(
        "SELECT message_id, bucket_id FROM bucket_message ORDER BY message_id"
    ).fetchall()
    return {int(row["message_id"]): int(row["bucket_id"]) for row in rows}


# --- settle-then-assign -------------------------------------------------------


def test_unsettled_messages_wait_for_the_next_tick(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _message(conn, 2, 4)
    _advance(conn, 2)  # neither t + 3 <= 2
    assert _assign(conn).assigned == ()
    _advance(conn, 3)  # only t=0 is settled
    assert _assign(conn).assigned == ((1, 1),)
    _advance(conn, 7)  # t=4 settled
    result = _assign(conn)
    assert [message_id for message_id, _ in result.assigned] == [2]
    assert _composition(conn) == [[1, 2]]


def test_settle_boundary_is_inclusive_at_exactly_c(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, 5)
    _advance(conn, 7)  # 5 + 3 > 7
    assert _assign(conn).assigned == ()
    _advance(conn, 8)  # 5 + 3 == 8 -> settled
    assert _assign(conn).assigned == ((1, 1),)


def test_settled_assignment_is_arrival_order_independent(
    conn: sqlite3.Connection,
) -> None:
    """FR-BKT-06: within the settle window the composition cannot depend on order."""
    times = [0, 3, 8]
    baselines: list[list[list[int]]] = []
    for order in ([0, 1, 2], [2, 1, 0], [1, 0, 2]):
        connection = connect(":memory:")
        apply_migrations(connection)
        _group(connection)
        for index in order:
            _message(connection, index + 1, times[index])
        _advance(connection, 11)  # max t + C
        _assign(connection)
        baselines.append(_composition(connection))
        connection.close()
    assert baselines[0] == [[1, 2, 3]]
    assert baselines[1] == baselines[0]
    assert baselines[2] == baselines[0]


def test_two_arrivals_inside_the_grace_compose_together(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _advance(conn, 2)  # not settled yet
    _message(conn, 2, 3)  # arrives inside the grace
    _advance(conn, 6)  # both settled before either is assigned
    _assign(conn)
    assert _composition(conn) == [[1, 2]]


def test_assignment_never_closes_a_bucket(conn: sqlite3.Connection) -> None:
    """PB-1: closure stays in advance_group_clock, never in assignment."""
    _group(conn)
    _advance(conn, 30)
    _message(conn, 1, 0)
    _assign(conn)
    assert _state(conn, 1) == "OPEN"
    closed = advance_group_clock(conn, 1, now=_iso(30), mode="LIVE")
    assert closed.advanced is False
    assert closed.closed_bucket_ids == (1,)
    assert _state(conn, 1) == "CLOSED"


def test_replay_mode_applies_the_same_settle_rule(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    advance_group_clock(conn, 1, now=_iso(2), mode="REPLAY")
    _message(conn, 1, 0)
    assert _assign(conn).assigned == ()
    advance_group_clock(conn, 1, now=_iso(3), mode="REPLAY")
    assert _assign(conn).assigned == ((1, 1),)


def test_assignment_without_a_clock_keeps_the_legacy_path(
    conn: sqlite3.Connection,
) -> None:
    """Documented compatibility: no clock row means no settle window.

    A group that has never been clocked (direct Phase 2/3 service calls and
    pre-clock databases) keeps the pre-PB-1 assignment contract. The live
    pipeline always advances the clock before assigning (W3 tick), so no
    message in the live path skips the settle window.
    """
    _group(conn)
    _message(conn, 1, 0)
    _message(conn, 2, 3)
    result = _assign(conn)
    assert [message_id for message_id, _ in result.assigned] == [1, 2]
    assert _composition(conn) == [[1, 2]]


# --- PB-7 late one-hop predecessor links --------------------------------------


def test_late_message_gets_a_one_hop_link_to_the_closed_bucket(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, 0)  # A: end 10
    _message(conn, 2, 10)  # B: start 10, end 20 (10-0 == W, no join)
    _advance(conn, 13)
    _assign(conn)
    _advance(conn, 23)  # closes A at 13 and B at 23
    assert [_state(conn, b["id"]) for b in _buckets(conn)] == ["CLOSED", "CLOSED"]
    _message(conn, 3, 15)  # settled at 18; would have joined closed B
    result = _assign(conn)
    late_id = result.assigned[0][1]
    assert result.late_message_ids == (3,)
    # Latest closed bucket whose end <= 15 is A (end 10), never B (end 20).
    assert _pred(conn, late_id) == 1
    assert _pred(conn, late_id) != 2
    assert _buckets(conn)[2]["start_event_timestamp"] == _iso(15)


def test_late_message_without_a_qualified_closed_bucket_links_null(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, 0)  # A: end 10
    _advance(conn, 3)
    _assign(conn)
    _advance(conn, 13)  # A closed
    _message(conn, 2, 5)  # late; A's end (10) is after the message time
    result = _assign(conn)
    late_id = result.assigned[0][1]
    assert result.late_message_ids == (2,)
    assert _pred(conn, late_id) is None


def test_normal_bucket_has_a_null_predecessor(conn: sqlite3.Connection) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _advance(conn, 3)
    _assign(conn)
    _advance(conn, 13)  # A closed
    _message(conn, 2, 25)  # beyond A's rules
    _advance(conn, 28)
    result = _assign(conn)
    assert result.late_message_ids == ()
    assert _pred(conn, result.assigned[0][1]) is None


def test_late_links_never_chain_through_a_late_bucket(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, 0)  # A: end 10
    _message(conn, 2, 10)  # B: end 20
    _advance(conn, 13)
    _assign(conn)
    _advance(conn, 23)  # both closed
    _message(conn, 3, 15)  # late C, predecessor A
    first = _assign(conn).assigned[0][1]
    _message(conn, 4, 14)  # late D, still before C's start
    second = _assign(conn).assigned[0][1]
    assert _pred(conn, first) == 1
    assert _pred(conn, second) == 1  # not chained to the OPEN late bucket C
    assert _pred(conn, second) != first


def test_late_bucket_can_be_born_closed_under_the_clock(
    conn: sqlite3.Connection,
) -> None:
    _group(conn)
    _message(conn, 1, 0)
    _advance(conn, 3)
    _assign(conn)
    _advance(conn, 30)  # A closed
    _message(conn, 2, 5)  # late; e = 15, e + 3 = 18 <= 30
    result = _assign(conn)
    late_id = result.assigned[0][1]
    assert late_id in result.born_closed_bucket_ids
    assert _state(conn, late_id) == "CLOSED"
    assert _buckets(conn)[1]["end_event_timestamp"] == _iso(15)


def test_receipt_time_basis_still_settles_and_links(
    conn: sqlite3.Connection,
) -> None:
    """PB-2 and PB-1 interact: the receipt time is the settled effective time."""
    _group(conn)
    _message(conn, 1, 0)
    _advance(conn, 3)
    _assign(conn)
    _advance(conn, 13)  # A closed
    conn.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " event_timestamp, received_at, content)"
        " VALUES (2, 'discord', 'm-2', 1, 'nonsense', ?, 'content')",
        (_iso(5),),
    )
    result = _assign(conn)
    late_id = result.assigned[0][1]
    assert result.late_message_ids == (2,)
    assert _book(conn)[2] == late_id
    basis = conn.execute(
        "SELECT time_basis FROM source_message WHERE id = 2"
    ).fetchone()[0]
    assert basis == "RECEIPT"
