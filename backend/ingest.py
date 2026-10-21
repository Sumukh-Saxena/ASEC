"""Normalized-event ingestion for ASEC (Slice 3.1).

This module is the backend's entry point for normalized message records
produced at the adapter boundary (FR-ING-01, EIR-01). It validates each event
against the storage-domain contracts of migrations 0001/0002/0012, then
persists the message, its mention evidence (0012) and any embedded reaction
evidence (0002) inside one transaction (FR-ING-02..07, FR-ING-05). Reaction
additions or removals arriving after the original message are recorded
separately by :func:`ingest_reaction` (FR-ING-07).

Storage-domain decisions recorded with this slice (owner-approved 2026-10-20,
docs/PHASE2_PLAN.md, Slice 3.1 entry):

- D-1 A: an event from a Discord channel with no ``source_group`` row is
  stored after auto-registering the group with ``vertical_id NULL``; the
  message is flagged UNMAPPED (FR-ING-09).
- A1: ``event_timestamp`` is stored verbatim; format validation happens
  downstream (bucketing), never at ingestion.
- A2: when the adapter omits ``received_at``, the backend stamps its own
  UTC receipt time; a supplied value is stored verbatim (FR-ING-03).
- A3: ``message_mention.position`` is stored only as supplied (NULL when
  absent); positions are never inferred.
- A4: ``raw_metadata`` is stored verbatim when supplied as text, or
  serialized deterministically (``json.dumps(..., sort_keys=True)``) when
  supplied as a mapping.

Validation happens before any write, so a rejected event leaves no trace —
not even D-1's auto-registration. All writes of one event run in a single
transaction from :mod:`backend.db.connection`, so a failure cannot leave a
partially persisted event (FR-ING-05, NFR-REL-04). Reactions are evidence
only (FR-ING-07, BR-09): they never create, change or complete a proposal or
task. Discord content and metadata are untrusted data and are never
interpreted (AGENTS.md); masking happens at the pre-AI boundary, not here —
originals are stored unchanged (AC-21, first half).

Batch replay (:func:`ingest_events`) applies the same per-record transaction
discipline: partial success per the owner decision of 2026-10-21
(docs/PHASE2_PLAN.md §7/§11.2) — per-record CREATED/DUPLICATE/REJECTED
outcomes, record-local rollback, and a loud systemic abort that never
reports an incomplete batch as successful.
"""

from __future__ import annotations

import enum
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from backend.db.connection import transaction


@dataclass(frozen=True)
class MentionEvent:
    """One mention carried by an event: opaque Discord user ID (EIR-03)."""

    mentioned_discord_user_id: str
    position: int | None = None


@dataclass(frozen=True)
class ReactionEvent:
    """One reaction record; ``direction`` is the adapter-supplied token."""

    reactor_discord_user_id: str
    emoji: str
    direction: str
    reacted_at: str | None = None


@dataclass(frozen=True)
class MessageEvent:
    """A normalized message record from the adapter boundary (FR-ING-01/02).

    Textual values are stored verbatim; optional fields stay ``None`` when
    absent rather than being guessed (DR-04). ``raw_metadata`` is either
    supplied text or a mapping that ingestion serializes deterministically
    (A4). ``mentions`` and ``reactions`` are the evidence «embedded» in the
    original delivery; later reaction events use :func:`ingest_reaction`.
    """

    source: str
    source_message_id: str
    discord_channel_id: str
    sender_discord_user_id: str | None = None
    event_timestamp: str | None = None
    received_at: str | None = None
    content: str | None = None
    reply_reference: str | None = None
    raw_metadata: str | Mapping[str, object] | None = None
    mentions: tuple[MentionEvent, ...] = ()
    reactions: tuple[ReactionEvent, ...] = ()


@dataclass(frozen=True)
class StoreResult:
    """Outcome of one :func:`ingest_message` call.

    ``created`` is False for a duplicate delivery (FR-ING-04, NFR-CON-01):
    the existing message identity is returned and nothing is written again.
    """

    message_id: int
    created: bool


class IngestionError(Exception):
    """Base class for failures reported by this module."""


class InvalidMessageEventError(IngestionError):
    """The normalized event fails a storage-domain contract before any write.

    Raised for malformed required identifiers, blank or unsupported values
    that would violate the 0001/0002/0012 CHECK contracts, and errors
    serializing ``raw_metadata``. No database writes have happened.
    """


class RecordStatus(enum.Enum):
    """Per-record batch outcome vocabulary (owner decision 2026-10-21).

    Replays are partial-success batches (docs/PHASE2_PLAN.md §7): one
    transaction per record, exactly the live path.
    """

    CREATED = "CREATED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class RecordOutcome:
    """Outcome of one record of an :func:`ingest_events` batch.

    ``index`` is the record's position in the batch; ``message_id`` is the
    durable row's id for CREATED/DUPLICATE and exactly ``None`` for
    REJECTED; ``error`` is ``None`` unless REJECTED, where it carries the
    reason prefixed with the record position ("``record <index>: …``").
    """

    index: int
    status: RecordStatus
    message_id: int | None
    error: str | None


class IngestionBatchError(IngestionError):
    """A batch was aborted by a failure that is not record-specific.

    Raised instead of returning any result object so an incomplete batch can
    never be read as successful durable ingestion. ``outcomes`` carries the
    results of every record processed before the abort — those records were
    committed durably, record by record — and the original error is chained
    (``raise … from``).
    """

    def __init__(self, outcomes: tuple[RecordOutcome, ...], detail: str) -> None:
        super().__init__(detail)
        self.outcomes = outcomes


@dataclass(frozen=True)
class _ValidatedEvent:
    """Column-ready values of one event; produced before any write."""

    source: str
    source_message_id: str
    discord_channel_id: str
    sender_discord_user_id: str | None
    event_timestamp: str | None
    received_at: str
    content: str | None
    reply_reference: str | None
    raw_metadata: str | None
    mentions: tuple[tuple[str, int | None], ...]
    reactions: tuple[tuple[str, str, str, str | None], ...]


def _require_non_blank(value: str | None, label: str) -> str:
    """The 0002-style CHECK ``length(trim(v)) > 0``: non-blank, kept verbatim."""
    if value is None or value.strip() == "":
        raise InvalidMessageEventError(f"{label} must be a non-blank value")
    return value


def _require_trimmed_non_blank(value: str | None, label: str) -> str:
    """The CHECK ``v = trim(v) AND length(v) > 0``: no surrounding whitespace."""
    checked = _require_non_blank(value, label)
    if checked != checked.strip():
        raise InvalidMessageEventError(f"{label} must not have surrounding whitespace")
    return checked


def _require_position(value: int | None) -> int | None:
    """The 0012 CHECK ``position IS NULL OR position >= 0``, as supplied (A3)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidMessageEventError("mention position must be a non-negative integer or None")
    return value


def _require_content(value: str | None) -> str | None:
    """The 0002 CHECK ``content IS NULL OR length(content) > 0`` (whitespace kept)."""
    if value is None:
        return None
    if len(value) == 0:
        raise InvalidMessageEventError("content, when supplied, must not be empty")
    return value


def _require_raw_metadata(value: str | Mapping[str, object] | None) -> str | None:
    """A4: text verbatim, Mapping serialized deterministically, else rejected."""
    if value is None:
        return None
    if isinstance(value, str):
        return _require_non_blank(value, "raw_metadata")
    if isinstance(value, Mapping):
        try:
            return json.dumps(dict(value), sort_keys=True)
        except (TypeError, ValueError) as error:
            raise InvalidMessageEventError(
                f"raw_metadata is not JSON-serializable: {error}"
            ) from error
    raise InvalidMessageEventError("raw_metadata must be text or a mapping")


def _validated_mentions(
    mentions: tuple[MentionEvent, ...],
) -> tuple[tuple[str, int | None], ...]:
    return tuple(
        (
            _require_non_blank(mention.mentioned_discord_user_id, "mentioned_discord_user_id"),
            _require_position(mention.position),
        )
        for mention in mentions
    )


def _validated_reactions(
    reactions: tuple[ReactionEvent, ...],
) -> tuple[tuple[str, str, str, str | None], ...]:
    """Validate reaction evidence against the 0002 CHECKs, verbatim."""
    validated: list[tuple[str, str, str, str | None]] = []
    for reaction in reactions:
        reacted_at = (
            None
            if reaction.reacted_at is None
            else _require_non_blank(reaction.reacted_at, "reacted_at")
        )
        validated.append(
            (
                _require_non_blank(reaction.reactor_discord_user_id, "reactor_discord_user_id"),
                _require_non_blank(reaction.emoji, "emoji"),
                _require_trimmed_non_blank(reaction.direction, "direction"),
                reacted_at,
            )
        )
    return tuple(validated)


def _validate(event: MessageEvent) -> _ValidatedEvent:
    """Whole-event validation (A1/A3: verbatim; A2: stamp when absent)."""
    received_at = (
        datetime.now(UTC).isoformat()
        if event.received_at is None
        else _require_non_blank(event.received_at, "received_at")
    )
    return _ValidatedEvent(
        source=_require_trimmed_non_blank(event.source, "source"),
        source_message_id=_require_non_blank(event.source_message_id, "source_message_id"),
        discord_channel_id=_require_non_blank(event.discord_channel_id, "discord_channel_id"),
        sender_discord_user_id=(
            None
            if event.sender_discord_user_id is None
            else _require_non_blank(event.sender_discord_user_id, "sender_discord_user_id")
        ),
        event_timestamp=(
            None
            if event.event_timestamp is None
            else _require_non_blank(event.event_timestamp, "event_timestamp")
        ),
        received_at=received_at,
        content=_require_content(event.content),
        reply_reference=(
            None
            if event.reply_reference is None
            else _require_non_blank(event.reply_reference, "reply_reference")
        ),
        raw_metadata=_require_raw_metadata(event.raw_metadata),
        mentions=_validated_mentions(event.mentions),
        reactions=_validated_reactions(event.reactions),
    )


def _ensure_group(
    connection: sqlite3.Connection, discord_channel_id: str
) -> tuple[int, bool]:
    """Resolve the channel's source_group, registering it when unknown (D-1 A).

    Returns ``(group_id, has_vertical)``; ``has_vertical`` is exactly the
    UNMAPPED flag of FR-ING-09. Assumes the caller runs inside a transaction:
    ``BEGIN IMMEDIATE`` already serializes writers, so the in-branch retry
    below is a defensive path (reachable only if the connection were driven
    outside :func:`transaction`).
    """
    row = connection.execute(
        "SELECT id, vertical_id FROM source_group WHERE discord_channel_id = ?",
        (discord_channel_id,),
    ).fetchone()
    if row is not None:
        return int(row["id"]), row["vertical_id"] is not None
    try:
        cursor = connection.execute(
            "INSERT INTO source_group (discord_channel_id, vertical_id)"
            " VALUES (?, NULL)",
            (discord_channel_id,),
        )
    except sqlite3.IntegrityError:
        # Another writer registered the same channel first; reuse its row.
        row = connection.execute(
            "SELECT id, vertical_id FROM source_group WHERE discord_channel_id = ?",
            (discord_channel_id,),
        ).fetchone()
        if row is None:
            raise
        return int(row["id"]), row["vertical_id"] is not None
    return int(cursor.lastrowid or 0), False


def _sender_person_id(
    connection: sqlite3.Connection, sender_discord_user_id: str | None
) -> int | None:
    """FR-ING-10: roster membership by Discord ID, or None (unknown sender)."""
    if sender_discord_user_id is None:
        return None
    row = connection.execute(
        "SELECT id FROM person WHERE discord_user_id = ?", (sender_discord_user_id,)
    ).fetchone()
    return None if row is None else int(row["id"])


def _is_unique_duplicate(error: sqlite3.IntegrityError) -> bool:
    """True only for the ``UNIQUE (source, source_message_id)`` conflict.

    Any other integrity error (foreign keys, other constraints) must surface
    unchanged, never as an idempotent no-op.
    """
    message = str(error)
    if "UNIQUE constraint failed" not in message:
        return False
    return "source_message.source" in message


def _matched_message_id(
    connection: sqlite3.Connection,
    source: str,
    source_message_id: str,
) -> int | None:
    """The stored message id for ``(source, source_message_id)``, if any."""
    row = connection.execute(
        "SELECT id FROM source_message WHERE source = ? AND source_message_id = ?",
        (source, source_message_id),
    ).fetchone()
    return None if row is None else int(row["id"])


def _insert_reaction(
    connection: sqlite3.Connection,
    message_id: int,
    reaction: tuple[str, str, str, str | None],
) -> int:
    reactor, emoji, direction, reacted_at = reaction
    cursor = connection.execute(
        "INSERT INTO message_reaction"
        " (message_id, reactor_discord_user_id, emoji, direction, reacted_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (message_id, reactor, emoji, direction, reacted_at),
    )
    return int(cursor.lastrowid or 0)


def _persist_validated_message(
    connection: sqlite3.Connection, validated: _ValidatedEvent
) -> StoreResult:
    """Insert one already-validated event and its evidence, caller's transaction.

    Shared by live ingestion (:func:`ingest_message`) and replay
    (:func:`ingest_events`). The caller must be inside a ``transaction()``.
    The duplicate pre-check runs before ``_ensure_group`` so a redelivery is a
    pure no-op — not even a D-1 channel registration can appear as a side
    effect; the UNIQUE-conflict conversion stays as the defensive fallback for
    writers racing outside this serialization point.
    """
    existing = _matched_message_id(
        connection, validated.source, validated.source_message_id
    )
    if existing is not None:
        return StoreResult(message_id=existing, created=False)
    group_id, has_vertical = _ensure_group(connection, validated.discord_channel_id)
    sender_person_id = _sender_person_id(connection, validated.sender_discord_user_id)
    try:
        cursor = connection.execute(
            "INSERT INTO source_message"
            " (source, source_message_id, group_id, sender_person_id,"
            "  sender_discord_user_id, event_timestamp, received_at, content,"
            "  reply_reference, raw_metadata, flag_unmapped, flag_unknown_sender)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                validated.source,
                validated.source_message_id,
                group_id,
                sender_person_id,
                validated.sender_discord_user_id,
                validated.event_timestamp,
                validated.received_at,
                validated.content,
                validated.reply_reference,
                validated.raw_metadata,
                0 if has_vertical else 1,
                1 if sender_person_id is None else 0,
            ),
        )
    except sqlite3.IntegrityError as error:
        if not _is_unique_duplicate(error):
            raise
        existing = _matched_message_id(
            connection, validated.source, validated.source_message_id
        )
        if existing is None:
            raise
        return StoreResult(message_id=existing, created=False)
    message_id = int(cursor.lastrowid or 0)
    for mention_user_id, mention_position in validated.mentions:
        connection.execute(
            "INSERT INTO message_mention"
            " (message_id, mentioned_discord_user_id, position) VALUES (?, ?, ?)",
            (message_id, mention_user_id, mention_position),
        )
    for reaction in validated.reactions:
        _insert_reaction(connection, message_id, reaction)
    return StoreResult(message_id=message_id, created=True)


def ingest_message(
    connection: sqlite3.Connection, event: MessageEvent
) -> StoreResult:
    """Validate ``event`` and persist it with its initial evidence atomically.

    One transaction covers the D-1 group auto-registration (when needed), the
    ``source_message`` row and all embedded mention/reaction rows; a failure
    rolls everything back (FR-ING-05, NFR-REL-04). A duplicate delivery (same
    ``source`` + ``source_message_id``) returns the existing message identity
    with ``created=False`` and writes nothing — not even the channel
    registration of an unknown-channel redelivery — so evidence is never
    duplicated (FR-ING-04, NFR-CON-01). Reactions stored here (and later via
    :func:`ingest_reaction`) are evidence only (FR-ING-07, BR-09).

    Raises :class:`InvalidMessageEventError` for any event that would violate
    the storage contracts (no writes happen in that case), and lets unrelated
    sqlite errors surface unchanged.
    """
    validated = _validate(event)
    with transaction(connection):
        return _persist_validated_message(connection, validated)


def ingest_events(
    connection: sqlite3.Connection, events: Sequence[MessageEvent]
) -> tuple[RecordOutcome, ...]:
    """Replay records through the same live ingestion path (FR-ING-08).

    FR-ING-08 exists "for replay and testing only": every record passes
    through live-message validation, flag semantics and idempotency, and
    replay introduces no store effects of its own. Batch semantics follow
    the owner's partial-success decision (2026-10-21; docs/PHASE2_PLAN.md
    §7/§11.2) — **one ``transaction()`` per record**, exactly the live
    path, so independent records never fail together:

    - ``CREATED``: a new durable message row; its D-1 channel registration
      (when needed) and all embedded evidence commit inside the same
      transaction.
    - ``DUPLICATE``: the ``(source, source_message_id)`` identity is already
      stored; the existing id is returned and nothing is written again.
    - ``REJECTED``: a record-specific failure — validation, or a
      persistence-time constraint violation. That record's transaction
      rolled back wholly (no side effects, not even channel registration).
      Records before and after a rejected record are unaffected (P-2).
    - A failure that is **not** record-specific (lock timeout, disk full,
      missing table, any other unexpected error) aborts the batch and
      raises :class:`IngestionBatchError` carrying the durable prefix of
      outcomes with the original error chained — a systemic failure is
      never reported as successful durable ingestion. Interrupts
      (``KeyboardInterrupt``/``SystemExit``) propagate unchanged; their
      record is rolled back by ``transaction()``.

    Retrying a partially successful batch is idempotent: stored identities
    come back as ``DUPLICATE`` with the same message id, rejected records
    are re-evaluated, and a corrected redelivery can be created. Records
    lacking second-level timestamps, message IDs or reactions stay as
    missing where the store allows missing (the SRS marks such exports
    "indicative results only"); nothing is fabricated.

    Returns one outcome per processed record in batch order
    (``outcome.index`` is the record's position, outcomes[0..len-1]); an
    empty batch is a no-op returning ``()``. Record ``i`` of the normal
    result describes record ``i`` — the ordering return of the superseded
    all-or-nothing contract carries into ``RecordOutcome.index``.
    """
    outcomes: list[RecordOutcome] = []
    for index, event in enumerate(events):
        try:
            validated = _validate(event)
        except InvalidMessageEventError as error:
            outcomes.append(
                RecordOutcome(
                    index=index,
                    status=RecordStatus.REJECTED,
                    message_id=None,
                    error=f"record {index}: {error}",
                )
            )
            continue
        try:
            with transaction(connection):
                result = _persist_validated_message(connection, validated)
            outcomes.append(
                RecordOutcome(
                    index=index,
                    status=RecordStatus.CREATED
                    if result.created
                    else RecordStatus.DUPLICATE,
                    message_id=result.message_id,
                    error=None,
                )
            )
        except sqlite3.IntegrityError as error:
            # The constraint binds to this record's data and transaction()
            # already rolled the record back wholly (message, D-1
            # registration and evidence). Record-specific → REJECTED and
            # the batch continues (owner decision P-2).
            outcomes.append(
                RecordOutcome(
                    index=index,
                    status=RecordStatus.REJECTED,
                    message_id=None,
                    error=f"record {index}: {error}",
                )
            )
        except Exception as error:
            # Not record-specific: abort loudly. The current record was
            # rolled back by transaction(); already-processed records are
            # durably committed and reported in the prefix.
            raise IngestionBatchError(
                tuple(outcomes),
                f"record {index}: replay aborted by a non-record-specific error: {error}",
            ) from error
    return tuple(outcomes)


def ingest_reaction(
    connection: sqlite3.Connection, reaction: ReactionEvent, message_id: int
) -> int:
    """Record one later-arriving reaction as evidence for ``message_id``.

    FR-ING-07: the reactor, emoji, direction (as supplied by the adapter) and
    time are stored verbatim on the message; evidence rows are append-only
    (the 0002 migration defines no reaction uniqueness). A reaction never
    changes authoritative state (BR-09). An unknown ``message_id`` fails
    through the store's foreign-key contract (``sqlite3.IntegrityError``)
    inside a transaction, leaving nothing written.

    Returns the id of the stored ``message_reaction`` row.
    """
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        raise InvalidMessageEventError("message_id must be a positive integer")
    reactor, emoji, direction, reacted_at = _validated_reactions((reaction,))[0]
    with transaction(connection):
        return _insert_reaction(connection, message_id, (reactor, emoji, direction, reacted_at))
