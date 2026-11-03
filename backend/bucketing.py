"""Bucket-formation service for ASEC (Phase 2 — bucket formation; SRS §3.2).

Implements FR-BKT-01..08, FR-BKT-10 and FR-BKT-12 over the committed schema
(0002 source messages, 0003 buckets), the durable progress table (0010
source_group_progress) and the PB-2 time-basis column (0019).

Binding semantics (engineering lead, approved Revision 4; Phase 9 PB-1/2/7):

- Assignment never closes an existing bucket and never advances progress.
  Closure happens only in :func:`mark_stream_progress` (legacy event-time
  progress) or :func:`backend.clock.advance_group_clock` (PB-1 per-group
  clock), and only when ``e + C <= boundary`` (inclusive, ``e = min(s + M,
  max(s + W, l + G))`` per FR-BKT-05). A late bucket is born CLOSED iff its
  own ``e + C`` is at or behind the boundary, otherwise OPEN.
- Settle-then-assign (PB-1): when the group has a clock row, only messages
  with ``t + C <= clock`` are assigned; the rest stay unassigned for the next
  tick. Without a clock row the pre-clock contract applies unchanged (direct
  service callers and databases that never started a clock).
- Late under the clock: the message would have joined a bucket that is
  already CLOSED (FR-BKT-08), or it precedes the current OPEN bucket's start
  (``t < current.start``). Every late message creates its own bucket; it
  never joins or mutates a bucket created before it. In the legacy no-clock
  path a message is late when ``t <= progress`` or ``t < current.start``.
- The current OPEN bucket per message is the OPEN bucket with the greatest
  ``(start_event_timestamp, id)``; FR-BKT-04 (``t - s < M and (t - s < W or
  t - l < G)``, all strict) is evaluated only against it, and a same-start
  late bucket wins by id and becomes current while OPEN.
- Predecessor links (PB-7): only a late bucket gets one, and it is one hop to
  the latest CLOSED bucket whose end is at or before ``t`` (ties: greatest
  id), else NULL. A bucket that is not late has NULL. There is no fallback
  chain and no multi-hop graph; buckets are processing boundaries, not
  conversations (BR-05, FR-BKT-09/10).
- Order independence: within one call, composition depends only on the set
  of settled messages (sorted by ``(event time, source message id)``). The
  clock's settle window means messages arriving within ``C`` of each other are
  all present before any of them is assigned (FR-BKT-06); a message that
  arrives later than that window is late by FR-BKT-08.
- Invalid timestamps (missing, malformed, timezone-naive) never block the
  group: PB-2 selects ``received_at`` when the event time is unusable, records
  ``time_basis='RECEIPT'`` and reports ``EVENT_TIME_UNUSABLE`` in the skip
  report while still placing the message (FR-BKT-01). The stored
  ``event_timestamp`` is never overwritten; event times are never replaced
  with receipt time. A message whose receipt time is also unusable stays
  stored and unassigned and is reported as skipped.

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
    EVENT_TIME_UNUSABLE = "event-time-unusable"


@dataclass(frozen=True)
class SkippedMessage:
    message_id: int
    reason: SkipReason


@dataclass(frozen=True)
class AssignmentResult:
    """Outcome of one assignment pass.

    ``skipped`` is the observable PB-2 report. A message with
    ``EVENT_TIME_UNUSABLE`` is reported *and* placed in a bucket using its
    receipt time; the other reasons mean the message stayed unassigned.
    """

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


class _TimestampError(ValueError):
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


def _effective_time(
    event_timestamp: str | None, received_at: str, message_id: int
) -> tuple[datetime, str, bool] | SkippedMessage:
    """PB-2 effective time: event time, else receipt time, else a reported skip.

    Returns ``(instant, basis, receipt_fallback)`` where ``basis`` is the value
    recorded in ``source_message.time_basis``. When the event time is unusable
    the receipt time is used *without* touching the stored event timestamp; the
    fallback flag tells the caller to report ``EVENT_TIME_UNUSABLE``. When
    neither time is usable the message stays unassigned and keeps the original
    event-time reason.
    """
    try:
        return parse_event_time(event_timestamp), "EVENT", False
    except _TimestampError as event_error:
        try:
            return parse_event_time(received_at), "RECEIPT", True
        except _TimestampError:
            return SkippedMessage(message_id=message_id, reason=event_error.reason)


def _group_ids(connection: sqlite3.Connection, group_id: int | None) -> list[int]:
    if group_id is not None:
        return [group_id]
    rows = connection.execute("SELECT id FROM source_group ORDER BY id").fetchall()
    return [int(row["id"]) for row in rows]


def _unassigned_messages(
    connection: sqlite3.Connection, group_id: int
) -> list[tuple[int, str | None, str, str]]:
    rows = connection.execute(
        "SELECT m.id, m.event_timestamp, m.received_at, m.source_message_id"
        " FROM source_message m"
        " LEFT JOIN bucket_message bm ON bm.message_id = m.id"
        " WHERE m.group_id = ? AND bm.message_id IS NULL",
        (group_id,),
    ).fetchall()
    return [
        (
            int(row["id"]),
            row["event_timestamp"],
            str(row["received_at"]),
            str(row["source_message_id"]),
        )
        for row in rows
    ]


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
    """l for FR-BKT-04: the group's latest member instant (>= start).

    Members are read through the PB-2 effective time (event, else receipt)
    because a placed member's raw ``event_timestamp`` may be unusable while
    its receipt time placed it; the stored event timestamp is not rewritten.
    """
    rows = connection.execute(
        "SELECT m.id, m.event_timestamp, m.received_at FROM bucket_message bm"
        " JOIN source_message m ON m.id = bm.message_id"
        " WHERE bm.bucket_id = ?",
        (bucket_id,),
    ).fetchall()
    latest = start
    for row in rows:
        outcome = _effective_time(
            row["event_timestamp"], str(row["received_at"]), int(row["id"])
        )
        if isinstance(outcome, SkippedMessage):
            # Assignment only places messages with a usable effective time;
            # never guess a member time here.
            continue
        member = outcome[0]
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


def _closed_ends(
    connection: sqlite3.Connection, group_id: int
) -> list[tuple[datetime, int]]:
    rows = connection.execute(
        "SELECT id, end_event_timestamp FROM bucket"
        " WHERE group_id = ? AND processing_state = 'CLOSED'"
        " AND end_event_timestamp IS NOT NULL",
        (group_id,),
    ).fetchall()
    return [
        (parse_event_time(str(row["end_event_timestamp"])), int(row["id"]))
        for row in rows
    ]


def _late_predecessor(
    connection: sqlite3.Connection, group_id: int, event: datetime
) -> int | None:
    """PB-7: one hop to the latest CLOSED bucket whose end is at or before ``t``.

    No fallback chain and no link for a non-late bucket: buckets are processing
    boundaries, not conversations (BR-05, FR-BKT-09/10).
    """
    qualified = [pair for pair in _closed_ends(connection, group_id) if pair[0] <= event]
    if not qualified:
        return None
    return max(qualified)[1]


def _joins_a_closed_bucket(
    connection: sqlite3.Connection,
    group_id: int,
    event: datetime,
    params: BucketParams,
) -> bool:
    """True when the message would have joined a bucket that is already CLOSED.

    PB-1's late rule: the message is late only if the bucket it would have
    joined has already closed (closure is recorded on the bucket itself). The
    closed bucket's own membership is historical and never rewritten.
    """
    rows = connection.execute(
        "SELECT id, start_event_timestamp FROM bucket"
        " WHERE group_id = ? AND processing_state = 'CLOSED'",
        (group_id,),
    ).fetchall()
    for row in rows:
        bucket_id = int(row["id"])
        start = parse_event_time(str(row["start_event_timestamp"]))
        if start > event:
            continue
        last = _last_member_time(connection, bucket_id, start)
        if _joins(event, start, last, params):
            return True
    return False


def _clock_boundary(
    connection: sqlite3.Connection, group_id: int
) -> datetime | None:
    """The group's durable clock boundary, or None before the first advance."""
    row = connection.execute(
        "SELECT clock_at FROM source_group_clock WHERE group_id = ?", (group_id,)
    ).fetchone()
    if row is None:
        return None
    return parse_event_time(str(row["clock_at"]))


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


def _place(
    connection: sqlite3.Connection, bucket_id: int, message_id: int, basis: str
) -> None:
    """Record membership and the PB-2 basis of the time that placed it."""
    _add_membership(connection, bucket_id, message_id)
    connection.execute(
        "UPDATE source_message SET time_basis = ? WHERE id = ?", (basis, message_id)
    )


def _mark_late(connection: sqlite3.Connection, message_id: int) -> None:
    connection.execute(
        "UPDATE source_message SET flag_late = 1 WHERE id = ? AND flag_late = 0",
        (message_id,),
    )


def _split_batch(
    connection: sqlite3.Connection,
    group_id: int,
    clock: datetime | None,
    params: BucketParams,
) -> tuple[list[tuple[datetime, int, str]], tuple[SkippedMessage, ...]]:
    """Select each unassigned message's effective time (PB-2, FR-BKT-03).

    The returned sequence is ordered by ``(effective time, source message ID)``
    and carries the basis to record. ``skipped`` is the observable report:
    ``EVENT_TIME_UNUSABLE`` entries were still placed via receipt time; the
    other reasons were left unassigned.

    PB-1: when a clock row exists, only messages with ``t + C <= clock`` are
    considered; the rest stay unassigned for the next tick and are not errors.
    Without a clock row the pre-clock assignment contract applies (documented
    in :func:`assign_messages`).
    """
    valid: list[tuple[datetime, int, str, str]] = []
    skipped: list[SkippedMessage] = []
    grace = timedelta(seconds=params.close_grace_c)
    for message_id, event_timestamp, received_at, source_message_id in _unassigned_messages(
        connection, group_id
    ):
        outcome = _effective_time(event_timestamp, received_at, message_id)
        if isinstance(outcome, SkippedMessage):
            skipped.append(outcome)
            continue
        effective, basis, receipt_fallback = outcome
        if clock is not None and effective + grace > clock:
            continue
        if receipt_fallback:
            skipped.append(
                SkippedMessage(
                    message_id=message_id, reason=SkipReason.EVENT_TIME_UNUSABLE
                )
            )
        valid.append((effective, message_id, source_message_id, basis))
    # FR-BKT-03: order by (event timestamp, source message ID). This single
    # sort is the composition decision; re-sorting by internal id would
    # silently override the source-message tie-break.
    valid.sort(key=lambda item: (item[0], item[2]))
    ordered = [(effective, message_id, basis) for effective, message_id, _, basis in valid]
    return ordered, tuple(skipped)


def _assign_group(
    connection: sqlite3.Connection, group_id: int, params: BucketParams
) -> tuple[AssignmentResult, bool]:
    assigned: list[tuple[int, int]] = []
    late_ids: list[int] = []
    born_closed: list[int] = []
    with transaction(connection):
        # The clock and the legacy progress watermark are read once inside the
        # transaction; assignment never writes either (FR-BKT-04/05, PB-1).
        clock = _clock_boundary(connection, group_id)
        progress = _read_progress(connection, group_id)
        ordered, skipped = _split_batch(connection, group_id, clock, params)
        if not ordered:
            return AssignmentResult(skipped=skipped), False
        for event, message_id, basis in ordered:
            current = _current_open(connection, group_id)
            if clock is not None:
                # PB-1 late rule: only a bucket that has already closed makes
                # the message late; the receipt-time progress boundary is not
                # an event-time watermark and is never used as one.
                late = current is not None and event < current[1]
                if not late and current is None:
                    late = _joins_a_closed_bucket(connection, group_id, event, params)
            else:
                late = (progress is not None and event <= progress) or (
                    current is not None and event < current[1]
                )
            if late:
                end = _computed_end(event, event, params)
                grace = timedelta(seconds=params.close_grace_c)
                if clock is not None:
                    born = end + grace <= clock
                else:
                    born = progress is not None and end + grace <= progress
                predecessor = _late_predecessor(connection, group_id, event)
                bucket_id = _create_bucket(
                    connection,
                    group_id=group_id,
                    start=event,
                    end=end if born else None,
                    state="CLOSED" if born else "OPEN",
                    predecessor=predecessor,
                    params=params,
                )
                _place(connection, bucket_id, message_id, basis)
                _mark_late(connection, message_id)
                late_ids.append(message_id)
                if born:
                    born_closed.append(bucket_id)
                assigned.append((message_id, bucket_id))
                continue
            if current is None:
                bucket_id = _create_bucket(
                    connection,
                    group_id=group_id,
                    start=event,
                    end=None,
                    state="OPEN",
                    predecessor=None,
                    params=params,
                )
                _place(connection, bucket_id, message_id, basis)
                assigned.append((message_id, bucket_id))
                continue
            bucket_id, start = current
            last = _last_member_time(connection, bucket_id, start)
            if _joins(event, start, last, params):
                _place(connection, bucket_id, message_id, basis)
                assigned.append((message_id, bucket_id))
            else:
                successor_id = _create_bucket(
                    connection,
                    group_id=group_id,
                    start=event,
                    end=None,
                    state="OPEN",
                    predecessor=None,
                    params=params,
                )
                _place(connection, successor_id, message_id, basis)
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
    No bucket is closed here and progress is never written. When the group has
    a clock row (PB-1), only settled messages (``t + C <= clock``) are
    considered; unsettled messages stay unassigned for the next tick. A group
    with no clock row keeps the pre-clock contract (assign every unassigned
    message) for direct callers and pre-clock databases; the live pipeline
    always advances the clock before assigning. A message whose event time is
    unusable is placed using its receipt time (PB-2,
    ``time_basis='RECEIPT'``) and reported as ``EVENT_TIME_UNUSABLE``; only a
    message with neither usable time stays stored, unassigned and reported.
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


def close_open_buckets_at_progress(
    connection: sqlite3.Connection, *, group_id: int, progress: datetime
) -> tuple[int, ...]:
    """Close every OPEN bucket whose own ``e + C <= progress`` (FR-BKT-05).

    The bucket's persisted ``parameters_used`` is the sole authority for its
    end (§4.1), so a parameter change after creation cannot move an existing
    boundary. Closure order is ``(e, id)`` and the transition is idempotent:
    only buckets still OPEN are updated. The caller must already hold the
    group's single write transaction; this function never opens one, because
    the clock writes the row, the progress boundary and the closures together.
    """
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
        if end + timedelta(seconds=own_params.close_grace_c) <= progress:
            eligible.append((end, bucket_id))
    eligible.sort(key=lambda item: (item[0], item[1]))
    for end, bucket_id in eligible:
        connection.execute(
            "UPDATE bucket SET processing_state = 'CLOSED',"
            " end_event_timestamp = ? WHERE id = ? AND processing_state = 'OPEN'",
            (_format_event_time(end), bucket_id),
        )
        closed.append(bucket_id)
    return tuple(closed)


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
        closed = close_open_buckets_at_progress(
            connection, group_id=group_id, progress=new_progress
        )
    return ProgressResult(
        accepted=True,
        regressed=False,
        accepted_progress=_format_event_time(new_progress),
        closed_bucket_ids=closed,
    )
