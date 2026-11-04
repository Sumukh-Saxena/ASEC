"""Scheduler and worker loop for the ASEC pipeline (Phase 9 W3, PB-1).

This module is the only live driver of bucket formation. One tick does, in
order:

1. advance every source group's durable clock (PB-1). The clock write and the
   closure of every bucket whose own ``e + C`` is at or behind the clock happen
   in the same transaction (:mod:`backend.clock`);
2. run the bucket-formation pass. Assignment is settle-then-assign: only
   messages with ``t + C <= clock`` are considered, so the composition of a
   bucket cannot depend on arrival order (FR-BKT-06);
3. record the per-message ingestion-to-bucket stage timings in
   ``latency_measurement`` (NFR-PERF-02). Classifier and proposal latency rows
   are written by their own passes, not duplicated here;
4. optionally run the relevance pass and then the proposal pass, only when a
   model object is injected. This module does nothing AI-specific itself and
   imports no AI adapter.

The pipeline holds no in-memory work queue: every tick re-reads durable state,
so a crash between ticks is recoverable without loss or duplicates (NFR-REL-04)
and a repeated tick is idempotent. ``now`` is injected into
:func:`run_pipeline_tick`; only :func:`run_worker` (the process boundary) reads
the wall clock. The worker is a plain :class:`threading.Event` loop with a
configurable interval and exponential backoff after a failed tick (O9-3).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from backend.bucketing import (
    AssignmentResult,
    BucketParams,
    _TimestampError,
    parse_event_time,
)
from backend.clock import (
    CLOCK_MODES,
    ClockModeError,
    ClockResult,
    advance_group_clock,
)
from backend.db.connection import transaction
from backend.orchestration import run_bucketing_pass
from backend.proposals import ProposalGenerator, run_proposal_pass
from backend.relevance import RelevanceClassifier, run_relevance_pass

logger = logging.getLogger(__name__)

#: SRS §6.4 vocabulary, recorded as free-text metric labels (0011 defines no
#: token vocabulary). Ingestion delay is event time to receipt; bucket delay is
#: receipt to the tick's clock boundary that authorized assignment.
INGESTION_DELAY_METRIC = "ingestion delay"
BUCKET_DELAY_METRIC = "bucket delay"


class StopEvent(Protocol):
    """The part of :class:`threading.Event` the worker loop needs."""

    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


@dataclass(frozen=True)
class TickResult:
    """Everything one pipeline tick did or delegated.

    ``relevance`` is the :class:`backend.relevance.RelevancePassResult` and
    ``proposals`` the :class:`backend.proposals.ProposalPassResult` when the
    corresponding model object was injected; both stay ``None`` otherwise, so
    an unconfigured pipeline never pretends the AI stages ran.
    """

    groups: tuple[ClockResult, ...] = ()
    assigned: int = 0
    skipped: int = 0
    relevance: object | None = None
    proposals: object | None = None


def run_pipeline_tick(
    connection: sqlite3.Connection,
    *,
    now: str,
    clock_mode: str = "LIVE",
    classifier: RelevanceClassifier | None = None,
    generator: ProposalGenerator | None = None,
    params: BucketParams | None = None,
) -> TickResult:
    """Run one durable pipeline tick; ``now`` is the injected clock instant.

    The mode is validated before any write (PB-1 mode lock). Every group is
    advanced and closed first, then the single bucketing pass assigns settled
    messages for all groups. Passing ``classifier`` or ``generator`` runs that
    pass after formation; exceptions from the passes are not swallowed here —
    the worker loop records the failure and retries on the next tick.
    """
    if clock_mode not in CLOCK_MODES:
        raise ClockModeError(
            f"unknown clock mode {clock_mode!r}; expected one of"
            f" {', '.join(CLOCK_MODES)}"
        )
    groups = tuple(
        advance_group_clock(connection, group_id, now=now, mode=clock_mode)
        for group_id in _group_ids(connection)
    )
    assignment = run_bucketing_pass(connection, params=params)
    _record_stage_latency(connection, assignment, now)
    relevance = (
        None if classifier is None else run_relevance_pass(connection, classifier)
    )
    proposals = (
        None if generator is None else run_proposal_pass(connection, generator)
    )
    return TickResult(
        groups=groups,
        assigned=len(assignment.assigned),
        skipped=len(assignment.skipped),
        relevance=relevance,
        proposals=proposals,
    )


def run_worker(
    connection: sqlite3.Connection,
    *,
    tick: Callable[[str], object],
    stop_event: StopEvent,
    interval_seconds: float,
    backoff_min_seconds: float = 1.0,
    backoff_max_seconds: float = 60.0,
) -> None:
    """Run ``tick`` until ``stop_event`` fires, reading the clock only here.

    ``tick`` receives the boundary-stamped UTC instant. A successful tick waits
    ``interval_seconds``; a failed tick is logged by exception type only (never
    its message, which may carry untrusted data) and waits the current backoff,
    doubling up to ``backoff_max_seconds``. Stopping is cooperative: the loop
    exits when :meth:`StopEvent.wait` returns true or the event is already set.
    """
    _validate_timing(interval_seconds, backoff_min_seconds, backoff_max_seconds)
    delay = interval_seconds
    backoff = backoff_min_seconds
    while not stop_event.is_set():
        try:
            tick(_utc_now())
        except Exception as error:  # noqa: BLE001 - the loop must survive a
            # failed tick; the durable attempt/clock state makes the failure
            # visible and the next tick retries it (NFR-REL-04/05).
            logger.error("pipeline tick failed: %s", type(error).__name__)
            delay = backoff
            backoff = min(backoff * 2, backoff_max_seconds)
        else:
            delay = interval_seconds
            backoff = backoff_min_seconds
        if stop_event.wait(delay):
            return


def _validate_timing(
    interval_seconds: float,
    backoff_min_seconds: float,
    backoff_max_seconds: float,
) -> None:
    for label, value in (
        ("interval_seconds", interval_seconds),
        ("backoff_min_seconds", backoff_min_seconds),
        ("backoff_max_seconds", backoff_max_seconds),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{label} must be positive, got {value!r}")
    if backoff_max_seconds < backoff_min_seconds:
        raise ValueError(
            "backoff_max_seconds must be at least backoff_min_seconds"
        )


def _utc_now() -> str:
    """The worker boundary's clock read (the only one in the tick path)."""
    return datetime.now(UTC).isoformat()


def _group_ids(connection: sqlite3.Connection) -> list[int]:
    """Every source group, ordered by id, so ticks are deterministic."""
    rows = connection.execute("SELECT id FROM source_group ORDER BY id").fetchall()
    return [int(row["id"]) for row in rows]


def _record_stage_latency(
    connection: sqlite3.Connection, assignment: AssignmentResult, now: str
) -> None:
    """Record ingestion-to-bucket timings for the messages this tick placed.

    One row per message and stage, subject = the source message (0011's
    exactly-one-subject contract). A negative delay (clock skew, or a replay
    instant behind the receipt) is never stored: it is simply not measurable
    from the recorded data, so no row is invented. Assignment happens once per
    message, so a repeated tick writes no duplicate rows.
    """
    if not assignment.assigned:
        return
    try:
        boundary = parse_event_time(now)
    except _TimestampError as error:
        raise ValueError(f"invalid tick instant: {now!r}") from error
    with transaction(connection):
        for message_id, _bucket_id in assignment.assigned:
            row = connection.execute(
                "SELECT event_timestamp, received_at FROM source_message"
                " WHERE id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                continue
            received = _optional_instant(row["received_at"])
            if received is None:
                continue
            event = _optional_instant(row["event_timestamp"])
            if event is not None and received >= event:
                _insert_measurement(
                    connection,
                    message_id,
                    INGESTION_DELAY_METRIC,
                    (received - event).total_seconds(),
                    now,
                )
            if boundary >= received:
                _insert_measurement(
                    connection,
                    message_id,
                    BUCKET_DELAY_METRIC,
                    (boundary - received).total_seconds(),
                    now,
                )


def _insert_measurement(
    connection: sqlite3.Connection,
    message_id: int,
    metric: str,
    value_seconds: float,
    recorded_at: str,
) -> None:
    connection.execute(
        "INSERT INTO latency_measurement"
        " (source_message_id, metric, value_seconds, recorded_at)"
        " VALUES (?, ?, ?, ?)",
        (message_id, metric, float(value_seconds), recorded_at),
    )


def _optional_instant(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_event_time(str(value))
    except _TimestampError:
        return None
