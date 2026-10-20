"""Bucket-formation service for ASEC (Phase 2 — bucket formation; SRS §3.2).

Implements FR-BKT-01..08, FR-BKT-10 and FR-BKT-12 over the committed schema
(0002 source messages, 0003 buckets) plus this slice's durable progress table
(0010 source_group_progress).

Binding semantics (engineering lead, approved Revision 4):

- Assignment never closes an existing bucket and never advances progress.
  Closure happens only in :func:`mark_stream_progress`, and only when
  ``e + C <= accepted progress`` (inclusive, ``e = min(s + M, max(s + W,
  l + G))`` per FR-BKT-05). A late bucket is born CLOSED iff its own
  ``e + C`` is at or behind the accepted progress, otherwise OPEN.
- A message is late iff its event time ``t`` is at or behind the group's
  accepted progress (``t <= P``), or it precedes the current OPEN bucket's
  start when one exists (``t < current.start``). Every late message creates
  its own bucket; it never joins a bucket created before it in event time.
- The current OPEN bucket per message is the OPEN bucket with the greatest
  ``(start_event_timestamp, id)``; FR-BKT-04 (``t - s < M and (t - s < W or
  t - l < G)``, all strict) is evaluated only against it, and a same-start
  late bucket wins by id and becomes current while OPEN.
- Predecessor links form a cycle-free graph, not one linear chain: for a new
  bucket the CLOSED bucket with the greatest start ``<= t`` (ties: greatest
  id), else the CLOSED bucket with the least start (recovery-link fallback,
  ties: least id), else the current OPEN bucket, else NULL. Predecessor ids
  are always smaller than the new bucket id; links are never rewritten.
- Order independence: within one call, composition depends only on the set
  of assigned messages (sorted by ``(event time, source message id)``).
  Across calls, composition depends on the order of calls: a message whose
  event time precedes an already-built bucket's start, or the accepted
  progress, is assigned as a late message (FR-BKT-08). This limitation is
  the subject of pending SRS amendment P5 and must not be claimed away.
- Invalid timestamps (missing, malformed, timezone-naive) never block the
  group: the message stays stored and unassigned and is reported as skipped;
  event times are never replaced with receipt time.

Timestamps are parsed with :meth:`datetime.fromisoformat` after ``Z`` to
``+00:00`` normalization and compared as timezone-aware UTC values. Bucket
bounds are stored in the same canonical UTC form, so lexicographic and
chronological order coincide.
"""

from __future__ import annotations

import enum
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backend.config import Config
from backend.db.connection import transaction

TRACKING_GROUP_KEY = "group_id"

# SRS §3.2 production defaults: base window 15 s, burst gap 5 s, maximum
# duration 30 s, close grace 2 s.
DEFAULT_BUCKET_WINDOW_W = 15
DEFAULT_BUCKET_BURST_GAP_G = 5
DEFAULT_BUCKET_MAX_DURATION_M = 30
DEFAULT_BUCKET_CLOSE_GRACE_C = 2

# Configuration keys read by load_bucket_params (NFR-MNT-01). Only the bucket
# parameters are read here; unset or blank values fall back to the §3.2
# production defaults. Also documented in `.env.example`.
_BUCKET_CONFIG_KEYS: tuple[tuple[str, str], ...] = (
    ("window_w", "BUCKET_WINDOW_W_S"),
    ("burst_gap_g", "BUCKET_BURST_GAP_G_S"),
    ("max_duration_m", "BUCKET_MAX_DURATION_M_S"),
    ("close_grace_c", "BUCKET_CLOSE_GRACE_C_S"),
)


def load_bucket_params(config: Config) -> BucketParams:
    """Build :class:`BucketParams` from optional configuration overrides.

    Reads ``BUCKET_WINDOW_W_S``, ``BUCKET_BURST_GAP_G_S``,
    ``BUCKET_MAX_DURATION_M_S`` and ``BUCKET_CLOSE_GRACE_C_S`` (positive
    integers, seconds) out of the given :class:`~backend.config.Config`.
    Absent or blank values keep the §3.2 production default; any value that is
    not a positive integer raises :class:`BucketParamsError` (invalid
    configuration fails loudly, it is never silently clamped).
    """
    overrides: dict[str, int] = {}
    for field, key in _BUCKET_CONFIG_KEYS:
        raw = config.get(key)
        if raw is None or raw.strip() == "":
            continue
        try:
            value = int(raw.strip())
        except ValueError as error:
            raise BucketParamsError(
                f"{key} must be a positive integer, got {raw!r}"
            ) from error
        overrides[field] = value
    try:
        return BucketParams(**overrides)
    except BucketParamsError as error:
        raise BucketParamsError(str(error)) from error


class BucketParamsError(ValueError):
    """Raised when bucket parameters are not positive integers."""


@dataclass(frozen=True)
class BucketParams:
    """SRS §3.2 parameters (seconds).

    Defaults are the §3.2 production set (W=15, G=5, M=30, C=2). SRS §10.1
    separately declares its own test parameter set (W=10, G=5, M=30, C=3);
    tests exercising it pass those values explicitly. Optional overrides come
    from :func:`load_bucket_params` (NFR-MNT-01: parameters are configuration).
    """

    window_w: int = DEFAULT_BUCKET_WINDOW_W
    burst_gap_g: int = DEFAULT_BUCKET_BURST_GAP_G
    max_duration_m: int = DEFAULT_BUCKET_MAX_DURATION_M
    close_grace_c: int = DEFAULT_BUCKET_CLOSE_GRACE_C

    def __post_init__(self) -> None:
        for name in ("window_w", "burst_gap_g", "max_duration_m", "close_grace_c"):
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise BucketParamsError(f"{name} must be a positive integer, got {value!r}")

    def as_json(self) -> str:
        """Canonical JSON recording of the effective parameters (§4.1)."""
        return json.dumps(
            {
                "window_w": self.window_w,
                "burst_gap_g": self.burst_gap_g,
                "max_duration_m": self.max_duration_m,
                "close_grace_c": self.close_grace_c,
            },
            sort_keys=False,
        )


class SkipReason(enum.Enum):
    """Why a stored message has no bucketable event time (observable, not a guess)."""

    MISSING_EVENT_TIME = "missing"
    MALFORMED_EVENT_TIME = "malformed"
    NAIVE_EVENT_TIME = "naive"


@dataclass(frozen=True)
class SkippedMessage:
    message_id: int
    reason: SkipReason


@dataclass(frozen=True)
class AssignmentResult:
    assigned: tuple[tuple[int, int], ...] = ()
    late_message_ids: tuple[int, ...] = ()
    born_closed_bucket_ids: tuple[int, ...] = ()
    skipped: tuple[SkippedMessage, ...] = ()


@dataclass(frozen=True)
class ProgressResult:
    accepted: bool
    regressed: bool
    accepted_progress: str | None
    closed_bucket_ids: tuple[int, ...] = ()


class _TimestampError(Exception):
    """Internal: an event timestamp cannot be used as an event time."""

    def __init__(self, reason: SkipReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def parse_event_time(value: str | None) -> datetime:
    """Parse an event timestamp into an aware UTC datetime.

    Accepts ISO-8601 strings; ``Z`` is normalized to ``+00:00``. Missing values
    raise :class:`_TimestampError` with MISSING; unparsable strings with
    MALFORMED; timezone-naive values with NAIVE. Nothing is ever guessed.
    """
    if value is None or value.strip() == "":
        raise _TimestampError(SkipReason.MISSING_EVENT_TIME, "event timestamp is missing")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise _TimestampError(
            SkipReason.MALFORMED_EVENT_TIME, f"event timestamp not ISO-8601: {value!r}"
        ) from error
    if parsed.tzinfo is None:
        raise _TimestampError(
            SkipReason.NAIVE_EVENT_TIME, f"event timestamp is timezone-naive: {value!r}"
        )
    return parsed.astimezone(UTC)


def _format_event_time(instant: datetime) -> str:
    """Canonical UTC ISO-8601 storage form (lexicographic == chronological)."""
    return instant.astimezone(UTC).isoformat()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_or_skip(value: str | None, message_id: int) -> tuple[datetime, int] | SkippedMessage:
    try:
        return (parse_event_time(value), message_id)
    except _TimestampError as error:
        return SkippedMessage(message_id=message_id, reason=error.reason)


def _group_ids(connection: sqlite3.Connection, group_id: int | None) -> list[int]:
    if group_id is not None:
        return [group_id]
    rows = connection.execute("SELECT id FROM source_group ORDER BY id").fetchall()
    return [int(row["id"]) for row in rows]


def _unassigned_messages(
    connection: sqlite3.Connection, group_id: int
) -> list[tuple[int, str | None, str]]:
    rows = connection.execute(
        "SELECT m.id, m.event_timestamp, m.source_message_id"
        " FROM source_message m"
        " LEFT JOIN bucket_message bm ON bm.message_id = m.id"
        " WHERE m.group_id = ? AND bm.message_id IS NULL",
        (group_id,),
    ).fetchall()
    return [(int(row["id"]), row["event_timestamp"], str(row["source_message_id"])) for row in rows]


def _read_progress(
    connection: sqlite3.Connection, group_id: int
) -> datetime | None:
    row = connection.execute(
        "SELECT progress_to FROM source_group_progress WHERE group_id = ?", (group_id,)
    ).fetchone()
    if row is None:
        return None
    return parse_event_time(str(row["progress_to"]))


def _current_open(
    connection: sqlite3.Connection, group_id: int
) -> tuple[int, datetime] | None:
    """The current OPEN bucket: greatest (start, id) among OPEN buckets."""
    row = connection.execute(
        "SELECT id, start_event_timestamp FROM bucket"
        " WHERE group_id = ? AND processing_state = 'OPEN'"
        " ORDER BY start_event_timestamp DESC, id DESC LIMIT 1",
        (group_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row["id"]), parse_event_time(str(row["start_event_timestamp"]))


def _last_member_time(
    connection: sqlite3.Connection, bucket_id: int, start: datetime
) -> datetime:
    """l for FR-BKT-04: the group's latest member instant (>= start)."""
    rows = connection.execute(
        "SELECT m.event_timestamp FROM bucket_message bm"
        " JOIN source_message m ON m.id = bm.message_id"
        " WHERE bm.bucket_id = ?",
        (bucket_id,),
    ).fetchall()
    latest = start
    for row in rows:
        member = parse_event_time(row["event_timestamp"])
        if member > latest:
            latest = member
    return latest


def _computed_end(start: datetime, last: datetime, params: BucketParams) -> datetime:
    """FR-BKT-05: e = min(s + M, max(s + W, l + G))."""
    return min(
        start + timedelta(seconds=params.max_duration_m),
        max(
            start + timedelta(seconds=params.window_w),
            last + timedelta(seconds=params.burst_gap_g),
        ),
    )


def _persisted_params(bucket_id: int, text: str | None) -> BucketParams:
    """The bucket's recorded parameters (§4.1 ``parameters_used``) — the sole
    authority for computing that bucket's closure end (review fix M1).

    Every bucket created by this service records its parameters and no
    migration or supported writer produces a bucket without them, so a missing
    or malformed record is a data-integrity error: it fails loudly instead of
    being silently replaced — closure is a state transition and must never
    guess.
    """
    if text is None:
        raise BucketParamsError(
            f"bucket {bucket_id} has no recorded parameters_used;"
            " cannot compute its closure end"
        )
    try:
        stored = json.loads(text)
    except ValueError as error:
        raise BucketParamsError(
            f"bucket {bucket_id} has malformed parameters_used: {text!r}"
        ) from error
    try:
        return BucketParams(
            window_w=int(stored["window_w"]),
            burst_gap_g=int(stored["burst_gap_g"]),
            max_duration_m=int(stored["max_duration_m"]),
            close_grace_c=int(stored["close_grace_c"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BucketParamsError(
            f"bucket {bucket_id} has invalid parameters_used: {text!r}"
        ) from error


def _joins(
    event: datetime, start: datetime, last: datetime, params: BucketParams
) -> bool:
    """FR-BKT-04, all inequalities strict."""
    duration = event - start
    return duration < timedelta(seconds=params.max_duration_m) and (
        duration < timedelta(seconds=params.window_w)
        or event - last < timedelta(seconds=params.burst_gap_g)
    )


def _closed_starts(
    connection: sqlite3.Connection, group_id: int
) -> list[tuple[datetime, int]]:
    rows = connection.execute(
        "SELECT id, start_event_timestamp FROM bucket"
        " WHERE group_id = ? AND processing_state = 'CLOSED'",
        (group_id,),
    ).fetchall()
    return [(parse_event_time(str(row["start_event_timestamp"])), int(row["id"])) for row in rows]


def _predecessor(
    connection: sqlite3.Connection,
    group_id: int,
    event: datetime,
    current_open: tuple[int, datetime] | None,
) -> int | None:
    """Cycle-free predecessor graph rule (see module docstring)."""
    closed = _closed_starts(connection, group_id)
    at_or_before = [pair for pair in closed if pair[0] <= event]
    if at_or_before:
        # Greatest start <= t; equal starts break to the greatest bucket id.
        return max(at_or_before, key=lambda pair: (pair[0], pair[1]))[1]
    if closed:
        # Recovery-link fallback: least start (ties: least id). Not an
        # event-time predecessor; documented in pending SRS amendment P3.
        return min(closed)[1]
    if current_open is not None:
        return current_open[0]
    return None


def _create_bucket(
    connection: sqlite3.Connection,
    *,
    group_id: int,
    start: datetime,
    end: datetime | None,
    state: str,
    predecessor: int | None,
    params: BucketParams,
) -> int:
    cursor = connection.execute(
        "INSERT INTO bucket (group_id, predecessor_bucket_id,"
        " start_event_timestamp, end_event_timestamp, parameters_used,"
        " processing_state) VALUES (?, ?, ?, ?, ?, ?)",
        (
            group_id,
            predecessor,
            _format_event_time(start),
            None if end is None else _format_event_time(end),
            params.as_json(),
            state,
        ),
    )
    return int(cursor.lastrowid or 0)


def _add_membership(
    connection: sqlite3.Connection, bucket_id: int, message_id: int
) -> None:
    connection.execute(
        "INSERT INTO bucket_message (bucket_id, message_id, group_id)"
        " SELECT ?, id, group_id FROM source_message WHERE id = ?",
        (bucket_id, message_id),
    )


def _mark_late(connection: sqlite3.Connection, message_id: int) -> None:
    connection.execute(
        "UPDATE source_message SET flag_late = 1 WHERE id = ? AND flag_late = 0",
        (message_id,),
    )


def _split_batch(
    connection: sqlite3.Connection, group_id: int
) -> tuple[list[tuple[datetime, int]], tuple[SkippedMessage, ...]]:
    valid: list[tuple[datetime, int, str]] = []
    skipped: list[SkippedMessage] = []
    for message_id, event_timestamp, source_message_id in _unassigned_messages(
        connection, group_id
    ):
        outcome = _parse_or_skip(event_timestamp, message_id)
        if isinstance(outcome, SkippedMessage):
            skipped.append(outcome)
        else:
            valid.append((outcome[0], message_id, source_message_id))
    # FR-BKT-03: order by (event timestamp, source message ID). This single
    # sort is the composition decision; re-sorting by internal id would
    # silently override the source-message tie-break.
    valid.sort(key=lambda item: (item[0], item[2]))
    ordered = [(event, message_id) for event, message_id, _ in valid]
    return ordered, tuple(skipped)


def _assign_group(
    connection: sqlite3.Connection, group_id: int, params: BucketParams
) -> tuple[AssignmentResult, bool]:
    ordered, skipped = _split_batch(connection, group_id)
    if not ordered:
        return AssignmentResult(skipped=skipped), False
    assigned: list[tuple[int, int]] = []
    late_ids: list[int] = []
    born_closed: list[int] = []
    with transaction(connection):
        # progress is read once inside the transaction; assignment never
        # writes it (FR-BKT-04 late rule, FR-BKT-05 closure via progress only)
        progress = _read_progress(connection, group_id)
        for event, message_id in ordered:
            current = _current_open(connection, group_id)
            late = (progress is not None and event <= progress) or (
                current is not None and event < current[1]
            )
            if late:
                end = _computed_end(event, event, params)
                born = progress is not None and end + timedelta(
                    seconds=params.close_grace_c
                ) <= progress
                predecessor = _predecessor(connection, group_id, event, current)
                bucket_id = _create_bucket(
                    connection,
                    group_id=group_id,
                    start=event,
                    end=end if born else None,
                    state="CLOSED" if born else "OPEN",
                    predecessor=predecessor,
                    params=params,
                )
                _add_membership(connection, bucket_id, message_id)
                _mark_late(connection, message_id)
                late_ids.append(message_id)
                if born:
                    born_closed.append(bucket_id)
                assigned.append((message_id, bucket_id))
                continue
            if current is None:
                predecessor = _predecessor(connection, group_id, event, None)
                bucket_id = _create_bucket(
                    connection,
                    group_id=group_id,
                    start=event,
                    end=None,
                    state="OPEN",
                    predecessor=predecessor,
                    params=params,
                )
                _add_membership(connection, bucket_id, message_id)
                assigned.append((message_id, bucket_id))
                continue
            bucket_id, start = current
            last = _last_member_time(connection, bucket_id, start)
            if _joins(event, start, last, params):
                _add_membership(connection, bucket_id, message_id)
                assigned.append((message_id, bucket_id))
            else:
                predecessor = _predecessor(connection, group_id, event, current)
                successor_id = _create_bucket(
                    connection,
                    group_id=group_id,
                    start=event,
                    end=None,
                    state="OPEN",
                    predecessor=predecessor,
                    params=params,
                )
                _add_membership(connection, successor_id, message_id)
                assigned.append((message_id, successor_id))
    return (
        AssignmentResult(
            assigned=tuple(assigned),
            late_message_ids=tuple(late_ids),
            born_closed_bucket_ids=tuple(born_closed),
            skipped=skipped,
        ),
        True,
    )


def assign_messages(
    connection: sqlite3.Connection,
    *,
    group_id: int | None = None,
    params: BucketParams | None = None,
) -> AssignmentResult:
    """Assign each group's unassigned messages deterministically (FR-BKT-01..04).

    Messages are ordered by ``(event time, source message id)`` per FR-BKT-03.
    No bucket is closed here and progress is never written. Invalid event
    timestamps are isolated: their messages stay stored and unassigned and are
    reported in ``skipped``.
    """
    effective = params or BucketParams()
    assigned: list[tuple[int, int]] = []
    late_ids: list[int] = []
    born_closed: list[int] = []
    skipped: list[SkippedMessage] = []
    for resolved_group in _group_ids(connection, group_id):
        group_result, _ = _assign_group(connection, resolved_group, effective)
        assigned.extend(group_result.assigned)
        late_ids.extend(group_result.late_message_ids)
        born_closed.extend(group_result.born_closed_bucket_ids)
        skipped.extend(group_result.skipped)
    return AssignmentResult(
        assigned=tuple(assigned),
        late_message_ids=tuple(late_ids),
        born_closed_bucket_ids=tuple(born_closed),
        skipped=tuple(skipped),
    )


def mark_stream_progress(
    connection: sqlite3.Connection,
    *,
    group_id: int,
    event_progress: str,
) -> ProgressResult:
    """Accept event-time progress for a group and close eligible buckets.

    The value must parse as a timezone-aware event time (``ValueError``
    otherwise). Inside one transaction: regression or equality with the stored
    progress is a no-op report (nothing is written); a strictly greater value
    is stored atomically together with the closure of every OPEN bucket whose
    own persisted ``e + C`` (from that bucket's ``parameters_used``, §4.1) is
    at or behind the new progress, in threshold order.
    """
    try:
        new_progress = parse_event_time(event_progress)
    except _TimestampError as error:
        raise ValueError(f"invalid event progress: {error.detail}") from error
    with transaction(connection):
        stored = _read_progress(connection, group_id)
        if stored is not None and new_progress <= stored:
            return ProgressResult(
                accepted=False,
                regressed=True,
                accepted_progress=_format_event_time(stored),
            )
        connection.execute(
            "INSERT INTO source_group_progress (group_id, progress_to, updated_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(group_id) DO UPDATE SET progress_to = excluded.progress_to,"
            " updated_at = excluded.updated_at",
            (group_id, _format_event_time(new_progress), _utc_now()),
        )
        closed: list[int] = []
        open_rows = connection.execute(
            "SELECT id, start_event_timestamp, parameters_used FROM bucket"
            " WHERE group_id = ? AND processing_state = 'OPEN'",
            (group_id,),
        ).fetchall()
        eligible: list[tuple[datetime, int]] = []
        for row in open_rows:
            bucket_id = int(row["id"])
            start = parse_event_time(str(row["start_event_timestamp"]))
            own_params = _persisted_params(bucket_id, row["parameters_used"])
            last = _last_member_time(connection, bucket_id, start)
            end = _computed_end(start, last, own_params)
            if end + timedelta(seconds=own_params.close_grace_c) <= new_progress:
                eligible.append((end, bucket_id))
        eligible.sort(key=lambda item: (item[0], item[1]))
        for end, bucket_id in eligible:
            connection.execute(
                "UPDATE bucket SET processing_state = 'CLOSED',"
                " end_event_timestamp = ? WHERE id = ? AND processing_state = 'OPEN'",
                (_format_event_time(end), bucket_id),
            )
            closed.append(bucket_id)
    return ProgressResult(
        accepted=True,
        regressed=False,
        accepted_progress=_format_event_time(new_progress),
        closed_bucket_ids=tuple(closed),
    )
