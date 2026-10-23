"""Pipeline orchestration and operational visibility for ASEC (Phase 3).

This module is the seam between the Phase 2 ingestion boundary
(:mod:`backend.ingest`), bucket formation (:mod:`backend.bucketing`) and the
bucket lifecycle service (:mod:`backend.bucket_lifecycle`). It contains no
new business semantics: formation, closure and state transitions stay in
their owning modules; this layer only drives, reads and reports them.

- :func:`run_bucketing_pass` is the sanctioned one-call orchestration pass
  for every (or one) source group: assign that group's unassigned ingested
  messages. It never closes a bucket and never advances event-time progress
  (FR-BKT-05: closure happens only inside an accepted
  :func:`backend.bucketing.mark_stream_progress` call, whose authority and
  scheduling remain an open owner decision — pending P2/P6 amendment).
- :func:`pipeline_backlog` makes the backlog of unprocessed buckets visible
  (count and age of the oldest) plus unassigned message counts, so overload
  shows as delay or backlog and is never silent loss (NFR-REL-06, FR-UI-05).
  Age is expressed in the domain buckets already live in — event time — as
  accepted group progress minus the oldest unprocessed bucket's anchor
  (its end, else its start). Wall-clock age is deliberately not derived.
- :func:`processing_queue` is the read-only hand-off for the classification
  stage (FR-REL-01 runs on each closed bucket): closed buckets that no
  :class:`backend.db migration 0004` ``processing_attempt`` has ever
  touched (never started), buckets paused in AWAITING_RECOVERY that resume
  processing after a component outage (NFR-REL-05, AC-13), and FAILED
  buckets awaiting the manual re-queue of NFR-REL-03.

Recovery and restart behavior needs no dedicated code path: all observed
state is committed in SQLite, a pass re-runs over whomever is still
unassigned, and already-assigned messages are deduplicated by the storage
schema. The tests demonstrate a full restart through a fresh connection.

Post-recovery UNCLASSIFIED-flag clearing is a recorded open owner decision
(docs/PHASE2_PLAN.md §7) and is intentionally not implemented here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from backend.bucket_lifecycle import BucketState
from backend.bucketing import (
    AssignmentResult,
    BucketParams,
    _TimestampError,
    assign_messages,
    parse_event_time,
)

_ALL_STATES = tuple(state.value for state in BucketState)


def run_bucketing_pass(
    connection: sqlite3.Connection,
    *,
    params: BucketParams | None = None,
    group_id: int | None = None,
) -> AssignmentResult:
    """Drive one formation pass (FR-BKT-01..04) over ingested messages.

    ``group_id=None`` covers every source group; each group's buckets form
    independently and in parallel (FR-BKT-02). No closures and no progress
    writes happen here (FR-BKT-05 authority stays with
    :func:`backend.bucketing.mark_stream_progress`).
    """
    return assign_messages(connection, group_id=group_id, params=params)


@dataclass(frozen=True)
class GroupBacklog:
    """Per-group backlog picture (NFR-REL-06, FR-UI-05).

    ``buckets_by_state`` is keyed by the FR-BKT-12 vocabulary and covers the
    five states with zero counts included. ``unprocessed_buckets`` is every
    bucket not yet PROCESSED, including FAILED and AWAITING_RECOVERY ones.
    ``age_event_seconds`` is accepted progress minus the oldest unprocessed
    bucket's anchor (end else start event timestamp); it is ``None`` when no
    bucket is unprocessed, when the group has no accepted progress yet,
    when either timestamp cannot be parsed as an event time — the oldest
    item is then ranked only as "undatable" (see ``_oldest_unprocessed``) —
    or when the anchor lies ahead of accepted progress: a negative
    event-time age is not meaningful, so the age is unavailable (``None``),
    never negative, until accepted progress passes the anchor.
    Timestamps are also reported raw so consumers never depend on the
    derived value.
    """

    group_id: int
    unassigned_messages: int
    buckets_by_state: Mapping[str, int]
    unprocessed_buckets: int
    oldest_unprocessed_bucket_id: int | None
    oldest_unprocessed_anchor: str | None
    progress_to: str | None
    age_event_seconds: int | None


@dataclass(frozen=True)
class PipelineBacklog:
    """Aggregate of every group's :class:`GroupBacklog` (ordered by id)."""

    groups: tuple[GroupBacklog, ...]

    @property
    def unprocessed_buckets(self) -> int:
        return sum(group.unprocessed_buckets for group in self.groups)

    @property
    def unassigned_messages(self) -> int:
        return sum(group.unassigned_messages for group in self.groups)


def _group_ids(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute(
        "SELECT id FROM source_group ORDER BY id"
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _reading_progress(
    connection: sqlite3.Connection, group_id: int
) -> str | None:
    row = connection.execute(
        "SELECT progress_to FROM source_group_progress WHERE group_id = ?",
        (group_id,),
    ).fetchone()
    return None if row is None else str(row["progress_to"])


def _oldest_unprocessed(
    connection: sqlite3.Connection, group_id: int
) -> tuple[int, str | None] | None:
    """Smallest unprocessed anchor, ranked in event time (not by text order).

    Anchors are ranked by parsed instant (``parse_event_time``), ties by id —
    verbatim timestamps are free text (DR-04), so lexicographic SQL ordering
    is not chronological. An unprocessed bucket whose anchor is missing or
    unparsable cannot be dated at all; it is reported as the oldest
    unprocessed backlog item (nothing can rank it later) with its raw anchor
    value and no derived age.
    """
    rows = connection.execute(
        """
        SELECT id, start_event_timestamp, end_event_timestamp
          FROM bucket
         WHERE group_id = ? AND processing_state != 'PROCESSED'
        """,
        (group_id,),
    ).fetchall()
    if not rows:
        return None

    def rank(row: sqlite3.Row) -> tuple[float, float, int]:
        anchor = (
            row["end_event_timestamp"]
            if row["end_event_timestamp"] is not None
            else row["start_event_timestamp"]
        )
        try:
            return (0.0, parse_event_time(anchor).timestamp(), int(row["id"]))
        except _TimestampError:
            # Undatable anchors are the oldest unresolvable backlog.
            return (-1.0, 0.0, int(row["id"]))

    oldest = min(rows, key=rank)
    anchor = (
        oldest["end_event_timestamp"]
        if oldest["end_event_timestamp"] is not None
        else oldest["start_event_timestamp"]
    )
    return int(oldest["id"]), None if anchor is None else str(anchor)


def _age_event_seconds(
    anchor: str | None, progress: str | None
) -> int | None:
    """Accepted progress minus the anchor, or ``None`` when unavailable.

    ``None`` when either timestamp is missing or unparsable, or when the
    anchor lies ahead of accepted progress: a late bucket can form with its
    computed boundary beyond the group's progress marker, and a negative
    event-time age is not meaningful, so it is never reported as negative —
    the age is simply unavailable until accepted progress passes the anchor.
    """
    if anchor is None or progress is None:
        return None
    try:
        anchor_time = parse_event_time(anchor)
        progress_time = parse_event_time(progress)
    except _TimestampError:
        return None
    delta: timedelta = progress_time - anchor_time
    if delta.total_seconds() < 0:
        return None
    return int(delta.total_seconds())


def pipeline_backlog(connection: sqlite3.Connection) -> PipelineBacklog:
    """Read-only backlog visibility for every source group (NFR-REL-06).

    Unassigned messages keep counting until they join a bucket — including
    messages bucketing had to skip for unusable timestamps — so overload and
    data problems surface as backlog, never as silent loss. Every query runs
    on the caller's connection; nothing here writes.
    """
    groups: list[GroupBacklog] = []
    for group_id in _group_ids(connection):
        counts = {state: 0 for state in _ALL_STATES}
        for row in connection.execute(
            """
            SELECT processing_state, COUNT(*) AS n
              FROM bucket
             WHERE group_id = ?
             GROUP BY processing_state
            """,
            (group_id,),
        ):
            counts[str(row["processing_state"])] = int(row["n"])
        unassigned = int(
            connection.execute(
                """
                SELECT COUNT(*) AS n
                  FROM source_message
                 WHERE group_id = ?
                   AND NOT EXISTS
                       (SELECT 1 FROM bucket_message
                         WHERE bucket_message.message_id = source_message.id)
                """,
                (group_id,),
            ).fetchone()["n"]
        )
        unprocessed = sum(
            n
            for state, n in counts.items()
            if state != BucketState.PROCESSED.value
        )
        oldest = _oldest_unprocessed(connection, group_id)
        progress = _reading_progress(connection, group_id)
        anchor = None if oldest is None else oldest[1]
        groups.append(
            GroupBacklog(
                group_id=group_id,
                unassigned_messages=unassigned,
                buckets_by_state=counts,
                unprocessed_buckets=unprocessed,
                oldest_unprocessed_bucket_id=None if oldest is None else oldest[0],
                oldest_unprocessed_anchor=anchor,
                progress_to=progress,
                age_event_seconds=_age_event_seconds(anchor, progress),
            )
        )
    return PipelineBacklog(groups=tuple(groups))


@dataclass(frozen=True)
class ProcessingQueue:
    """The classification stage's read-only work queues (§4.1, FR-REL-01).

    ``never_started``: CLOSED buckets with no :mod:`backend.db migration
    0004` ``processing_attempt`` row — the classifier has never touched them.
    ``awaiting_recovery``: AWAITING_RECOVERY buckets waiting to resume after
    a component outage (NFR-REL-05, AC-13).
    ``failed``: buckets whose processing failed permanently and that only a
    manual re-queue (NFR-REL-03, via the lifecycle service) moves back into
    ``awaiting_recovery``.
    """

    never_started: tuple[int, ...]
    awaiting_recovery: tuple[int, ...]
    failed: tuple[int, ...]


def processing_queue(connection: sqlite3.Connection) -> ProcessingQueue:
    """Read the queues downstream phases consume; makes no state change."""
    locked_rows = connection.execute(
        """
        SELECT b.id, b.processing_state
          FROM bucket AS b
         WHERE (b.processing_state = 'CLOSED'
                AND NOT EXISTS (SELECT 1 FROM processing_attempt
                                 WHERE bucket_id = b.id))
            OR b.processing_state IN ('AWAITING_RECOVERY', 'FAILED')
         ORDER BY b.id
        """
    ).fetchall()
    never_started: list[int] = []
    awaiting: list[int] = []
    failed: list[int] = []
    for row in locked_rows:
        bucket_id = int(row["id"])
        state = str(row["processing_state"])
        if state == BucketState.CLOSED.value:
            never_started.append(bucket_id)
        elif state == BucketState.AWAITING_RECOVERY.value:
            awaiting.append(bucket_id)
        else:
            failed.append(bucket_id)
    return ProcessingQueue(
        never_started=tuple(never_started),
        awaiting_recovery=tuple(awaiting),
        failed=tuple(failed),
    )
