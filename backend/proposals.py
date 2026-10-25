"""Proposal-generation service (Phase 5): the executable FR-PRP stage.

Same architecture as :mod:`backend.relevance`, one stage later (backend
contract per the §4 data flow): the whole per-bucket request is composed
and masked by the backend (EIR-03), the generator is a stateless injected
adapter (:class:`ProposalGenerator`, AI-01) that never touches the
database, and only backend-validated writes become authoritative state.
The backend owns everything

- request composition with opaque per-request mention handles (C1):
  handles ``@m1, @m2, …`` assign in order of first appearance scanning
  bucket members by ascending message id; real Discord identifiers never
  enter the dispatch (sender display names only — even the channel id is
  not part of the request; reconstruction goes through ``bucket_id``),
- three-tier validation of the untrusted response (FR-PRP-13, AI-02)
  with documented Tier-2 reason codes and the raw-response retention of
  the owner's C2 correction,
- storage-time assignee resolution (FR-PRP-10, AC-11) restricted to the
  source vertical's name space (DC-6) and the mention evidence map (C1),
- one append-only transaction per attempt (budget, supersede, latency,
  and the 0014 artifact; NFR-REL-01/04, NFR-AUD-02),
- deterministic pass orchestration with the approved Phase 4 budget /
  override pattern (NFR-REL-03/07), and
- read-only inventory/backlog helpers (C4, NFR-REL-06).

Deliberately absent: bucket lifecycle transitions (buckets stay PROCESSED,
DC-1), task/announcement/notification writes (FR-PRP-16/FR-PRP-17), any
`FAILED_PERMANENT` write (two-valued writer), and any comparison against
SRS latency targets (NFR-PERF-01: recording only, NFR-PERF-02).

Stage tokens are free text (the SRS defines no vocabulary; 0004):
primary ``PROPOSAL_GENERATION``, fallback
``PROPOSAL_FALLBACK_GENERATION``. Latency metric labels are free text per
the same rule: ``PROPOSAL_LATENCY`` (adapter call alone) and
``PROPOSAL_STAGE_DELAY`` (C5: this attempt's finish minus the latest
SUCCEEDED relevance attempt's finish — the wall-clock instant of bucket
closure is not persisted by 0003, so the nearest stored post-close
timestamp is the earliest available anchor; skipped honestly when no
parseable anchor exists; never negative).

Owner decisions recorded with this batch (docs/PHASE5_PLAN.md §12,
owner-vetoable where marked): DC-1..DC-16 as recorded there.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from backend.bucketing import parse_event_time
from backend.db.connection import transaction
from backend.masking import mask_pii
from backend.relevance import (
    MASKING_RULESET_VERSION,
    STAGE_RELEVANCE_CLASSIFICATION,
    STAGE_RELEVANCE_FALLBACK_CLASSIFICATION,
)

__all__ = [
    "DEFAULT_PROPOSAL_ATTEMPTS_PER_BUCKET",
    "DEFAULT_ELIGIBLE_ROLE_TOKENS",
    "LATENCY_METRIC_LABEL",
    "MAX_RAW_RESPONSE_BYTES",
    "PROPOSAL_PROMPT",
    "PROPOSAL_PROMPT_VERSION",
    "ProposalBacklog",
    "ProposalBucketOutcome",
    "ProposalDraft",
    "ProposalGenerationRequest",
    "ProposalGenerator",
    "ProposalMessage",
    "ProposalOutcome",
    "ProposalPassError",
    "ProposalPassResult",
    "ProposalPolicy",
    "ProposalRequestContext",
    "ProposalResponse",
    "ProposalServiceError",
    "REJECTION_REASON_CODES",
    "STAGE_DELAY_METRIC_LABEL",
    "STAGE_PROPOSAL_FALLBACK_GENERATION",
    "STAGE_PROPOSAL_GENERATION",
    "load_proposal_policy",
    "proposal_backlog",
    "proposal_exhausted_buckets",
    "proposal_pending_buckets",
    "run_proposal_pass",
    "serialize_proposal_request",
]

DEFAULT_PROPOSAL_ATTEMPTS_PER_BUCKET = 3
STAGE_PROPOSAL_GENERATION = "PROPOSAL_GENERATION"
STAGE_PROPOSAL_FALLBACK_GENERATION = "PROPOSAL_FALLBACK_GENERATION"
LATENCY_METRIC_LABEL = "PROPOSAL_LATENCY"
STAGE_DELAY_METRIC_LABEL = "PROPOSAL_STAGE_DELAY"
PROPOSAL_PROMPT_VERSION = "proposal-prompt-v1"
DEFAULT_ELIGIBLE_ROLE_TOKENS = ("AH", "Head")
MAX_ATTEMPT_ERROR_LENGTH = 500  # Attempt error text stays 500 chars (DC-20)
MAX_RAW_RESPONSE_BYTES = 65536  # C2/DC-20 cap for raw_response only

# Documented Tier-2 reason codes (internal audit annotation; not SRS state
# vocabulary — the D1 rule does not apply). Search §4.4 of the plan for the
# meaning of each.
REJECTION_REASON_CODES = (
    "PRIMARY_NOT_CANDIDATE",
    "TYPE_INVALID",
    "MODE_INVALID",
    "TASK_TYPE_INVALID",
    "MODE_ASSIGNEE_CONFLICT",
    "PEOPLE_NEEDED_CONFLICT",
    "PEOPLE_NEEDED_INVALID",
    "FIELD_TYPE_INVALID",
    "FIELD_VALUE_INVALID",
    "ATTRIBUTES_INVALID",
    "DUPLICATE_ATTR_KEY",
    "UNKNOWN_HANDLE",
)

SRC_DEL_RELEVANCE_STAGES = (
    STAGE_RELEVANCE_CLASSIFICATION,
    STAGE_RELEVANCE_FALLBACK_CLASSIFICATION,
)

PROPOSAL_PROMPT = (
    "Use the relevant messages from eligible conferencing volunteers to "
    "draft zero or more proposals of exactly the two kinds the backend will "
    "accept: a task or an announcement, with description exactly when one "
    "is written down — no proposals at all is a valid draft. A task whose "
    "text names someone is EXPLICIT with the named tokens; a task that "
    "names nobody is OPEN, and OPEN tasks never include people. "
    "Announcements carry the NONE assignment mode and never name people. "
    "Use the opaque handles (like @m1) exactly as they appear in the "
    "request; never invent handles, names, counts, or any value that is "
    "missing from the text. Missing stays missing; silence or reactions "
    "are not acceptance."
)


class ProposalServiceError(Exception):
    """Base class for failures reported by this module."""


class UnknownBucketError(ProposalServiceError):
    """No bucket row exists for the given id; nothing was processed."""


class GeneratorContractError(ProposalServiceError):
    """The generator response violates the EIR-03 envelope contract.

    Raised only after the adapter returned: bad shape, missing provenance.
    It is an execution failure (never a valid empty result) and is
    recorded as ``FAILED_RETRYABLE``.
    """


class ProposalPassError(ProposalServiceError):
    """A proposal pass was aborted by a failure that is not bucket-specific.

    Raised instead of returning any result object so an incomplete pass
    can never be read as successful (same posture as RelevancePassError).
    ``outcomes`` carries every per-bucket outcome committed before the
    abort; ``generated`` the bucket ids that already succeeded; the
    original error is chained.
    """

    def __init__(
        self,
        outcomes: tuple[ProposalBucketOutcome, ...],
        generated: tuple[int, ...],
        detail: str,
    ) -> None:
        super().__init__(detail)
        self.outcomes = outcomes
        self.generated = generated


_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
_CUSTOM_EMOJI_RE = re.compile(r"<a?:([a-zA-Z0-9_]+):\d+>")
_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")


@dataclass(frozen=True)
class ProposalMessage:
    """One bucket message as the generator sees it (C1).

    ``sender_*`` fields beyond the display name are excluded on purpose:
    the request carries no Discord identifiers. ``content`` is the
    NFR-SEC-04-masked copy with Discord markup rewritten (DC-18);
    originals never leave the backend (DR-03). ``reply_reference`` is the
    internal message id of the referenced message when in the same bucket,
    otherwise None (DC-19). ``mention_handles`` are per-request opaque handles
    mapped in :attr:`ProposalRequestContext.mention_handle_map` (never sent in
    the dispatch itself).
    """

    message_id: int
    sender_name: str | None
    event_timestamp: str | None
    reply_reference: int | None
    content: str | None
    mention_handles: tuple[str, ...]


@dataclass(frozen=True)
class ProposalGenerationRequest:
    """The model-facing per-bucket request (EIR-03 proposal half, C1/DC-12).

    Contains no real Discord identifiers and no mention handle map:
    mention handles appear opaquely in message content and context.
    Backend-only resolution and audit data live in
    :class:`ProposalRequestContext`.
    """

    bucket_id: int
    vertical_name: str | None
    candidate_messages: tuple[ProposalMessage, ...]
    context_messages: tuple[ProposalMessage, ...]
    instructions: str


@dataclass(frozen=True)
class ProposalRequestContext:
    """Backend-only request context containing model request + audit/resolution data (C1, DC-12).

    The generator receives :attr:`request` (no Discord IDs, no handle map);
    the backend uses :attr:`mention_handle_map` for assignee resolution and
    for serializing the stored 0014 artifact.
    """

    request: ProposalGenerationRequest
    mention_handle_map: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ProposalDraft:
    """One proposal item returned by the adapter (untrusted until
    validated; field defaults keep the SRS's 'missing stored as missing'
    shape — FR-PRP-11)."""

    primary_message_id: int
    ptype: str
    description: str | None = None
    assignment_mode: str = "EXPLICIT"
    proposed_assignees: tuple[str, ...] | None = ()
    people_needed: int | None = None
    people_needed_unlimited: int = 0
    task_type: str = "INTERNAL"
    suggested_responsible_vertical: str | None = None
    affected_verticals: tuple[str, ...] = ()
    attributes: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True)
class ProposalResponse:
    """The adapter's response (EIR-03): proposals plus provenance.

    The backend validates provenance and every item before anything is
    written; field values here are never trusted blindly.
    """

    model_id: str
    model_version: str
    prompt_version: str
    proposals: tuple[ProposalDraft, ...]


class ProposalGenerator(Protocol):
    """Stateless generator boundary (EIR-03, AI-01).

    One call, one bucket, no database access, no authority: the whole
    context is in the request and the whole result in the response.
    """

    def generate(self, request: ProposalGenerationRequest) -> ProposalResponse:
        """Return validated-shape proposals plus provenance for the request."""


class ProposalOutcome(enum.Enum):
    """Per-bucket outcome of one pass.

    ``GENERATED``: a validated response is durably recorded — zero,
    one, or many proposals, always with the 0014 artifact. ``RETRYING``:
    the attempts failed but the retry budget is not yet exhausted; the
    next pass retries. ``EXHAUSTED``: the attempt budget is consumed;
    recovery is an authorized override (:doc:`docs/PHASE5_PLAN.md` §4.7).
    """

    GENERATED = "GENERATED"
    RETRYING = "RETRYING"
    EXHAUSTED = "EXHAUSTED"


@dataclass(frozen=True)
class ProposalBucketOutcome:
    """What happened to one bucket in one pass.

    ``attempt_ids`` are the attempt rows *created this pass* (primary
    first, then fallback) in insertion order. ``proposal_count`` is the
    number of proposals persisted this pass (failures persist none);
    ``rejected_count`` the Tier-2-rejected items recorded on the artifact.
    """

    bucket_id: int
    status: ProposalOutcome
    attempt_ids: tuple[int, ...]
    proposal_count: int = 0
    rejected_count: int = 0


@dataclass(frozen=True)
class ProposalPassResult:
    """Everything one pass durably changed.

    ``generated`` lists bucket ids whose proposal stage succeeded in this
    pass (first-writer-wins and retry outcomes excluded); ``exhausted``
    the bucket ids refused because the stage budget is consumed.
    """

    outcomes: tuple[ProposalBucketOutcome, ...]
    generated: tuple[int, ...] = ()
    exhausted: tuple[int, ...] = ()


@dataclass(frozen=True)
class _AttemptResult:
    """Internal result of one attempt execution.

    ``attempt_id`` is ``None`` exactly when nothing was written: the
    budget guard refused (``superseded`` False) or a competing writer's
    committed SUCCEEDED attempt already exists for this bucket
    (``superseded`` True — first writer wins).
    """

    attempt_id: int | None
    generated: bool = False
    proposal_count: int = 0
    rejected_count: int = 0
    superseded: bool = False


@dataclass(frozen=True)
class ProposalPolicy:
    """Owner-approved eligibility policy (G1/G1b).

    ``eligible_roles`` are matched trimmed and case-insensitively against
    ``person.role``, with ``person.active = 1`` required and the G1b
    source-vertical membership flag off by default (owner may flip the
    default later).
    """

    eligible_roles: tuple[str, ...] = DEFAULT_ELIGIBLE_ROLE_TOKENS
    require_source_vertical_membership: bool = False


_CONF_TRUTHY = frozenset({"1", "true", "yes", "on"})
_CONF_FALSY = frozenset({"0", "false", "no", "off", ""})


class _ConfigLike(Protocol):
    def get(self, key: str, default: str | None = None) -> str | None: ...


def load_proposal_policy(config: _ConfigLike | None = None) -> ProposalPolicy:
    """Owner-approved config surface (G1/G1b), read-only.

    ``PROPOSAL_ELIGIBLE_ROLES``: comma-separated role tokens (blank or
    unset keeps the ``AH, Head`` default; a string with no token fails
    loud). ``PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP``: ``1/true/yes/
    on`` enables it, ``0/false/no/off`` or unset (empty) disables it; any
    other value fails loud.
    """
    roles: tuple[str, ...] = DEFAULT_ELIGIBLE_ROLE_TOKENS
    membership = False
    if config is not None:
        raw_roles = config.get("PROPOSAL_ELIGIBLE_ROLES")
        if raw_roles is not None and raw_roles.strip() != "":
            tokens = tuple(token.strip() for token in raw_roles.split(","))
            tokens = tuple(token for token in tokens if token != "")
            if not tokens:
                raise ValueError(
                    "PROPOSAL_ELIGIBLE_ROLES must carry at least one"
                    " non-blank role token"
                )
            roles = tokens
        raw_membership = config.get("PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP")
        if raw_membership is not None:
            lowered = raw_membership.strip().lower()
            if lowered in _CONF_TRUTHY:
                membership = True
            elif lowered in _CONF_FALSY:
                membership = False
            else:
                raise ValueError(
                    "PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP must be a"
                    " known on/off value (1/true/yes/on or 0/false/no/off),"
                    f" got {raw_membership!r}"
                )
    return ProposalPolicy(
        eligible_roles=roles, require_source_vertical_membership=membership
    )


def _utc_now() -> str:
    """Backend-stamped UTC timestamp (the A2 ingestion pattern)."""
    return datetime.now(UTC).isoformat()


def _hash_text(text: str) -> str:
    """Integrity hash over exactly the stored text (approved 0013/0014)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redact_error_text(text: str) -> str:
    """Persist error strings masked and bounded (NFR-SEC-04 reach)."""
    redacted = mask_pii(text)
    if len(redacted) > MAX_ATTEMPT_ERROR_LENGTH:
        return redacted[: MAX_ATTEMPT_ERROR_LENGTH - 3] + "..."
    return redacted


def serialize_proposal_request(
    request: ProposalGenerationRequest | ProposalRequestContext,
    *,
    include_handle_map: bool = False,
    handle_map: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
) -> str:
    """Canonical JSON of the proposal request (sorted keys, compact).

    The **dispatch form** (``include_handle_map=False``) is the exact
    string handed to the adapter: no real Discord identifiers anywhere,
    mention handles only. The **artifact form** (``True``) adds the
    ``mention_handle_map`` member the backend needs to reconstruct the
    resolution input (NFR-AUD-02, C1/DC-12).
    """
    if isinstance(request, ProposalRequestContext):
        model_req = request.request
        hmap: Mapping[str, str] | Sequence[tuple[str, str]] | None = (
            request.mention_handle_map if include_handle_map else None
        )
    else:
        model_req = request
        hmap = handle_map if include_handle_map else None

    payload: dict[str, object] = {
        "bucket_id": model_req.bucket_id,
        "vertical_name": model_req.vertical_name,
        "instructions": model_req.instructions,
        "candidate_messages": [
            _serialized_message(message)
            for message in model_req.candidate_messages
        ],
        "context_messages": [
            _serialized_message(message)
            for message in model_req.context_messages
        ],
    }
    if include_handle_map:
        if hmap is not None:
            if isinstance(hmap, Mapping):
                payload["mention_handle_map"] = dict(sorted(hmap.items()))
            else:
                payload["mention_handle_map"] = dict(sorted(hmap))
        else:
            payload["mention_handle_map"] = {}
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _serialized_message(message: ProposalMessage) -> dict[str, object]:
    return {
        "message_id": message.message_id,
        "sender_name": message.sender_name,
        "event_timestamp": message.event_timestamp,
        "reply_reference": message.reply_reference,
        "content": message.content,
        "mention_handles": list(message.mention_handles),
    }


# =========================================================================
# Discovery and eligibility (FR-PRP-01/02, §4.1, C3/G1/G1b)
# =========================================================================


def _lowered_role_pins(policy: ProposalPolicy) -> tuple[str, ...]:
    """Lower-cased role tokens for SQL matching (G1 matching rule)."""
    return tuple(token.strip().lower() for token in policy.eligible_roles)


def _eligible_person_subquery(
    connection_unused: sqlite3.Connection = None,  # type: ignore[assignment]
    *,
    policy: ProposalPolicy,
    bucket_ref: str,
) -> tuple[str, list[str]]:
    """EXISTS fragment: ≥1 RELEVANT decision from an eligible sender.

    Eligible sender (FR-PRP-02, §1.4, G1): a known ``person`` row with
    ``active = 1`` whose free-text role lower-cases to one of the G1
    tokens; when G1b is on, membership of the bucket's group's source
    vertical binds the sender to that mapping. ``bucket_ref`` is an
    internal SQL reference to the bucket id (``b.id``) embedded verbatim;
    only the token values are parameters.
    """
    del connection_unused
    tokens = _lowered_role_pins(policy)
    holders = ", ".join(["?"] * len(tokens))
    membership = ""
    if policy.require_source_vertical_membership:
        membership = (
            " AND EXISTS (SELECT 1 FROM person_vertical AS epv"
            "             JOIN bucket AS eb2 ON eb2.id = ed.bucket_id"
            "             JOIN source_group AS eg2 ON eg2.id = eb2.group_id"
            "              WHERE epv.person_id = ep.id"
            "                AND epv.vertical_id = eg2.vertical_id)"
        )
    parameters: list[str] = [*tokens]
    sql = (
        "EXISTS (SELECT 1 FROM relevance_decision AS ed"
        "         JOIN source_message AS em ON em.id = ed.message_id"
        "         JOIN person AS ep ON ep.id = em.sender_person_id"
        f"        WHERE ed.bucket_id = {bucket_ref}"
        "          AND ed.decision = 'RELEVANT'"
        "          AND ep.active = 1"
        f"          AND lower(trim(ep.role)) IN ({holders})"
        f"{membership}) "
    )
    return sql, parameters


def _pending_rows(
    connection: sqlite3.Connection, policy: ProposalPolicy
) -> list[tuple[int, int]]:
    """Pending proposal inventory rows: ``(bucket_id, attempts)`` per §4.1.

    A bucket is pending when it is PROCESSED, its group is vertically
    mapped, a relevance-stage SUCCEEDED attempt committed, at least one
    eligible (G1/G1b) RELEVANT decision exists, and no SUCCEEDED
    proposal attempt exists yet (first-writer-wins is state-derived).
    Ordered ascending by id.
    """
    eligibility_sql, eligibility_parameters = _eligible_person_subquery(
        policy=policy, bucket_ref="b.id"
    )
    rows = connection.execute(
        f"""
        SELECT b.id AS bucket_id,
               (SELECT COUNT(*) FROM processing_attempt AS pa
                 WHERE pa.bucket_id = b.id
                   AND pa.stage IN (?, ?)) AS attempts
          FROM bucket AS b
          JOIN source_group AS g ON g.id = b.group_id
         WHERE b.processing_state = 'PROCESSED'
           AND g.vertical_id IS NOT NULL
           AND EXISTS (SELECT 1 FROM processing_attempt AS ra
                        WHERE ra.bucket_id = b.id
                          AND ra.outcome = 'SUCCEEDED'
                          AND ra.stage IN (?, ?))
           AND NOT EXISTS (SELECT 1 FROM processing_attempt AS pa2
                            WHERE pa2.bucket_id = b.id
                              AND pa2.stage IN (?, ?)
                              AND pa2.outcome = 'SUCCEEDED')
           AND {eligibility_sql}
        """,
        (
            STAGE_PROPOSAL_GENERATION,
            STAGE_PROPOSAL_FALLBACK_GENERATION,
            STAGE_RELEVANCE_CLASSIFICATION,
            STAGE_RELEVANCE_FALLBACK_CLASSIFICATION,
            STAGE_PROPOSAL_GENERATION,
            STAGE_PROPOSAL_FALLBACK_GENERATION,
            *eligibility_parameters,
        ),
    ).fetchall()
    return [(int(row["bucket_id"]), int(row["attempts"])) for row in rows]


def _attempt_count(connection: sqlite3.Connection, bucket_id: int) -> int:
    """Committed proposal-stage attempts for ``bucket_id`` (all outcomes).

    Only the two proposal stage tokens count toward the budget (F2
    stage-scoped attempt accounting; other stages must keep theirs).
    """
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM processing_attempt"
        " WHERE bucket_id = ? AND stage IN (?, ?)",
        (
            bucket_id,
            STAGE_PROPOSAL_GENERATION,
            STAGE_PROPOSAL_FALLBACK_GENERATION,
        ),
    ).fetchone()
    return int(row["n"])


def _has_success(connection: sqlite3.Connection, bucket_id: int) -> bool:
    """True when a committed SUCCEEDED proposal attempt exists."""
    row = connection.execute(
        "SELECT 1 FROM processing_attempt"
        " WHERE bucket_id = ? AND stage IN (?, ?)"
        "   AND outcome = 'SUCCEEDED' LIMIT 1",
        (
            bucket_id,
            STAGE_PROPOSAL_GENERATION,
            STAGE_PROPOSAL_FALLBACK_GENERATION,
        ),
    ).fetchone()
    return row is not None


def _bucket_group(
    connection: sqlite3.Connection, bucket_id: int
) -> tuple[int, int, str | None]:
    """(bucket_id, group_id, vertical_name) for ``bucket_id``.

    Absent bucket fails loud; an unmapped group's vertical name stays
    ``None`` — missing stored as missing (DR-04), never a guessed name.
    """
    row = connection.execute(
        "SELECT g.id AS group_id, v.name AS vertical_name"
        "  FROM bucket AS b"
        "  JOIN source_group AS g ON g.id = b.group_id"
        "  LEFT JOIN vertical AS v ON v.id = g.vertical_id"
        " WHERE b.id = ?",
        (bucket_id,),
    ).fetchone()
    if row is None:
        raise UnknownBucketError(f"no bucket with id {bucket_id}")
    return (
        bucket_id,
        int(row["group_id"]),
        None if row["vertical_name"] is None else str(row["vertical_name"]),
    )


def _eligible_candidate_ids(
    connection: sqlite3.Connection,
    bucket_id: int,
    group_id: int,
    policy: ProposalPolicy,
) -> tuple[int, ...]:
    """Message ids of eligible RELEVANT decisions, ascending id (§4.1).

    Same eligibility rule as the discovery query, narrowed to ``members
    of this bucket``: relevance decided RELEVANT, sender person is
    known, active, one of the G1 tokens, and (G1b) a member of the
    group's source vertical.
    """
    tokens = _lowered_role_pins(policy)
    holders = ", ".join(["?"] * len(tokens))
    membership = ""
    if policy.require_source_vertical_membership:
        membership = (
            " AND EXISTS (SELECT 1 FROM person_vertical AS epv"
            "              WHERE epv.person_id = ep.id"
            "                AND epv.vertical_id ="
            "                    (SELECT vertical_id FROM source_group"
            "                      WHERE id = ?))"
        )
    parameters: list[object] = [bucket_id, bucket_id, *tokens]
    if policy.require_source_vertical_membership:
        parameters.append(group_id)
    rows = connection.execute(
        f"""
        SELECT rd.message_id AS message_id
          FROM relevance_decision AS rd
          JOIN source_message AS em ON em.id = rd.message_id
          JOIN person AS ep ON ep.id = em.sender_person_id
         WHERE rd.bucket_id = ?
           AND rd.decision = 'RELEVANT'
           AND rd.message_id IN (SELECT bm.message_id FROM bucket_message AS bm
                                  WHERE bm.bucket_id = ?)
           AND ep.active = 1
           AND lower(trim(ep.role)) IN ({holders})
           {membership}
         ORDER BY rd.message_id
        """,
        parameters,
    ).fetchall()
    return tuple(int(row["message_id"]) for row in rows)


def _bucket_entries(
    connection: sqlite3.Connection, bucket_id: int
) -> tuple[tuple[ProposalMessage, ...], dict[str, str]]:
    """Whole-bucket context entries + the C1 handle map.

    Messages ordered by ascending id; content masked only in the request
    copy (:func:`backend.masking.mask_pii`) — stored content never
    touched (DR-03, AC-21). Real Discord markup (<@ID>, <@!ID>, <@&ID>,
    <#ID>, <:name:ID>, <a:name:ID>) is rewritten in the request copy
    (C1, DC-18). Raw reply references are replaced with internal message
    IDs of the same bucket, or None (DC-19). Handles ``@m1, @m2, …``
    are assigned in order of first appearance scanning bucket members by
    ascending message id, first from stored message mentions (position,
    then row id) and then from content user mentions (DC-18).
    """
    entries: list[ProposalMessage] = []
    handle_map: dict[str, str] = {}
    next_handle = 1
    handle_for_user: dict[str, str] = {}
    mention_rows = connection.execute(
        "SELECT bm2.message_id AS message_id,"
        "       mm.mentioned_discord_user_id AS user_id,"
        "       mm.position AS position, mm.id AS mention_row_id"
        "  FROM message_mention AS mm"
        "  JOIN bucket_message AS bm2 ON bm2.message_id = mm.message_id"
        " WHERE bm2.bucket_id = ?"
        " ORDER BY mm.message_id,"
        "          (mm.position IS NULL), mm.position, mm.id",
        (bucket_id,),
    ).fetchall()
    handles_by_message: dict[int, list[str]] = {}
    for row in mention_rows:
        user_id = str(row["user_id"])
        handle = handle_for_user.get(user_id)
        if handle is None:
            handle = f"@m{next_handle}"
            next_handle += 1
            handle_for_user[user_id] = handle
            handle_map[handle] = user_id
        handles_by_message.setdefault(int(row["message_id"]), []).append(handle)

    message_rows = connection.execute(
        "SELECT m.id, m.source_message_id, p.name AS sender_name, m.event_timestamp,"
        "       m.reply_reference, m.content"
        "  FROM bucket_message AS bm"
        "  JOIN source_message AS m ON m.id = bm.message_id"
        "  LEFT JOIN person AS p ON p.id = m.sender_person_id"
        " WHERE bm.bucket_id = ?"
        " ORDER BY m.id",
        (bucket_id,),
    ).fetchall()

    internal_id_by_source_id = {
        str(r["source_message_id"]): int(r["id"]) for r in message_rows
    }

    for row in message_rows:
        msg_id = int(row["id"])
        raw_reply = row["reply_reference"]
        resolved_reply: int | None = None
        if raw_reply is not None:
            resolved_reply = internal_id_by_source_id.get(str(raw_reply))

        raw_content = row["content"]
        if raw_content is None:
            content_copy: str | None = None
        else:
            masked = mask_pii(str(raw_content))

            def _replace_user(match: re.Match[str], mid: int = msg_id) -> str:
                nonlocal next_handle
                uid = match.group(1)
                h = handle_for_user.get(uid)
                if h is None:
                    h = f"@m{next_handle}"
                    next_handle += 1
                    handle_for_user[uid] = h
                    handle_map[h] = uid
                msg_handles = handles_by_message.setdefault(mid, [])
                if h not in msg_handles:
                    msg_handles.append(h)
                return h

            masked = _USER_MENTION_RE.sub(_replace_user, masked)
            masked = _ROLE_MENTION_RE.sub("@role", masked)
            masked = _CHANNEL_MENTION_RE.sub("#channel", masked)
            masked = _CUSTOM_EMOJI_RE.sub(r":\1:", masked)
            content_copy = masked

        entries.append(
            ProposalMessage(
                message_id=msg_id,
                sender_name=(
                    None if row["sender_name"] is None else str(row["sender_name"])
                ),
                event_timestamp=(
                    None
                    if row["event_timestamp"] is None
                    else str(row["event_timestamp"])
                ),
                reply_reference=resolved_reply,
                content=content_copy,
                mention_handles=tuple(handles_by_message.get(msg_id, ())),
            )
        )
    return tuple(entries), handle_map


def _proposal_request(
    connection: sqlite3.Connection,
    bucket_id: int,
    policy: ProposalPolicy | None = None,
) -> ProposalRequestContext:
    """Compose the whole per-bucket request (FR-PRP-03, EIR-03, C1, DC-12).

    Candidates are the eligible RELEVANT messages (ordering by id);
    context is the whole bucket. An absent bucket fails loud. Returns
    the backend :class:`ProposalRequestContext` containing the model-facing
    request and the handle map.
    """
    policy = policy if policy is not None else ProposalPolicy()
    _bucket_id, group_id, vertical_name = _bucket_group(connection, bucket_id)
    entries, handle_map = _bucket_entries(connection, bucket_id)
    candidate_ids = _eligible_candidate_ids(
        connection, bucket_id, group_id, policy
    )
    by_id = {entry.message_id: entry for entry in entries}
    candidates = tuple(by_id[cid] for cid in sorted(candidate_ids))
    req = ProposalGenerationRequest(
        bucket_id=bucket_id,
        vertical_name=vertical_name,
        candidate_messages=candidates,
        context_messages=entries,
        instructions=PROPOSAL_PROMPT,
    )
    return ProposalRequestContext(
        request=req,
        mention_handle_map=tuple(sorted(handle_map.items())),
    )


# =========================================================================
# Validation (FR-PRP-05/13, §4.4; C2 reason codes; DC-3/DC-16)
# =========================================================================


def _validated_provenance(value: object, label: str) -> str:
    """EIR-03 provenance checks: non-blank, trimmed, verbatim otherwise."""
    if not isinstance(value, str) or value.strip() == "" or value != value.strip():
        raise GeneratorContractError(
            f"response {label} must be a non-blank trimmed string, got {value!r}"
        )
    return value


def _is_plain_int(value: object) -> bool:
    """True for ints only — bool is never a count (FR-PRP-06)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _tokens_of(value: object) -> tuple[str, ...]:
    """Assignee tokens as a verbatim tuple (validated shape beforehand)."""
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(token for token in value if isinstance(token, str))
    return ()


def _handle_shaped(token: str) -> bool:
    """C1: a token the model could only have invented as an opaque handle."""
    return token.startswith("@m") and token[2:].isdigit()


def _validated_response(
    response: object,
    request: ProposalGenerationRequest,
    handle_map: Mapping[str, str],
) -> tuple[
    tuple[str, str, str],
    tuple[ProposalDraft, ...],
    tuple[dict[str, object], ...],
]:
    """Three-tier structural validation (§4.4) of the untrusted response.

    Tier 1 (envelope): non-blank provenance and an iterable-of-items
    ``proposals`` attribute — violations raise
    :class:`GeneratorContractError` so the whole attempt fails (EIR-03;
    malformed output is an execution failure, never a valid empty
    result). Tier 2 (items): every structurally-wrong item becomes a
    rejection record with a documented reason code — it never becomes a
    proposal row (C2/DC-4) and never blocks its valid siblings.
    Tier 3 (content): anything else is stored as generated.

    Returns (provenance, valid drafts, rejection records). A response
    carrying items but **no valid item** is *not* an empty success
    (DC-3): the caller records a FAILED_RETRYABLE attempt.
    """
    model_id = _validated_provenance(
        getattr(response, "model_id", None), "model_id"
    )
    model_version = _validated_provenance(
        getattr(response, "model_version", None), "model_version"
    )
    prompt_version = _validated_provenance(
        getattr(response, "prompt_version", None), "prompt_version"
    )
    try:
        raw_items = list(getattr(response, "proposals", None))  # type: ignore[arg-type]
    except TypeError as error:
        raise GeneratorContractError(
            "response proposals must be an iterable of proposal items"
        ) from error
    candidate_ids = {message.message_id for message in request.candidate_messages}
    valid: list[ProposalDraft] = []
    rejections: list[dict[str, object]] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, ProposalDraft):
            rejections.append({"item_index": index, "reason_code": "FIELD_TYPE_INVALID"})
            continue
        issue = _validated_draft(item, candidate_ids, handle_map)
        if issue is None:
            valid.append(item)
        else:
            rejections.append({"item_index": index, "reason_code": issue})
    return (model_id, model_version, prompt_version), tuple(valid), tuple(rejections)


def _validated_draft(
    item: ProposalDraft,
    candidate_ids: set[int],
    handle_map: Mapping[str, str],
) -> str | None:
    """Structural item check; a reason code or ``None`` when valid.

    Rules (§4.4): token vocabularies verbatim (0005 CHECKs), the
    FR-PRP-08 shall-never combinations, people-number encodings,
    distinguishability of missing vs blank (DC-16), attribute-shape and
    uniqueness rules, and the C1 UNKNOWN_HANDLE rule (an unknown
    handle-shaped token is a violation, never a guess).
    """
    if not _is_plain_int(item.primary_message_id):
        return "FIELD_TYPE_INVALID"
    if item.primary_message_id not in candidate_ids:
        return "PRIMARY_NOT_CANDIDATE"
    if not isinstance(item.ptype, str) or item.ptype not in (
        "TASK",
        "ANNOUNCEMENT",
    ):
        return "TYPE_INVALID"
    if not isinstance(item.assignment_mode, str) or item.assignment_mode not in (
        "EXPLICIT",
        "OPEN",
        "NONE",
    ):
        return "MODE_INVALID"
    if not isinstance(item.task_type, str) or item.task_type not in (
        "INTERNAL",
        "CROSS_VERTICAL",
    ):
        return "TASK_TYPE_INVALID"
    if item.ptype == "TASK" and item.assignment_mode == "NONE":
        # 0005 CHECK: assignment_mode NONE occurs on announcements only.
        return "MODE_INVALID"
    if item.description is not None and not isinstance(item.description, str):
        return "FIELD_TYPE_INVALID"
    if item.description is not None and str(item.description).strip() == "":
        return "FIELD_VALUE_INVALID"
    if item.people_needed is not None and not _is_plain_int(item.people_needed):
        return "FIELD_TYPE_INVALID"
    if (
        item.people_needed is not None
        and int(item.people_needed) <= 0
    ):
        return "PEOPLE_NEEDED_INVALID"
    if not _is_plain_int(item.people_needed_unlimited):
        return "FIELD_TYPE_INVALID"
    if item.people_needed_unlimited not in (0, 1):
        return "PEOPLE_NEEDED_INVALID"
    if item.people_needed is not None and item.people_needed_unlimited == 1:
        return "PEOPLE_NEEDED_CONFLICT"
    if item.proposed_assignees is not None and not isinstance(
        item.proposed_assignees, (list, tuple)
    ):
        return "FIELD_TYPE_INVALID"
    tokens = _tokens_of(item.proposed_assignees)
    if len(tokens) != len(
        item.proposed_assignees or ()
    ):
        return "FIELD_TYPE_INVALID"
    if any(not isinstance(token, str) or token.strip() == "" for token in tokens):
        return "FIELD_VALUE_INVALID"
    if (item.assignment_mode in ("OPEN", "NONE") or item.ptype == "ANNOUNCEMENT") and tokens:
        return "MODE_ASSIGNEE_CONFLICT"
    if not isinstance(item.affected_verticals, (list, tuple)):
        return "FIELD_TYPE_INVALID"
    if any(
        not isinstance(name, str) or name.strip() == ""
        for name in item.affected_verticals
    ):
        return "FIELD_VALUE_INVALID"
    if item.suggested_responsible_vertical is not None and not isinstance(
        item.suggested_responsible_vertical, str
    ):
        return "FIELD_TYPE_INVALID"
    if (
        item.suggested_responsible_vertical is not None
        and item.suggested_responsible_vertical.strip() == ""
    ):
        return "FIELD_VALUE_INVALID"
    issue = _attributes_issue(item.attributes)
    if issue is not None:
        return issue
    if any(
        _handle_shaped(token) and token not in handle_map for token in tokens
    ):
        return "UNKNOWN_HANDLE"
    return None


def _attributes_issue(
    attributes: tuple[tuple[str, str | None], ...],
) -> str | None:
    """Attribute-shape checks (FR-PRP-07, DR-04): pairs of (key, value)
    with non-blank keys and NULL-or-non-blank values; unique keys."""
    if not isinstance(attributes, (list, tuple)):
        return "ATTRIBUTES_INVALID"
    keys: set[str] = set()
    for entry in attributes:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            return "ATTRIBUTES_INVALID"
        key, value = entry
        if not isinstance(key, str) or key.strip() == "":
            return "FIELD_VALUE_INVALID"
        if value is not None and (
            not isinstance(value, str) or value.strip() == ""
        ):
            return "FIELD_VALUE_INVALID"
        if key in keys:
            return "DUPLICATE_ATTR_KEY"
        keys.add(key)
    return None


# =========================================================================
# Storage-time resolution (FR-PRP-10, AC-11; DC-6)
# =========================================================================


def _vertical_id_by_name(
    connection: sqlite3.Connection, name: str | None
) -> int | None:
    """Exact case-insensitive ``vertical.name`` resolution or ``None``
    (unresolvable names never invent FK rows — DC-7)."""
    if name is None or name.strip() == "":
        return None
    row = connection.execute(
        "SELECT id FROM vertical WHERE lower(name) = lower(?)",
        (name.strip(),),
    ).fetchone()
    return None if row is None else int(row["id"])


def _resolved_assignees(
    connection: sqlite3.Connection,
    proposal_id: int,
    tokens: tuple[str, ...],
    handle_map: Mapping[str, str],
    source_vertical_id: int,
) -> int:
    """Persist ``proposal_assignee`` rows for ``tokens`` (FR-PRP-10).

    MENTION route (C1): a token inside the request handle map resolves
    exactly by the mapped Discord user ID — active flag irrelevant
    (DC-15); a mapped ID without a roster person is FLAGGED. NAME route
    (DC-6): trimmed+case-folded equality against display names of
    ACTIVE roster members of the source vertical — one match RESOLVED,
    several UNRESOLVED, none FLAGGED. Never a guess: implicit-person
    rows are unrepresentable by the 0012 CHECK. Returns the row count.
    """
    created = 0
    for token in tokens:
        name_pin = token in handle_map
        person_id: int | None = None
        route: str | None = None
        resolution: str
        if name_pin:
            mapped_id = str(handle_map[token])
            row = connection.execute(
                "SELECT id FROM person WHERE discord_user_id = ?",
                (mapped_id,),
            ).fetchone()
            if row is not None:
                person_id = int(row["id"])
                route = "MENTION"
                resolution = "RESOLVED"
            else:
                resolution = "FLAGGED"
        else:
            rows = connection.execute(
                "SELECT p.id, p.name FROM person AS p"
                " WHERE p.active = 1"
                "   AND EXISTS (SELECT 1 FROM person_vertical AS pv"
                "                WHERE pv.person_id = p.id"
                "                  AND pv.vertical_id = ?)"
                " ORDER BY p.id",
                (source_vertical_id,),
            ).fetchall()
            target_name = token.strip().casefold()
            matches = [
                row
                for row in rows
                if row["name"] is not None
                and str(row["name"]).strip().casefold() == target_name
            ]
            if len(matches) == 1:
                person_id = int(matches[0]["id"])
                route = "NAME_MATCH"
                resolution = "RESOLVED"
            elif len(matches) > 1:
                resolution = "UNRESOLVED"
            else:
                resolution = "FLAGGED"
        connection.execute(
            "INSERT INTO proposal_assignee"
            " (proposal_id, assignee_text, resolution, person_id, resolved_via)"
            " VALUES (?, ?, ?, ?, ?)",
            (proposal_id, token, resolution, person_id, route),
        )
        created += 1
    return created


def _raw_response_text(response: object) -> str:
    """C2 retention source: the adapter output as evidence text.

    A validated-shape response serializes canonically; anything else (an
    envelope-failure object that reached validation) degrades to its
    repr. The result still goes through the same mask + cap treatment as
    attempt errors before storage.
    """
    if isinstance(response, ProposalResponse):
        try:
            return json.dumps(
                {
                    "model_id": response.model_id,
                    "model_version": response.model_version,
                    "prompt_version": response.prompt_version,
                    "proposals": [
                        {
                            "primary_message_id": draft.primary_message_id,
                            "ptype": draft.ptype,
                            "description": draft.description,
                            "assignment_mode": draft.assignment_mode,
                            "proposed_assignees": (
                                None
                                if draft.proposed_assignees is None
                                else list(draft.proposed_assignees)
                            ),
                            "people_needed": draft.people_needed,
                            "people_needed_unlimited": draft.people_needed_unlimited,
                            "task_type": draft.task_type,
                            "suggested_responsible_vertical":
                                draft.suggested_responsible_vertical,
                            "affected_verticals": list(draft.affected_verticals),
                            "attributes": [
                                [key, value] for key, value in draft.attributes
                            ],
                        }
                        for draft in response.proposals
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except Exception:  # noqa: BLE001 - malformed draft shapes degrade to repr
            return repr(response)
    return repr(response)


def _process_raw_response(response: object) -> tuple[str, str, int]:
    """C2/DC-20 retention: adapter output masked, hashed, and bounded.

    1. Get the raw evidence text (canonical JSON or degraded repr).
    2. Mask PII via mask_pii.
    3. Compute response_sha256 over the FULL masked text BEFORE truncation.
    4. If the UTF-8 byte length exceeds MAX_RAW_RESPONSE_BYTES (65536),
       truncate on a character boundary and set raw_response_truncated = 1,
       otherwise 0.
    Returns (stored_text, full_response_sha256, raw_response_truncated).
    """
    raw = _raw_response_text(response)
    masked = mask_pii(raw)
    full_sha = _hash_text(masked)
    raw_bytes = masked.encode("utf-8")
    if len(raw_bytes) > MAX_RAW_RESPONSE_BYTES:
        truncated = raw_bytes[:MAX_RAW_RESPONSE_BYTES].decode("utf-8", errors="ignore")
        return truncated, full_sha, 1
    return masked, full_sha, 0


def _stored_raw_response(response: object) -> str:
    """The exact ``raw_response`` artifact text: masked and bounded (C2/DC-20)."""
    stored_text, _sha, _truncated = _process_raw_response(response)
    return stored_text


# =========================================================================
# Attempt execution (FR-PRP-12/14, NFR-REL-01/02/04, NFR-PERF-02, C1/C2)
# =========================================================================


def _relevance_delay_anchor(
    connection: sqlite3.Connection, bucket_id: int
) -> str | None:
    """The latest parseable SUCCEEDED relevance-stage finish (DC-14).

    The wall-clock instant of bucket closure is not persisted by 0003, so
    the latest stored relevance finish is the earliest available
    post-close anchor; unparsable stored samples never fabricate one.
    """
    row = connection.execute(
        "SELECT finished_at FROM processing_attempt"
        " WHERE bucket_id = ? AND outcome = 'SUCCEEDED'"
        "   AND stage IN (?, ?)"
        " ORDER BY id DESC LIMIT 1",
        (
            bucket_id,
            STAGE_RELEVANCE_CLASSIFICATION,
            STAGE_RELEVANCE_FALLBACK_CLASSIFICATION,
        ),
    ).fetchone()
    if row is None or row["finished_at"] is None:
        return None
    raw = str(row["finished_at"])
    try:
        parse_event_time(raw)
    except Exception:  # noqa: BLE001 - unparsable anchor: skip honestly
        return None
    return raw


def _stage_delay_seconds(
    connection: sqlite3.Connection, bucket_id: int, finished_at: str
) -> float | None:
    """C5: this attempt's finish minus the relevance anchor (never < 0).

    ``None`` when no parseable anchor exists or the anchor would make the
    delay negative (clock skew — never a fabricated zero).
    """
    anchor = _relevance_delay_anchor(connection, bucket_id)
    if anchor is None:
        return None
    try:
        anchor_at = parse_event_time(anchor).timestamp()
        finished_at_time = parse_event_time(finished_at).timestamp()
    except Exception:  # noqa: BLE001 - unparsable anchor: skip honestly
        return None
    delay = finished_at_time - anchor_at
    if delay < 0.0:
        return None
    return delay


def _persist_proposal(
    connection: sqlite3.Connection,
    bucket_id: int,
    source_vertical_id: int,
    attempt_id: int,
    provenance: tuple[str, str, str],
    draft: ProposalDraft,
    handle_map: Mapping[str, str],
    finished_at: str,
) -> int:
    """Store one validated proposal with its children (FR-PRP-12/07).

    Values are stored verbatim after validation; missing stays missing
    (FR-PRP-11/DR-04). Assignee tokens resolve at storage time (§4.5);
    context links cover every bucket member including the primary
    (FR-PRP-03).
    """
    model_id, model_version, prompt_version = provenance
    tokens = _tokens_of(draft.proposed_assignees)
    assignees_text = ", ".join(tokens) if tokens else None
    suggested_id = _vertical_id_by_name(
        connection, draft.suggested_responsible_vertical
    )
    cursor = connection.execute(
        "INSERT INTO proposal ("
        " attempt_id, bucket_id, primary_message_id, ptype, description,"
        " assignment_mode, proposed_assignees_text, people_needed,"
        " people_needed_unlimited, task_type, source_vertical_id,"
        " suggested_responsible_vertical_id, structurally_valid, generated_at,"
        " model_id, model_version, prompt_version)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (
            attempt_id,
            bucket_id,
            draft.primary_message_id,
            draft.ptype,
            draft.description,
            draft.assignment_mode,
            assignees_text,
            draft.people_needed,
            draft.people_needed_unlimited,
            draft.task_type,
            source_vertical_id,
            suggested_id,
            finished_at,
            model_id,
            model_version,
            prompt_version,
        ),
    )
    proposal_id = int(cursor.lastrowid or 0)
    if tokens and draft.assignment_mode == "EXPLICIT":
        _resolved_assignees(
            connection, proposal_id, tokens, handle_map, source_vertical_id
        )
    for key, value in draft.attributes:
        connection.execute(
            "INSERT INTO proposal_attribute (proposal_id, attr_key, attr_value)"
            " VALUES (?, ?, ?)",
            (proposal_id, key, value),
        )
    for name in draft.affected_verticals:
        vertical_id = _vertical_id_by_name(connection, name)
        if vertical_id is not None:
            connection.execute(
                "INSERT INTO proposal_affected_vertical"
                " (proposal_id, vertical_id) VALUES (?, ?)",
                (proposal_id, vertical_id),
            )
    connection.execute(
        "INSERT INTO proposal_context_message (proposal_id, bucket_id,"
        " message_id)"
        " SELECT ?, ?, message_id FROM bucket_message WHERE bucket_id = ?",
        (proposal_id, bucket_id, bucket_id),
    )
    return proposal_id


def _record_latency(
    connection: sqlite3.Connection,
    attempt_id: int,
    call_seconds: float,
    finished_at: str,
    stage_delay: float | None,
) -> None:
    """Persist the recorded latency rows (NFR-PERF-01/02) — recording only:
    the 30 s/8 s SRS targets are never compared in code."""
    connection.execute(
        "INSERT INTO latency_measurement (processing_attempt_id, metric,"
        " value_seconds, recorded_at) VALUES (?, ?, ?, ?)",
        (attempt_id, LATENCY_METRIC_LABEL, float(call_seconds), finished_at),
    )
    if stage_delay is not None:
        connection.execute(
            "INSERT INTO latency_measurement (processing_attempt_id, metric,"
            " value_seconds, recorded_at) VALUES (?, ?, ?, ?)",
            (
                attempt_id,
                STAGE_DELAY_METRIC_LABEL,
                float(stage_delay),
                finished_at,
            ),
        )


@dataclass(frozen=True)
class _AttemptInputs:
    """Everything one attempt needs before its write transaction."""

    bucket_id: int
    group_id: int
    source_vertical_id: int
    vertical_name: str | None
    attempt_count: int
    budget: int
    context: ProposalRequestContext

    @property
    def request(self) -> ProposalGenerationRequest:
        return self.context.request

    @property
    def handle_map(self) -> dict[str, str]:
        return dict(self.context.mention_handle_map)


def _attempt_inputs(
    connection: sqlite3.Connection,
    bucket_id: int,
    *,
    budget: int,
    policy: ProposalPolicy,
) -> _AttemptInputs:
    """Compose the request and read the pre-call state (one read pass)."""
    _bucket_id, group_id, vertical_name = _bucket_group(connection, bucket_id)
    row = connection.execute(
        "SELECT vertical_id, g.vertical_id FROM source_group AS g"
        " WHERE g.id = ?",
        (group_id,),
    ).fetchone()
    if row is None or row["vertical_id"] is None:
        raise UnknownBucketError(
            f"bucket {bucket_id} has no vertically mapped source group"
        )
    source_vertical_id = int(row["vertical_id"])
    context = _proposal_request(connection, bucket_id, policy)
    return _AttemptInputs(
        bucket_id=bucket_id,
        group_id=group_id,
        source_vertical_id=source_vertical_id,
        vertical_name=vertical_name,
        attempt_count=_attempt_count(connection, bucket_id),
        budget=budget,
        context=context,
    )


def _run_attempt(
    connection: sqlite3.Connection,
    inputs: _AttemptInputs,
    generator: ProposalGenerator,
    *,
    stage: str,
    mono_start: float,
    started_at: str,
) -> _AttemptResult:
    """One adapter call + one authoritative write transaction (§4.6).

    Semantics mirror relevance (approved Phase 4 pattern): the adapter
    exception or envelope failure persists a FAILED_RETRYABLE attempt;
    item-level failures (DC-3) likewise persist a failed attempt with the
    evidence; success persists attempts + proposals + latency + the 0014
    artifact in a single transaction, guarded by the authoritative budget
    re-check and first-writer-wins. An interrupt leaves nothing written.
    """
    bucket_id = inputs.bucket_id
    response: object | None = None
    failure: BaseException | None = None
    provenance: tuple[str, str, str] = ("", "", "")
    drafts: tuple[ProposalDraft, ...] = ()
    rejections: tuple[dict[str, object], ...] = ()
    try:
        response = generator.generate(inputs.request)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        failure = error
    if failure is None:
        try:
            provenance, drafts, rejections = _validated_response(
                response, inputs.request, inputs.handle_map
            )
        except GeneratorContractError as error:
            failure = error
    if failure is None and not drafts and rejections:
        # DC-3 (owner-vetoable): a non-empty response whose every item
        # failed Tier-2 is an execution failure — never a legitimate
        # zero-proposals success. Evidence still rides the artifact.
        codes = ", ".join(str(entry["reason_code"]) for entry in rejections)
        failure = GeneratorContractError(
            f"every one of {len(rejections)} response items failed"
            f" structural validation: {codes}"
        )
    with transaction(connection):
        # Authoritative budget re-check: a concurrent writer may have
        # consumed attempts after the pre-call count (NFR-REL-02).
        current_count = _attempt_count(connection, bucket_id)
        if current_count >= inputs.budget:
            return _AttemptResult(attempt_id=None)
        if failure is None and _has_success(connection, bucket_id):
            # First-writer-wins (F1): a competing SUCCEEDED attempt means
            # this pass contributes nothing; the pass reports no outcome.
            return _AttemptResult(attempt_id=None, superseded=True)
        finished_at = _utc_now()
        succeeded = failure is None
        outcome = "SUCCEEDED" if succeeded else "FAILED_RETRYABLE"
        error_text: str | None = None
        if not succeeded:
            if isinstance(failure, GeneratorContractError):
                error_text = _redact_error_text(str(failure))
            else:
                error_text = _redact_error_text(str(failure or ""))
        if succeeded:
            decision_count = len(drafts)
            model_id, model_version, prompt_version = provenance
            cursor = connection.execute(
                "INSERT INTO processing_attempt (stage, bucket_id, started_at,"
                " finished_at, outcome, error, model_id, model_version,"
                " config_version) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    stage,
                    bucket_id,
                    started_at,
                    finished_at,
                    outcome,
                    model_id,
                    model_version,
                    prompt_version,
                ),
            )
            attempt_id = int(cursor.lastrowid or 0)
            for draft in drafts:
                _persist_proposal(
                    connection,
                    bucket_id,
                    inputs.source_vertical_id,
                    attempt_id,
                    provenance,
                    draft,
                    inputs.handle_map,
                    finished_at,
                )
            call_seconds = (
                time.monotonic() - mono_start if mono_start >= 0 else 0.0
            )
            _record_latency(
                connection,
                attempt_id,
                call_seconds,
                finished_at,
                _stage_delay_seconds(connection, bucket_id, finished_at),
            )
        else:
            decision_count = 0
            cursor = connection.execute(
                "INSERT INTO processing_attempt (stage, bucket_id, started_at,"
                " finished_at, outcome, error) VALUES (?, ?, ?, ?, ?, ?)",
                (stage, bucket_id, started_at, finished_at, outcome,
                 error_text),
            )
            attempt_id = int(cursor.lastrowid or 0)
        if response is not None:
            raw_text, response_sha, raw_truncated = _process_raw_response(response)
        else:
            raw_text, response_sha, raw_truncated = None, None, 0
        connection.execute(
            "INSERT INTO proposal_attempt_input (attempt_id,"
            " masking_ruleset_version, policy_version, masked_request,"
            " request_sha256, raw_response, response_sha256,"
            " raw_response_truncated, rejected_items, captured_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt_id,
                MASKING_RULESET_VERSION,
                PROPOSAL_PROMPT_VERSION,
                serialize_proposal_request(
                    inputs.context, include_handle_map=True
                ),
                _hash_text(
                    serialize_proposal_request(
                        inputs.request, include_handle_map=False
                    )
                ),
                raw_text,
                response_sha,
                raw_truncated,
                (
                    None
                    if not rejections
                    else json.dumps(
                        list(rejections),
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                ),
                finished_at,
            ),
        )
    return _AttemptResult(
        attempt_id=attempt_id,
        generated=succeeded,
        proposal_count=decision_count,
        rejected_count=len(rejections),
    )


def _process_bucket(
    connection: sqlite3.Connection,
    bucket_id: int,
    primary: ProposalGenerator,
    fallback: ProposalGenerator | None,
    *,
    budget: int,
    policy: ProposalPolicy,
) -> ProposalBucketOutcome | None:
    """All attempts for one bucket in one pass (primary, then fallback).

    Budget refusal (attempts ≥ budget) reports EXHAUSTED with zero
    contribution (NFR-REL-03 honesty); a competing SUCCEEDED attempt makes
    the pass skip the bucket with no outcome row (``None``); a failed
    attempt reports RETRYING or EXHAUSTED from the post-failure count.
    """
    inputs = _attempt_inputs(connection, bucket_id, budget=budget, policy=policy)
    if inputs.attempt_count >= budget:
        return ProposalBucketOutcome(
            bucket_id=bucket_id,
            status=ProposalOutcome.EXHAUSTED,
            attempt_ids=(),
        )
    outcome: ProposalOutcome | None = None
    attempt_ids: list[int] = []
    proposal_count = 0
    rejected_count = 0
    for index, generator in enumerate(
        (primary,) if fallback is None else (primary, fallback)
    ):
        stage = (
            STAGE_PROPOSAL_GENERATION
            if index == 0
            else STAGE_PROPOSAL_FALLBACK_GENERATION
        )
        started_at = _utc_now()
        mono_start = time.monotonic()
        result = _run_attempt(
            connection,
            inputs,
            generator,
            stage=stage,
            mono_start=mono_start,
            started_at=started_at,
        )
        if result.attempt_id is None:
            if result.superseded:
                return None  # first-writer-wins: contribute nothing
            break  # budget exhausted mid-pass; nothing further to do
        attempt_ids.append(result.attempt_id)
        if result.generated:
            proposal_count = result.proposal_count
            rejected_count = result.rejected_count
            outcome = ProposalOutcome.GENERATED
            break
    if outcome is None:
        after = _attempt_count(connection, bucket_id)
        outcome = (
            ProposalOutcome.EXHAUSTED
            if after >= budget
            else ProposalOutcome.RETRYING
        )
    return ProposalBucketOutcome(
        bucket_id=bucket_id,
        status=outcome,
        attempt_ids=tuple(attempt_ids),
        proposal_count=proposal_count,
        rejected_count=rejected_count,
    )


def _validate_pass_arguments(
    max_attempts_per_bucket: int,
    budget_overrides: Mapping[int, int] | None,
) -> dict[int, int]:
    """Loud, pre-state validation of the pass budget arguments (Phase 4
    approved pattern: a caller typo must never mass-fail the backlog)."""
    if not _is_plain_int(max_attempts_per_bucket) or max_attempts_per_bucket <= 0:
        raise ValueError(
            "max_attempts_per_bucket must be a positive integer,"
            f" got {max_attempts_per_bucket!r}"
        )
    overrides: dict[int, int] = {}
    if budget_overrides is not None:
        for key, value in budget_overrides.items():
            if not isinstance(key, int) or isinstance(key, bool):
                raise ValueError(
                    f"budget override keys must be bucket ids, got {key!r}"
                )
            if not _is_plain_int(value) or value <= 0:
                raise ValueError(
                    "budget override values must be a positive integer,"
                    f" got {value!r} for bucket {key}"
                )
            overrides[int(key)] = int(value)
    return overrides


def run_proposal_pass(
    connection: sqlite3.Connection,
    primary: ProposalGenerator,
    fallback: ProposalGenerator | None = None,
    *,
    max_attempts_per_bucket: int = DEFAULT_PROPOSAL_ATTEMPTS_PER_BUCKET,
    budget_overrides: Mapping[int, int] | None = None,
    policy: ProposalPolicy | None = None,
) -> ProposalPassResult:
    """Run one deterministic proposal pass over pending buckets (FR-PRP-01).

    Buckets are discovered state-derived (§4.1), ordered ascending by id,
    and processed with the approved budget/override pattern: the override
    for a bucket is its raised *total* attempt cap for this pass.
    Success means the validated proposals, latency rows and the 0014
    artifact are durably recorded with the attempt in one transaction; no
    bucket-state change, no task/announcement/notification writes.

    A non-bucket-specific failure (e.g. another writer holding the write
    lock) aborts the pass and raises :class:`ProposalPassError` carrying
    the committed per-bucket prefix — an incomplete pass is never
    reported as successful. Interrupts (``KeyboardInterrupt``/
    ``SystemExit``) propagate unchanged with nothing written for the
    in-flight attempt.
    """
    overrides = _validate_pass_arguments(max_attempts_per_bucket, budget_overrides)
    active_policy = policy if policy is not None else ProposalPolicy()
    outcomes: list[ProposalBucketOutcome] = []
    generated: list[int] = []
    exhausted: list[int] = []
    try:
        for bucket_id, _attempts in _pending_rows(connection, active_policy):
            budget = overrides.get(bucket_id, max_attempts_per_bucket)
            outcome = _process_bucket(
                connection,
                bucket_id,
                primary,
                fallback,
                budget=budget,
                policy=active_policy,
            )
            if outcome is None:
                continue  # first-writer-wins: another writer already succeeded
            outcomes.append(outcome)
            if outcome.status is ProposalOutcome.GENERATED:
                generated.append(bucket_id)
            elif outcome.status is ProposalOutcome.EXHAUSTED:
                exhausted.append(bucket_id)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        # A failure that is not bucket-specific aborts the pass loudly:
        # an incomplete pass is never reported as successful, but every
        # bucket outcome committed before the abort stays durable.
        raise ProposalPassError(
            tuple(outcomes),
            tuple(generated),
            f"proposal pass aborted: {_redact_error_text(str(error))}",
        ) from error
    return ProposalPassResult(
        outcomes=tuple(outcomes),
        generated=tuple(generated),
        exhausted=tuple(exhausted),
    )


def proposal_pending_buckets(
    connection: sqlite3.Connection,
    *,
    max_attempts_per_bucket: int = DEFAULT_PROPOSAL_ATTEMPTS_PER_BUCKET,
    policy: ProposalPolicy | None = None,
) -> tuple[int, ...]:
    """Read-only inventory: bucket ids still awaiting proposals (C4).

    A bucket whose proposal attempts already reach ``budget`` is not
    listed (it is exhausted; see :func:`proposal_exhausted_buckets`).
    Ordered ascending by id; zero writes, zero state change.
    """
    _validate_pass_arguments(max_attempts_per_bucket, None)
    active_policy = policy if policy is not None else ProposalPolicy()
    return tuple(
        bucket_id
        for bucket_id, attempts in _pending_rows(connection, active_policy)
        if attempts < max_attempts_per_bucket
    )


def proposal_exhausted_buckets(
    connection: sqlite3.Connection,
    *,
    max_attempts_per_bucket: int = DEFAULT_PROPOSAL_ATTEMPTS_PER_BUCKET,
    policy: ProposalPolicy | None = None,
) -> tuple[int, ...]:
    """Read-only inventory: pending buckets with the budget consumed (C4).

    These buckets' attempt history reached ``max_attempts_per_bucket``
    without a success; recovery is an authorized override (§4.7). Ordered
    ascending by id; zero writes, zero state change.
    """
    _validate_pass_arguments(max_attempts_per_bucket, None)
    active_policy = policy if policy is not None else ProposalPolicy()
    return tuple(
        bucket_id
        for bucket_id, attempts in _pending_rows(connection, active_policy)
        if attempts >= max_attempts_per_bucket
    )


# =========================================================================
# C4 — backlog visibility (NFR-REL-06, FR-UI-05)
# =========================================================================


@dataclass(frozen=True)
class ProposalBacklog:
    """Read-only proposal backlog snapshot (NFR-REL-06, FR-UI-05).

    ``awaiting_bucket_ids``: pending buckets whose proposal budget is not
    yet consumed. ``exhausted_bucket_ids``: pending buckets at/over the
    budget. The oldest awaiting bucket is ranked on the event-time
    convention (``orchestration.pipeline_backlog``): its end-event anchor
    when present, else its start-event anchor; a bucket whose anchor
    cannot be parsed at all ranks as the oldest unresolvable work and
    carries no derived age. ``oldest_awaiting_age_event_seconds`` is the
    group's event progress minus that anchor — ``None`` when either side
    is missing/unparsable or the difference would be negative.
    """

    awaiting_bucket_ids: tuple[int, ...]
    exhausted_bucket_ids: tuple[int, ...]
    oldest_awaiting_bucket_id: int | None
    oldest_awaiting_anchor: str | None
    oldest_awaiting_age_event_seconds: float | None


def _backlog_anchor_row(
    connection: sqlite3.Connection, bucket_ids: Sequence[int]
) -> tuple[int, str | None, float | None]:
    """The oldest awaiting bucket with its anchor and event age (C4)."""
    if not bucket_ids:
        return 0, None, None
    placeholders = ", ".join(["?"] * len(bucket_ids))
    rows = connection.execute(
        f"""
        SELECT b.id AS bucket_id,
               b.start_event_timestamp AS start_event_timestamp,
               b.end_event_timestamp AS end_event_timestamp,
               p.progress_to AS progress_to
          FROM bucket AS b
          JOIN source_group AS g ON g.id = b.group_id
          LEFT JOIN source_group_progress AS p ON p.group_id = g.id
         WHERE b.id IN ({placeholders})
        """,
        tuple(bucket_ids),
    ).fetchall()

    def rank(row: sqlite3.Row) -> tuple[float, float, int]:
        anchor = (
            row["end_event_timestamp"]
            if row["end_event_timestamp"] is not None
            else row["start_event_timestamp"]
        )
        try:
            return (0.0, parse_event_time(anchor).timestamp(), int(row["bucket_id"]))
        except Exception:  # noqa: BLE001 - undatable anchors rank oldest
            return (-1.0, 0.0, int(row["bucket_id"]))

    oldest = min(rows, key=rank)
    anchor = (
        oldest["end_event_timestamp"]
        if oldest["end_event_timestamp"] is not None
        else oldest["start_event_timestamp"]
    )
    progress = oldest["progress_to"]
    age: float | None = None
    if anchor is not None and progress is not None:
        try:
            delay = (
                parse_event_time(str(progress)).timestamp()
                - parse_event_time(str(anchor)).timestamp()
            )
        except Exception:  # noqa: BLE001 - unparsable side: no derived age
            delay = None
        if delay is not None and delay >= 0.0:
            age = delay
    return int(oldest["bucket_id"]), anchor, age


def proposal_backlog(
    connection: sqlite3.Connection,
    *,
    max_attempts_per_bucket: int = DEFAULT_PROPOSAL_ATTEMPTS_PER_BUCKET,
    policy: ProposalPolicy | None = None,
) -> ProposalBacklog:
    """Read-only proposal backlog snapshot (C4, NFR-REL-06, FR-UI-05).

    Mirrors the pipeline-backlog convention of
    :mod:`backend.orchestration` for the proposal stage; zero writes and
    zero state changes — the sibling keeps ``pipeline_backlog`` untouched.
    """
    _validate_pass_arguments(max_attempts_per_bucket, None)
    active_policy = policy if policy is not None else ProposalPolicy()
    awaiting: list[int] = []
    exhausted_ids: list[int] = []
    for bucket_id, attempts in _pending_rows(connection, active_policy):
        if attempts >= max_attempts_per_bucket:
            exhausted_ids.append(bucket_id)
        else:
            awaiting.append(bucket_id)
    oldest_id, anchor, age = _backlog_anchor_row(connection, awaiting)
    return ProposalBacklog(
        awaiting_bucket_ids=tuple(awaiting),
        exhausted_bucket_ids=tuple(exhausted_ids),
        oldest_awaiting_bucket_id=(oldest_id if awaiting else None),
        oldest_awaiting_anchor=(anchor if awaiting else None),
        oldest_awaiting_age_event_seconds=(age if awaiting else None),
    )








