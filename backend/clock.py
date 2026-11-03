"""Per-group pipeline clock for ASEC (Phase 9 — PB-1; migration 0020).

PB-1 replaces the single event-time progress watermark as the driver of
bucket formation with a durable per-group clock:

- The clock is monotonic per group: ``new = max(stored, now)``. An earlier or
  equal ``now`` is a no-op (``advanced=False``) but still evaluates closure
  idempotently, because assignment never closes a bucket and the next clock
  call must finish the job.
- The mode is written on the first call and locked afterwards (O9-5: no seed;
  the first caller sets the mode). Switching a group between LIVE and REPLAY
  raises :class:`ClockModeError`, because mixing live and replay data in one
  group's clock needs an owner decision.
- One transaction writes the clock row, sets ``source_group_progress`` to the
  same boundary, and closes every OPEN bucket whose own ``e + C`` is at or
  behind the clock, in ``(e, id)`` order (FR-BKT-05). Closure is delegated to
  the bucket service so the persisted ``parameters_used`` stays the only
  authority for a bucket's end.
- ``now`` is injected by the caller. This module never reads the wall clock
  (NFR-MNT-01): only the worker/CLI boundary turns ``datetime.now(UTC)`` into
  the argument.

The clock does not create buckets and does not assign messages; it is read by
:func:`backend.bucketing.assign_messages` for settle-then-assign.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from backend.bucketing import close_open_buckets_at_progress, parse_event_time
from backend.db.connection import transaction

ClockMode = Literal["LIVE", "REPLAY"]

#: The only FR-BKT-12-independent modes PB-1 allows. No implicit default.
CLOCK_MODES: tuple[ClockMode, ...] = ("LIVE", "REPLAY")


class ClockError(ValueError):
    """Raised when a clock value or mode is unusable; nothing was written."""


class ClockModeError(ClockError):
    """The stored mode differs from the caller's mode (PB-1 mode lock)."""


@dataclass(frozen=True)
class ClockResult:
    """Outcome of one authoritative clock advance for a group."""

    group_id: int
    clock_at: str
    advanced: bool
    closed_bucket_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class GroupClock:
    """The durable clock of one group, as read back from the database."""

    group_id: int
    clock_at: str
    mode: str


def advance_group_clock(
    connection: sqlite3.Connection, group_id: int, *, now: str, mode: str
) -> ClockResult:
    """Advance one group's clock and close the buckets it authorizes.

    ``now`` must be a timezone-aware ISO-8601 instant; ``mode`` must be one of
    :data:`CLOCK_MODES`. Validation happens before the transaction so a
    rejected call writes nothing. Inside one ``BEGIN IMMEDIATE`` transaction:
    the first call inserts the row with its mode; later calls must match the
    stored mode; the clock never moves backwards; an advancing call also
    writes the same boundary to ``source_group_progress``; closure runs on
    every call (idempotent) and its bucket ids are returned in ``(e, id)``
    order.
    """
    if mode not in CLOCK_MODES:
        raise ClockModeError(
            f"unknown clock mode {mode!r}; expected one of {', '.join(CLOCK_MODES)}"
        )
    instant = _parse_instant(now)
    with transaction(connection):
        row = connection.execute(
            "SELECT clock_at, mode FROM source_group_clock WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        if row is None:
            boundary = instant
            advanced = True
        else:
            stored_mode = str(row["mode"])
            if stored_mode != mode:
                raise ClockModeError(
                    f"group {group_id} clock mode is {stored_mode};"
                    f" cannot advance it as {mode}"
                )
            stored = _parse_instant(str(row["clock_at"]))
            advanced = instant > stored
            boundary = instant if advanced else stored
        canonical = _canonical(boundary)
        if advanced:
            connection.execute(
                "INSERT INTO source_group_clock"
                " (group_id, clock_at, mode, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(group_id) DO UPDATE SET"
                " clock_at = excluded.clock_at, updated_at = excluded.updated_at",
                (group_id, canonical, mode, canonical),
            )
            connection.execute(
                "INSERT INTO source_group_progress (group_id, progress_to, updated_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(group_id) DO UPDATE SET"
                " progress_to = excluded.progress_to, updated_at = excluded.updated_at",
                (group_id, canonical, canonical),
            )
        closed = close_open_buckets_at_progress(
            connection, group_id=group_id, progress=boundary
        )
    return ClockResult(
        group_id=group_id,
        clock_at=canonical,
        advanced=advanced,
        closed_bucket_ids=closed,
    )


def read_group_clock(connection: sqlite3.Connection, group_id: int) -> GroupClock | None:
    """Read a group's clock, or ``None`` before its first advance."""
    row = connection.execute(
        "SELECT clock_at, mode FROM source_group_clock WHERE group_id = ?",
        (group_id,),
    ).fetchone()
    if row is None:
        return None
    return GroupClock(
        group_id=group_id,
        clock_at=_canonical(_parse_instant(str(row["clock_at"]))),
        mode=str(row["mode"]),
    )


def _parse_instant(value: str) -> datetime:
    """Parse a clock instant; unusable values raise :class:`ClockError`."""
    try:
        return parse_event_time(value)
    except ValueError as error:
        raise ClockError(f"invalid clock instant: {value!r}") from error


def _canonical(instant: datetime) -> str:
    """Canonical UTC storage form (matches the bucket service, DR-04)."""
    return instant.astimezone(UTC).isoformat()
