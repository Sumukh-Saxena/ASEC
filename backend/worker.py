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
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from backend.alerts import POLL_FAILURE_RULE, Alert, AlertSender
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
    read_group_clock,
)
from backend.config import (
    DEFAULT_POLL_ALERT_AFTER_FAILURES,
    DEFAULT_POLL_SAFETY_MARGIN_SECONDS,
    PartSettings,
)
from backend.db.connection import transaction
from backend.discord_adapter import DiscordSource, read_cursor
from backend.ingest import ingest_message
from backend.orchestration import run_bucketing_pass
from backend.proposals import ProposalGenerator, run_proposal_pass
from backend.relevance import RelevanceClassifier, run_relevance_pass
from backend.system_control import optional_stages_paused

logger = logging.getLogger("asec.worker")

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


@dataclass(frozen=True)
class GroupPollResult:
    """One group's live poll outcome (W8a).

    ``stored`` counts messages newly committed by :func:`ingest_message`;
    ``duplicates`` are redeliveries that wrote nothing. ``polled_at`` is the
    instant recorded only after the whole page committed, and stays ``None``
    on failure or for a REPLAY group that the live driver must not touch.
    """

    group_id: int
    channel_id: str
    fetched: int = 0
    stored: int = 0
    duplicates: int = 0
    failed: bool = False
    skipped_replay: bool = False
    polled_at: str | None = None


@dataclass(frozen=True)
class LiveTickResult:
    """What one live poll-and-tick cycle did (W8a)."""

    polls: tuple[GroupPollResult, ...]
    tick: TickResult
    consecutive_failures: Mapping[int, int]


def run_pipeline_tick(
    connection: sqlite3.Connection,
    *,
    now: str,
    clock_mode: str = "LIVE",
    classifier: RelevanceClassifier | None = None,
    generator: ProposalGenerator | None = None,
    params: BucketParams | None = None,
    parts: PartSettings | None = None,
    poll_instants: Mapping[int, str] | None = None,
    poll_safety_margin_seconds: float = DEFAULT_POLL_SAFETY_MARGIN_SECONDS,
    processing_paused: bool | None = None,
) -> TickResult:
    """Run one durable pipeline tick; ``now`` is the injected clock instant.

    The mode is validated before any write (PB-1 mode lock). Every group is
    advanced and closed first, then the single bucketing pass assigns settled
    messages for all groups. Passing ``classifier`` or ``generator`` runs that
    pass after formation; exceptions from the passes are not swallowed here —
    the worker loop records the failure and retries on the next tick.
    ``parts`` carries the validated O9-6 per-call part sizes (FR-BKT-11); when
    omitted the passes use their owner-approved defaults.

    ``poll_instants`` switches the tick to the live W8a rule: a group whose
    poll did not succeed has no bound and its clock is left unchanged, while a
    polled group advances only to ``min(now, polled_at - margin)``. REPLAY
    groups are never advanced by a live poll. With the default ``None`` the
    caller is the authority and every group advances to ``now`` as before.
    """
    if clock_mode not in CLOCK_MODES:
        raise ClockModeError(
            f"unknown clock mode {clock_mode!r}; expected one of"
            f" {', '.join(CLOCK_MODES)}"
        )
    if poll_safety_margin_seconds < 0:
        raise ValueError(
            "poll_safety_margin_seconds must be non-negative,"
            f" got {poll_safety_margin_seconds!r}"
        )
    if processing_paused is None:
        # Owner decision C: a processing pause skips only the optional AI
        # stages. Ingestion, the durable clock and bucket closure keep running,
        # so the backlog is ready and lossless when the operator resumes.
        processing_paused, _delivery = optional_stages_paused(connection)
    if poll_instants is None:
        groups = tuple(
            advance_group_clock(connection, group_id, now=now, mode=clock_mode)
            for group_id in _group_ids(connection)
        )
    else:
        bounds = _poll_bounded_instants(
            connection,
            now=now,
            poll_instants=poll_instants,
            margin_seconds=poll_safety_margin_seconds,
            clock_mode=clock_mode,
        )
        groups = tuple(
            advance_group_clock(
                connection, group_id, now=bounds[group_id], mode=clock_mode
            )
            for group_id in _group_ids(connection)
            if group_id in bounds
        )
    assignment = run_bucketing_pass(connection, params=params)
    _record_stage_latency(connection, assignment, now)
    active_parts = PartSettings() if parts is None else parts
    relevance = (
        None
        if classifier is None or processing_paused
        else run_relevance_pass(
            connection,
            classifier,
            max_messages_per_call=active_parts.relevance_max_messages_per_call,
        )
    )
    proposals = (
        None
        if generator is None or processing_paused
        else run_proposal_pass(
            connection,
            generator,
            max_candidates_per_call=active_parts.proposal_max_candidates_per_call,
        )
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
            logger.error(
                "pipeline tick failed: %s frames=%s",
                type(error).__name__,
                _traceback_frames(error),
            )
            delay = backoff
            backoff = min(backoff * 2, backoff_max_seconds)
        else:
            delay = interval_seconds
            backoff = backoff_min_seconds
        if stop_event.wait(delay):
            return


def run_live_tick(
    connection: sqlite3.Connection,
    *,
    source: DiscordSource,
    now: Callable[[], str],
    alert_sender: AlertSender | None = None,
    poll_safety_margin_seconds: float = DEFAULT_POLL_SAFETY_MARGIN_SECONDS,
    poll_alert_after_failures: int = DEFAULT_POLL_ALERT_AFTER_FAILURES,
    clock_mode: str = "LIVE",
    params: BucketParams | None = None,
    classifier: RelevanceClassifier | None = None,
    generator: ProposalGenerator | None = None,
    parts: PartSettings | None = None,
    consecutive_failures: dict[int, int] | None = None,
    delivery_tick: Callable[[], object] | None = None,
) -> LiveTickResult:
    """Poll every mapped group, ingest its page, then run the bounded tick.

    For each group the cursor is read from the stored messages (no cursor
    table), every fetched message is committed through
    :func:`backend.ingest.ingest_message`, and only then is the poll instant
    recorded. A group that fails while fetching or ingesting gets no instant,
    so the tick leaves its clock unchanged and closes nothing; the remaining
    groups continue. Repeated consecutive failures raise a
    :data:`backend.alerts.POLL_FAILURE_RULE` alert through ``alert_sender``.

    REPLAY groups are skipped entirely: live polling must not mix live data
    into a replay clock (PB-1 mode lock). ``consecutive_failures`` is an
    optional caller-owned counter map so a long-running loop can accumulate
    the repeated-failure condition across calls; the returned mapping is a
    snapshot. ``now`` is injected; this function never reads the wall clock.
    """
    if poll_safety_margin_seconds < 0:
        raise ValueError(
            "poll_safety_margin_seconds must be non-negative,"
            f" got {poll_safety_margin_seconds!r}"
        )
    if (
        isinstance(poll_alert_after_failures, bool)
        or not isinstance(poll_alert_after_failures, int)
        or poll_alert_after_failures < 1
    ):
        raise ValueError(
            "poll_alert_after_failures must be a positive integer,"
            f" got {poll_alert_after_failures!r}"
        )
    failures: dict[int, int] = (
        {} if consecutive_failures is None else consecutive_failures
    )
    processing_paused, delivery_paused = optional_stages_paused(connection)
    tick_now = now()
    poll_instants: dict[int, str] = {}
    polls: list[GroupPollResult] = []
    for group_id, channel_id in _mapped_groups(connection):
        stored = read_group_clock(connection, group_id)
        if stored is not None and stored.mode == "REPLAY":
            polls.append(
                GroupPollResult(
                    group_id=group_id,
                    channel_id=channel_id,
                    skipped_replay=True,
                )
            )
            continue
        cursor = read_cursor(connection, channel_id=channel_id)
        try:
            events = source.fetch_messages(channel_id=channel_id, after=cursor)
            stored_count = 0
            duplicates = 0
            for event in events:
                outcome = ingest_message(connection, event)
                if outcome.created:
                    stored_count += 1
                else:
                    duplicates += 1
            # The page is committed at this point; only now may the poll
            # instant authorize the group's clock (W8a).
            polled_at = now()
            poll_instants[group_id] = polled_at
            failures[group_id] = 0
            polls.append(
                GroupPollResult(
                    group_id=group_id,
                    channel_id=channel_id,
                    fetched=len(events),
                    stored=stored_count,
                    duplicates=duplicates,
                    polled_at=polled_at,
                )
            )
        except Exception as error:  # noqa: BLE001 - one group must not block
            # the others; the durable cursor/clock state makes the retry safe.
            failures[group_id] = failures.get(group_id, 0) + 1
            logger.error(
                "live poll failed for group %s channel %s: %s",
                group_id,
                channel_id,
                type(error).__name__,
            )
            if (
                alert_sender is not None
                and failures[group_id] >= poll_alert_after_failures
            ):
                alert_sender.send(
                    Alert(
                        rule=POLL_FAILURE_RULE,
                        severity="ERROR",
                        observed=failures[group_id],
                        threshold=poll_alert_after_failures,
                        message=(
                            f"group {group_id} channel {channel_id}:"
                            " consecutive live poll failures"
                        ),
                        at=tick_now,
                    )
                )
            polls.append(
                GroupPollResult(
                    group_id=group_id,
                    channel_id=channel_id,
                    failed=True,
                )
            )
    tick = run_pipeline_tick(
        connection,
        now=tick_now,
        clock_mode=clock_mode,
        classifier=classifier,
        generator=generator,
        params=params,
        parts=parts,
        poll_instants=poll_instants,
        poll_safety_margin_seconds=poll_safety_margin_seconds,
        processing_paused=processing_paused,
    )
    if delivery_tick is not None and not delivery_paused:
        try:
            delivery_tick()
        except Exception as error:  # noqa: BLE001 - delivery must not fail
            # the pipeline tick; the durable attempt rows make the failure
            # visible and the next tick retries it (FR-NOT-04).
            logger.error(
                "delivery tick failed: %s frames=%s",
                type(error).__name__,
                _traceback_frames(error),
            )
    return LiveTickResult(
        polls=tuple(polls),
        tick=tick,
        consecutive_failures=dict(failures),
    )


def _mapped_groups(connection: sqlite3.Connection) -> list[tuple[int, str]]:
    """Every source group with its Discord channel, ordered by id."""
    rows = connection.execute(
        "SELECT id, discord_channel_id FROM source_group ORDER BY id"
    ).fetchall()
    return [(int(row["id"]), str(row["discord_channel_id"])) for row in rows]


def _poll_bounded_instants(
    connection: sqlite3.Connection,
    *,
    now: str,
    poll_instants: Mapping[int, str],
    margin_seconds: float,
    clock_mode: str,
) -> dict[int, str]:
    """Compute ``min(now, polled_at - margin)`` per successfully polled group.

    Missing poll instants produce no bound, so a failed or absent poll holds
    the clock. A group already locked to a different mode is skipped: a live
    poll never advances (or relabels) a REPLAY clock.
    """
    now_instant = _parse_instant(now)
    margin = timedelta(seconds=margin_seconds)
    bounds: dict[int, str] = {}
    for group_id in _group_ids(connection):
        raw = poll_instants.get(group_id)
        if raw is None:
            continue
        stored = read_group_clock(connection, group_id)
        if stored is not None and stored.mode != clock_mode:
            continue
        polled_at = _parse_instant(raw)
        bounds[group_id] = min(now_instant, polled_at - margin).isoformat()
    return bounds


def _parse_instant(value: str) -> datetime:
    try:
        return parse_event_time(value)
    except _TimestampError as error:
        raise ValueError(f"invalid instant: {value!r}") from error


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


def _traceback_frames(error: BaseException, limit: int = 5) -> str:
    """Format the innermost ``limit`` frames as ``file:line function``.

    Only the frame locations and function names are returned. The exception
    message, its arguments, local values and source lines are deliberately
    omitted: any of them may carry message content or a token (SRS DR-07).
    """
    frames = traceback.extract_tb(error.__traceback__)[-limit:]
    return " ".join(
        f"{Path(frame.filename).name}:{frame.lineno} {frame.name}"
        for frame in frames
    )


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
