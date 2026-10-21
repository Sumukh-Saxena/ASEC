"""Relevance classification for ASEC (Phase 4).

FR-REL-01..07 (docs/SRS.md §3.3), EIR-03 (§5, lines 419–426), NFR-REL-01..05
and NFR-REL-07 (§6.1), NFR-AUD-02 (§6.2), NFR-PERF-02 (§6.4),
NFR-SEC-04/EIR-03 masking and AI-01..04 (§7). Storage contracts are the
committed migrations 0002 (message flags incl. ``flag_unclassified``),
0004 (``processing_attempt``, ``relevance_decision``,
``relevance_correction``) and 0011 (``latency_measurement``); lifecycle
edges come from :mod:`backend.bucket_lifecycle` (Phase 3) and discovery
consumes :func:`backend.orchestration.processing_queue` read-only.

The classifier itself is a stateless, injected adapter
(:class:`RelevanceClassifier`): one bucket in, a list of
``{message_id, decision}`` plus model/prompt provenance out (EIR-03). The
adapter never touches the database; the backend composes the request,
masks personal data immediately before every call (NFR-SEC-04, including
fallback calls), validates the response as untrusted data, and only
(approved) persisted decisions become authoritative state (AI-02).

Design decisions recorded with this batch (owner-vetoable where marked;
docs/PHASE2_PLAN.md §13):

- Stage tokens (free text; the SRS defines no vocabulary, 0004): primary
  ``RELEVANCE_CLASSIFICATION``, fallback ``RELEVANCE_FALLBACK_CLASSIFICATION``.
- Every execution failure — adapter exception, malformed/incomplete/
  duplicated/foreign/vocabulary-violating output, missing provenance — is
  recorded as ``FAILED_RETRYABLE`` (FR-REL-07, AC-12); ``FAILED_PERMANENT``
  stays reserved (owner-vetoable) and is never written automatically.
- Attempt rows are written only after the adapter responded and the
  backend validated, inside one ``transaction()`` that also holds the
  decisions, the latency record and the UNCLASSIFIED flag marking: a
  partially recorded attempt is structurally impossible, and a crash
  mid-flight leaves nothing to duplicate (NFR-REL-01/04).
- Attempt provenance (model id/version, prompt/config version) is stored
  on the attempt only when the whole response validated; failed attempts
  keep provenance missing (DR-04 — a failed response's provenance must not
  be presented as verified). ``config_version`` carries the validated
  prompt/config version, the only configuration identity this contract
  has (EIR-03).
- A failed attempt marks the bucket's messages ``flag_unclassified``
  (FR-REL-07; the column exists since 0002). Post-recovery clearing of
  that flag is **deferred** by standing owner decision, so a message that
  is classified successfully later keeps ``flag_unclassified = 1`` until
  the deferred clearing policy is decided — the exact AC-13 acceptance
  gap recorded in docs/PHASE2_PLAN.md §13; it is never silently
  implemented here.
- Retry budget: ``DEFAULT_MAX_ATTEMPTS_PER_BUCKET`` attempt rows per
  bucket across passes (owner-adjustable argument). Each would-be attempt
  re-checks the budget inside its transaction; a bucket that reaches the
  budget while still outstanding is left FAILED for manual re-queue (the
  attempts keep their own ``FAILED_RETRYABLE`` outcomes — the budget is
  accumulation policy, not a per-attempt property). The count is
  **stage-scoped**: only the two relevance stage tokens consume it, so a
  later phase writing the same attempts table cannot silently eat
  relevance retries. The argument must be a positive integer; anything
  else fails loud before any state change.
- Race guard (first writer wins, NFR-REL-01/04): an attempt transaction
  refuses to record a second SUCCEEDED result for a bucket whose
  committed attempts already contain one — the competing pass contributes
  no attempt, no decisions and no outcome, and the finalize convergence
  advances the bucket. Failure convergence likewise never marks FAILED
  over a competing committed success.
- Adapter error strings persisted on the attempt are masked
  (``mask_pii``) and length-capped (``MAX_ATTEMPT_ERROR_LENGTH``): the
  error channel is a review surface, and an adapter's own message must
  not carry unmasked personal data or be unbounded.
- ``CLASSIFIER_LATENCY`` measures the adapter call alone (a monotonic
  window around the ``classify`` invocation), excluding backend-side
  validation time.
- Fallback (NFR-REL-07): the fallback classifier runs only after the
  primary failed; both attempts are recorded; a successful fallback
  supplies the effective result without erasing the primary's failure
  record.
- Discovery (deterministic, ascending bucket id):
  :func:`backend.orchestration.processing_queue` (``never_started``,
  ``awaiting_recovery``) plus the §12 coverage gap — CLOSED buckets whose
  attempts are all ``FAILED_RETRYABLE`` and none ``SUCCEEDED`` (the queue
  is not a complete inventory). FAILED buckets are never auto-attempted
  (NFR-REL-03 manual re-queue only, :func:`requeue_failed_bucket`);
  OPEN buckets are never touched (FR-REL-01).
- Crash convergence (NFR-REL-04): any CLOSED/AWAITING_RECOVERY bucket
  holding a SUCCEEDED attempt but not yet PROCESSED (a crash between the
  attempt commit and the lifecycle transition) is finalized to PROCESSED
  by the next pass — no new attempts, no loss, no duplicates.
- Terminal-failure convergence (NFR-REL-02/04, approved F4): a **CLOSED**
  bucket whose committed attempts include a ``FAILED_PERMANENT`` and
  neither a success nor a retryable failure sits in no queue and no
  discovery rule — the pass converges it to FAILED through the existing
  legal edge, the exact state NFR-REL-03's manual re-queue acts on. The
  writer itself stays two-valued; convergence reads whatever committed
  outcomes exist. AWAITING_RECOVERY is deliberately excluded from the
  sweep: an AWAITING bucket with only-permanent attempts exists only via
  an authorized manual re-queue, so it is owner-authorized recovery work —
  converging it back would make manual recovery structurally impossible
  (refinement-letter deviation, recorded and vetoable in §13.11). A bucket
  with at least one retryable failure and no success remains automatically
  retriable (mixed-outcome policy: retryable dominates).
- Authorized recovery budget (approved F4/R3): ``budget_overrides`` gives
  individual buckets a *raised total attempt cap* per pass, so a manual
  re-queue of budget-exhausted work can actually execute new attempts.
  Invalid values raise loud before any state change; buckets without an
  override keep the ordinary budget; without an override a requeued
  exhausted bucket is refused and its exhaustion reported honestly
  (EXHAUSTED → FAILED), never a fake success. History is append-only:
  prior attempts and decisions are never erased or overwritten.
- Masked-input audit snapshot (NFR-AUD-02, approved F3): every committed
  adapter-executing attempt (primary or fallback, success or execution
  failure) also persists a canonical serialization of the exact
  ``RelevanceRequest`` it received, its SHA-256 and the
  ``MASKING_RULESET_VERSION``/``CLASSIFICATION_POLICY_VERSION`` identities
  (migration 0013, ``relevance_attempt_input``). The artifact is captured
  inside the attempt's transaction, so a later masking-ruleset change can
  alter what a *new* attempt sends — never what an old attempt stored.
  A version string alone would not be a retrievable artifact; the stored
  request is. Refused and superseded paths create no attempt and no
  snapshot, by construction.
- Classifier latency (NFR-PERF-02): one ``CLASSIFIER_LATENCY``
  ``latency_measurement`` row, subject = the successful
  ``processing_attempt``, recorded inside the same transaction. Targets
  (≤ 1.5 s P95) are evaluation-only inputs (NFR-PERF-01) and are never
  asserted by running code.
"""

from __future__ import annotations

import enum
import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from backend.bucket_lifecycle import (
    BucketState,
    InvalidBucketTransitionError,
    transition_bucket_processing_state,
)
from backend.db.connection import transaction
from backend.masking import mask_pii
from backend.orchestration import processing_queue

#: FR-REL-02 verbatim: the only decisions; there is no UNCERTAIN state.
DECISION_VOCABULARY: tuple[str, str] = ("RELEVANT", "IRRELEVANT")

#: Stage tokens for attempt rows (free text per 0004; autonomous choice).
STAGE_RELEVANCE_CLASSIFICATION = "RELEVANCE_CLASSIFICATION"
STAGE_RELEVANCE_FALLBACK_CLASSIFICATION = "RELEVANCE_FALLBACK_CLASSIFICATION"

#: Latency metric label for 0011 rows (free text per 0011; autonomous).
LATENCY_METRIC_LABEL = "CLASSIFIER_LATENCY"

#: Deterministic per-bucket attempt-row cap across passes (autonomous,
#: owner-adjustable argument of :func:`run_relevance_pass`).
DEFAULT_MAX_ATTEMPTS_PER_BUCKET = 3

#: Upper bound for the stored ``processing_attempt.error`` text (autonomous;
#: the error channel must stay bounded even for pathological adapters).
MAX_ATTEMPT_ERROR_LENGTH = 500

#: Durable lineage identity of the masking ruleset at request-build time
#: (approved F3, NFR-AUD-02). Bumping this string is an owner-approved
#: change: older artifacts stay self-contained in
#: ``relevance_attempt_input``, these markers only identify which ruleset
#: produced each snapshot's masking.
MASKING_RULESET_VERSION = "masking-rules-v1"

#: Durable lineage identity of :data:`CLASSIFICATION_POLICY` text (approved
#: F3, NFR-AUD-02); same discipline as above.
CLASSIFICATION_POLICY_VERSION = "classification-policy-v1"

#: FR-REL-02/FR-REL-03/FR-REL-04 policy carried through the request
#: contract. The backend can only enforce the vocabulary and coverage
#: deterministically; whether a real model *complies* with the policy is
#: review/evaluation material (AI-05/AI-06), never something schema
#: validation can prove. The boundary is documented, not claimed closed.
CLASSIFICATION_POLICY = (
    "Classify each message of the bucket for its relevance to the group's"
    " event-coordination work. The only permitted decisions are RELEVANT"
    " and IRRELEVANT; there is no UNCERTAIN state. When in doubt, decide"
    " RELEVANT: missing real work is worse than reviewing extra proposals"
    " (FR-REL-03). Judge each message independently: one message's"
    " relevance never implies anything about its bucket-mates (FR-REL-04)."
    " Messages from volunteers and unknown senders are classified like"
    " everyone else (FR-REL-01) and may become evaluation data."
)


class RelevanceServiceError(Exception):
    """Base class for failures reported by this module."""


class UnknownBucketError(RelevanceServiceError):
    """No bucket row exists for the given id; nothing was processed."""


class ClassifierContractError(RelevanceServiceError):
    """The classifier response violates the EIR-03 output contract.

    Raised only after the adapter returned: bad shape, missing coverage,
    foreign or duplicated message ids, vocabulary violations, or missing
    provenance. It is an execution failure (never a valid empty result,
    never IRRELEVANT) and is recorded as ``FAILED_RETRYABLE``.
    """


class RelevancePassError(RelevanceServiceError):
    """A relevance pass was aborted by a failure that is not bucket-specific.

    Raised instead of returning any result object so an incomplete pass
    can never be read as successful (same posture as
    ``backend.ingest.IngestionBatchError``). ``outcomes`` carries every
    per-bucket outcome committed before the abort; ``finalized`` the
    bucket ids convergently moved to PROCESSED before the abort; the
    original error is chained.
    """

    def __init__(
        self,
        outcomes: tuple[BucketOutcome, ...],
        finalized: tuple[int, ...],
        detail: str,
        terminal_failed: tuple[int, ...] = (),
    ) -> None:
        super().__init__(detail)
        self.outcomes = outcomes
        self.finalized = finalized
        self.terminal_failed = terminal_failed


@dataclass(frozen=True)
class ContextMessage:
    """One bucket message as the classifier sees it (EIR-03 request).

    ``sender_*`` fields are ``None`` for unknown senders (missing stored
    as missing, DR-04). ``masked_content`` is the NFR-SEC-04-masked copy;
    originals never leave the backend (DR-03). Mention handles are opaque
    Discord IDs, never resolved or remapped here (EIR-03).
    """

    message_id: int
    sender_person_id: int | None
    sender_discord_user_id: str | None
    sender_name: str | None
    event_timestamp: str | None
    reply_reference: str | None
    masked_content: str | None
    mention_handles: tuple[str, ...]


@dataclass(frozen=True)
class RelevanceRequest:
    """The whole bucket as one stateless request (EIR-03, FR-REL-01).

    ``instructions`` carries :data:`CLASSIFICATION_POLICY` — the doubt policy
    and vocabulary the backend requires; conveying it is contract, enforcing
    it semantically is evaluation, not validation.
    """

    bucket_id: int
    group_discord_channel_id: str
    vertical_name: str | None
    instructions: str
    messages: tuple[ContextMessage, ...]


@dataclass(frozen=True)
class MessageDecision:
    """One decision returned by the adapter (untrusted until validated)."""

    message_id: int
    decision: str


@dataclass(frozen=True)
class RelevanceResponse:
    """The adapter's response (EIR-03): decisions plus provenance.

    The backend validates provenance and every decision before anything is
    written; field values here are never trusted blindly.
    """

    model_id: str
    model_version: str
    prompt_version: str
    decisions: tuple[MessageDecision, ...]


class RelevanceClassifier(Protocol):
    """Stateless classifier boundary (EIR-03, AI-01).

    One call, one bucket, no database access, no conversations: the whole
    context is in the request and the whole result in the response.
    """

    def classify(self, request: RelevanceRequest) -> RelevanceResponse:
        """Return the decisions and provenance for exactly ``request``."""


class RelevanceOutcome(enum.Enum):
    """Per-bucket outcome of one pass.

    ``CLASSIFIED``: a validated decision set for every message is durably
    recorded and the bucket advanced to PROCESSED. ``RETRYING``: the
    attempts failed but the retry budget is not yet exhausted — the bucket
    paused in AWAITING_RECOVERY (legal edge) and the next pass retries.
    ``EXHAUSTED``: the attempt budget is consumed; the bucket is FAILED and
    returns only via manual re-queue (:func:`requeue_failed_bucket`).
    """

    CLASSIFIED = "CLASSIFIED"
    RETRYING = "RETRYING"
    EXHAUSTED = "EXHAUSTED"


@dataclass(frozen=True)
class BucketOutcome:
    """What happened to one bucket in one pass.

    ``attempt_ids`` are the attempt rows *created this pass* (primary
    first, then fallback), in insertion order — earlier attempts stay
    untouched (records are never erased or overwritten). ``decision_count``
    is the number of decisions persisted this pass; failures persist none.
    """

    bucket_id: int
    status: RelevanceOutcome
    attempt_ids: tuple[int, ...]
    decision_count: int


@dataclass(frozen=True)
class RelevancePassResult:
    """Everything one pass durably changed.

    ``finalized`` lists bucket ids that already held a SUCCEEDED attempt
    but were not yet PROCESSED (crash between commit and transition) and
    are converged by this pass without new attempts (NFR-REL-04).
    ``terminal_failed`` lists bucket ids holding only terminal failures
    (approved F4) converged to FAILED through existing legal edges so the
    NFR-REL-03 manual re-queue can act on them — never auto-retried, no
    new attempt rows.
    """

    finalized: tuple[int, ...]
    outcomes: tuple[BucketOutcome, ...]
    terminal_failed: tuple[int, ...] = ()


@dataclass(frozen=True)
class _AttemptResult:
    """Internal result of one attempt execution.

    ``attempt_id`` is ``None`` exactly when nothing was written: either the
    budget guard refused (``superseded`` False) or a competing writer's
    committed SUCCEEDED attempt already exists for this bucket
    (``superseded`` True — first writer wins; this pass never adds to it).
    """

    attempt_id: int | None
    classified: bool
    decision_count: int
    superseded: bool = False


def _utc_now() -> str:
    """Backend-stamped UTC timestamp (the A2 ingestion pattern)."""
    return datetime.now(UTC).isoformat()


def _redact_error_text(text: str) -> str:
    """Persist adapter error strings masked and bounded (NFR-SEC-04 reach).

    The error channel is evidence too: a failed response's message must
    neither carry unmasked personal data into review surfaces nor be
    unbounded. Truncation is marked with an ellipsis suffix.
    """
    redacted = mask_pii(text)
    if len(redacted) > MAX_ATTEMPT_ERROR_LENGTH:
        return redacted[: MAX_ATTEMPT_ERROR_LENGTH - 3] + "..."
    return redacted


def _serialize_masked_request(request: RelevanceRequest) -> str:
    """Canonical JSON of the exact masked request the adapter received.

    The duplication of every field is deliberate: the artifact is a
    self-contained durable record (NFR-AUD-02), immune to later edits of
    the masking ruleset or policy text — a ruleset change can alter what a
    NEW attempt sends, never what an OLD attempt stored. Content here is
    already masked (:meth:`_bucket_request`); the original messages stay
    verbatim in the source store (DR-03).
    """
    payload = {
        "bucket_id": request.bucket_id,
        "group_discord_channel_id": request.group_discord_channel_id,
        "vertical_name": request.vertical_name,
        "instructions": request.instructions,
        "messages": [
            {
                "message_id": message.message_id,
                "sender_person_id": message.sender_person_id,
                "sender_discord_user_id": message.sender_discord_user_id,
                "sender_name": message.sender_name,
                "event_timestamp": message.event_timestamp,
                "reply_reference": message.reply_reference,
                "masked_content": message.masked_content,
                "mention_handles": list(message.mention_handles),
            }
            for message in request.messages
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _request_sha256(serialized: str) -> str:
    """Integrity hash of the stored masked-request artifact (approved F3)."""
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _bucket_request(
    connection: sqlite3.Connection, bucket_id: int
) -> RelevanceRequest:
    """Compose the whole-bucket request for ``bucket_id`` (FR-REL-01, EIR-03).

    Messages ordered by ascending id; content is masked only in the
    request copy through :func:`backend.masking.mask_pii` — the stored
    content is never touched (DR-03, AC-21). An absent bucket fails loud;
    an unmapped group's vertical name stays ``None`` (DR-04), never a
    guessed name. ``raw_metadata`` is deliberately not part of the request:
    it is adapter-supplied envelope data, not the message content the
    classifier judges (EIR-03 fixes the request contract).
    """
    context = connection.execute(
        "SELECT g.discord_channel_id AS channel, v.name AS vertical_name"
        "  FROM bucket AS b"
        "  JOIN source_group AS g ON g.id = b.group_id"
        "  LEFT JOIN vertical AS v ON v.id = g.vertical_id"
        " WHERE b.id = ?",
        (bucket_id,),
    ).fetchone()
    if context is None:
        raise UnknownBucketError(f"no bucket with id {bucket_id}")
    handles: dict[int, list[str]] = {}
    for row in connection.execute(
        "SELECT bm2.message_id AS message_id, mm.mentioned_discord_user_id AS handle"
        "  FROM message_mention AS mm"
        "  JOIN bucket_message AS bm2 ON bm2.message_id = mm.message_id"
        " WHERE bm2.bucket_id = ?"
        " ORDER BY mm.message_id, mm.mentioned_discord_user_id",
        (bucket_id,),
    ).fetchall():
        handles.setdefault(int(row["message_id"]), []).append(
            str(row["handle"])
        )
    entries = tuple(
        ContextMessage(
            message_id=int(row["id"]),
            sender_person_id=(
                None
                if row["sender_person_id"] is None
                else int(row["sender_person_id"])
            ),
            sender_discord_user_id=(
                None
                if row["sender_discord_user_id"] is None
                else str(row["sender_discord_user_id"])
            ),
            sender_name=(
                None if row["sender_name"] is None else str(row["sender_name"])
            ),
            event_timestamp=(
                None
                if row["event_timestamp"] is None
                else str(row["event_timestamp"])
            ),
            reply_reference=(
                None
                if row["reply_reference"] is None
                else str(row["reply_reference"])
            ),
            masked_content=(
                None if row["content"] is None else mask_pii(str(row["content"]))
            ),
            mention_handles=tuple(handles.get(int(row["id"]), ())),
        )
        for row in connection.execute(
            "SELECT m.id, m.sender_person_id, m.sender_discord_user_id,"
            "       p.name AS sender_name, m.event_timestamp,"
            "       m.reply_reference, m.content"
            "  FROM bucket_message AS bm"
            "  JOIN source_message AS m ON m.id = bm.message_id"
            "  LEFT JOIN person AS p ON p.id = m.sender_person_id"
            " WHERE bm.bucket_id = ?"
            " ORDER BY m.id",
            (bucket_id,),
        ).fetchall()
    )
    return RelevanceRequest(
        bucket_id=bucket_id,
        group_discord_channel_id=str(context["channel"]),
        vertical_name=(
            None if context["vertical_name"] is None else str(context["vertical_name"])
        ),
        instructions=CLASSIFICATION_POLICY,
        messages=entries,
    )


def _validated_provenance(value: object, label: str) -> str:
    """EIR-03 provenance checks: non-blank, trimmed, verbatim otherwise."""
    if not isinstance(value, str) or value.strip() == "" or value != value.strip():
        raise ClassifierContractError(
            f"response {label} must be a non-blank trimmed string, got {value!r}"
        )
    return value


def _validated_response(
    response: RelevanceResponse, request: RelevanceRequest
) -> tuple[tuple[str, str, str], tuple[tuple[int, str], ...]]:
    """Validate the whole response against the bucket (EIR-03, FR-REL-02/05).

    Required provenance (model id and version, prompt/config version) and
    exactly one decision per bucket message — no foreign ids, no
    duplicates, FR-REL-02 vocabulary only. Any violation is an execution
    failure; nothing is returned for persistence until everything holds
    (a malformed or incomplete output is never a valid empty result).

    Deliberately *not* validated here: whether a RELEVANT/IRRELEVANT
    assignment is semantically correct or honestly doubt-biased — that is
    the documented validation boundary (see :data:`CLASSIFICATION_POLICY`).
    """
    model_id = _validated_provenance(response.model_id, "model_id")
    model_version = _validated_provenance(response.model_version, "model_version")
    prompt_version = _validated_provenance(response.prompt_version, "prompt_version")
    bucket_ids = {entry.message_id for entry in request.messages}
    try:
        raw_decisions = list(response.decisions)
    except TypeError as error:
        raise ClassifierContractError(
            "response decisions must be an iterable of per-message decisions"
        ) from error
    covered: dict[int, str] = {}
    for item in raw_decisions:
        try:
            message_id = item.message_id
            decision = item.decision
        except AttributeError as error:
            raise ClassifierContractError(
                "every decision must expose message_id and decision"
            ) from error
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            raise ClassifierContractError(
                f"decision message_id must be an integer, got {message_id!r}"
            )
        if message_id not in bucket_ids:
            raise ClassifierContractError(
                f"decision references message {message_id},"
                f" which is not in bucket {request.bucket_id}"
            )
        if message_id in covered:
            raise ClassifierContractError(
                f"decision covers message {message_id} more than once"
            )
        if decision not in DECISION_VOCABULARY:
            raise ClassifierContractError(
                f"decision {decision!r} is outside the FR-REL-02 vocabulary"
                f" {DECISION_VOCABULARY}"
            )
        covered[message_id] = str(decision)
    missing = bucket_ids.difference(covered)
    if missing:
        raise ClassifierContractError(
            f"incomplete coverage: {len(missing)} of {len(bucket_ids)} messages"
            " lack a decision; an incomplete output is an execution failure,"
            " never a valid empty result (EIR-03)"
        )
    return (model_id, model_version, prompt_version), tuple(covered.items())


def _attempt_count(connection: sqlite3.Connection, bucket_id: int) -> int:
    """Committed relevance-stage attempts for ``bucket_id`` (all outcomes).

    Only the two relevance stage tokens count toward the budget: a later
    phase writing the same attempts table must not consume relevance
    retries (per-stage budgets, audit F2).
    """
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM processing_attempt"
        " WHERE bucket_id = ? AND stage IN (?, ?)",
        (
            bucket_id,
            STAGE_RELEVANCE_CLASSIFICATION,
            STAGE_RELEVANCE_FALLBACK_CLASSIFICATION,
        ),
    ).fetchone()
    return int(row["n"])


def _has_success(connection: sqlite3.Connection, bucket_id: int) -> bool:
    """True when a committed SUCCEEDED attempt exists for ``bucket_id``."""
    row = connection.execute(
        "SELECT 1 FROM processing_attempt"
        " WHERE bucket_id = ? AND outcome = 'SUCCEEDED' LIMIT 1",
        (bucket_id,),
    ).fetchone()
    return row is not None


def _finalize_candidates(connection: sqlite3.Connection) -> tuple[int, ...]:
    """Buckets holding a SUCCEEDED attempt but not yet PROCESSED.

    Reached only when a run committed the classification and died before
    (or while) advancing the lifecycle state: the next pass converges them
    deterministically, creating no new attempts (NFR-REL-04).
    """
    rows = connection.execute(
        "SELECT b.id"
        "  FROM bucket AS b"
        " WHERE b.processing_state IN ('CLOSED', 'AWAITING_RECOVERY')"
        "   AND EXISTS (SELECT 1 FROM processing_attempt AS pa"
        "                WHERE pa.bucket_id = b.id AND pa.outcome = 'SUCCEEDED')"
        " ORDER BY b.id"
    ).fetchall()
    return tuple(int(row["id"]) for row in rows)


def stalled_terminal_buckets(connection: sqlite3.Connection) -> tuple[int, ...]:
    """Terminal-failed CLOSED buckets invisible to every operational inventory.

    A CLOSED bucket whose committed attempts hold at least one
    ``FAILED_PERMANENT`` but neither a success nor a retryable failure
    matches no queue entry (orchestration) and no discovery rule — without
    convergence it would be unrecoverable work (NFR-REL-04). Read-only;
    the pass converges such buckets to FAILED (existing legal edge), the
    one state NFR-REL-03's manual re-queue acts on.

    AWAITING_RECOVERY buckets are deliberately *not* converged: in this
    system an AWAITING bucket with only-permanent attempts has exactly one
    producer — an authorized manual re-queue (:func:`requeue_failed_bucket`)
    — so that state is owner-authorized recovery work, reachable through the
    recovery queue; converging it back to FAILED would make manual recovery
    structurally impossible (verified against the recovery path before the
    batch landed). A bucket with at least one ``FAILED_RETRYABLE`` and no
    success is *not* terminal: it stays automatically retriable (approved
    mixed-outcome policy — retryable dominates; §13.11).
    """
    rows = connection.execute(
        "SELECT b.id"
        "  FROM bucket AS b"
        " WHERE b.processing_state = 'CLOSED'"
        "   AND EXISTS (SELECT 1 FROM processing_attempt AS pa"
        "                WHERE pa.bucket_id = b.id"
        "                  AND pa.outcome = 'FAILED_PERMANENT')"
        "   AND NOT EXISTS (SELECT 1 FROM processing_attempt AS pa"
        "                WHERE pa.bucket_id = b.id"
        "                  AND pa.outcome IN ('SUCCEEDED', 'FAILED_RETRYABLE'))"
        " ORDER BY b.id"
    ).fetchall()
    return tuple(int(row["id"]) for row in rows)


def _discovery_targets(connection: sqlite3.Connection) -> tuple[int, ...]:
    """Eligible buckets for new classification attempts, ascending id.

    Union of the Phase 3 hand-off queue's ``never_started`` and
    ``awaiting_recovery`` entries and the §12 coverage gap: CLOSED buckets
    whose committed attempts are all ``FAILED_RETRYABLE`` and of which
    none succeeded — real outstanding work the queue cannot show. FAILED
    buckets are deliberately absent (manual re-queue owner operation,
    NFR-REL-03); OPEN buckets are never eligible (FR-REL-01).
    """
    queue = processing_queue(connection)
    targets: set[int] = {*queue.never_started, *queue.awaiting_recovery}
    gap = connection.execute(
        "SELECT b.id"
        "  FROM bucket AS b"
        " WHERE b.processing_state = 'CLOSED'"
        "   AND EXISTS (SELECT 1 FROM processing_attempt AS pa"
        "                WHERE pa.bucket_id = b.id"
        "                  AND pa.outcome = 'FAILED_RETRYABLE')"
        "   AND NOT EXISTS (SELECT 1 FROM processing_attempt AS pa"
        "                WHERE pa.bucket_id = b.id AND pa.outcome = 'SUCCEEDED')"
        " ORDER BY b.id"
    ).fetchall()
    targets.update(int(row["id"]) for row in gap)
    return tuple(sorted(targets))


def _run_attempt(
    connection: sqlite3.Connection,
    bucket_id: int,
    classifier: RelevanceClassifier,
    stage: str,
    budget: int,
) -> _AttemptResult:
    """Run one attempt against ``classifier`` and persist its full record.

    Order of operations: budget pre-check (spares an adapter call), the
    stateless adapter call over the masked whole-bucket request, backend
    validation, then one ``transaction()`` that re-checks the budget (the
    serialized write window is authoritative), refuses second-guessing a
    competing writer's committed success (first writer wins), inserts the
    attempt row and — on success — every decision and the latency record,
    or — on failure — marks the bucket's messages UNCLASSIFIED. A partially
    recorded attempt is impossible; a crash mid-call records nothing at all.
    """
    if _attempt_count(connection, bucket_id) >= budget:
        return _AttemptResult(attempt_id=None, classified=False, decision_count=0)
    request = _bucket_request(connection, bucket_id)
    started_at = _utc_now()
    failure: Exception | None = None
    validated: tuple[tuple[str, str, str], tuple[tuple[int, str], ...]] | None = None
    call_latency: float | None = None
    call_started_mono = time.monotonic()
    try:
        response = classifier.classify(request)
        # The latency metric covers the adapter call alone (audit F7);
        # backend validation time is deliberately not part of it.
        call_latency = time.monotonic() - call_started_mono
        validated = _validated_response(response, request)
    except Exception as error:  # noqa: BLE001 - FR-REL-07/AC-12: every
        # classifier-side failure is an execution failure, recorded as
        # FAILED_RETRYABLE. Interrupts (KeyboardInterrupt/SystemExit) are
        # BaseException and propagate unchanged, leaving nothing written.
        failure = error
    with transaction(connection):
        # Authoritative budget re-check inside the serialized writer window
        # (defense in depth against writers outside this connection's
        # ordering — compare backend.ingest's UNIQUE-conflict fallback).
        if _attempt_count(connection, bucket_id) >= budget:
            return _AttemptResult(attempt_id=None, classified=False, decision_count=0)
        # First writer wins (audit F1 race guard): a competing writer's
        # committed SUCCEEDED attempt precedes ours — never record a second
        # classification for the bucket, never duplicate its decisions.
        if failure is None and _has_success(connection, bucket_id):
            return _AttemptResult(
                attempt_id=None, classified=False, decision_count=0, superseded=True
            )
        finished_at = _utc_now()
        outcome: str
        error_text: str | None
        model_id: str | None
        model_version: str | None
        config_version: str | None
        if failure is None and validated is not None:
            (model_id, model_version, config_version) = validated[0]
            outcome = "SUCCEEDED"
            error_text = None
        else:
            model_id = None
            model_version = None
            config_version = None
            outcome = "FAILED_RETRYABLE"
            error_text = _redact_error_text(f"{type(failure).__name__}: {failure}")
        cursor = connection.execute(
            "INSERT INTO processing_attempt"
            " (stage, bucket_id, started_at, finished_at, outcome, error,"
            "  model_id, model_version, config_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stage,
                bucket_id,
                started_at,
                finished_at,
                outcome,
                error_text,
                model_id,
                model_version,
                config_version,
            ),
        )
        attempt_id = int(cursor.lastrowid or 0)
        decision_count = 0
        if outcome == "SUCCEEDED" and validated is not None:
            decided_at = _utc_now()
            for message_id, decision in validated[1]:
                connection.execute(
                    "INSERT INTO relevance_decision"
                    " (bucket_id, message_id, attempt_id, decision,"
                    "  model_id, model_version, prompt_version, decided_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        bucket_id,
                        message_id,
                        attempt_id,
                        decision,
                        model_id,
                        model_version,
                        config_version,
                        decided_at,
                    ),
                )
                decision_count += 1
            connection.execute(
                "INSERT INTO latency_measurement"
                " (metric, value_seconds, recorded_at, processing_attempt_id)"
                " VALUES (?, ?, ?, ?)",
                (
                    LATENCY_METRIC_LABEL,
                    float(call_latency) if call_latency is not None else 0.0,
                    finished_at,
                    attempt_id,
                ),
            )
        else:
            # FR-REL-07: affected messages are marked UNCLASSIFIED (the
            # 0002 flag column); no decision says IRRELEVANT. Clearing the
            # flag after recovery is deferred by standing owner decision.
            connection.execute(
                "UPDATE source_message SET flag_unclassified = 1"
                " WHERE id IN (SELECT message_id FROM bucket_message"
                "               WHERE bucket_id = ?)",
                (bucket_id,),
            )
        # Masked-input audit snapshot (approved F3, NFR-AUD-02): one durable
        # artifact per committed adapter-executing attempt — success or
        # execution failure, primary or fallback — captured inside the same
        # transaction that commits the attempt row. Refused and superseded
        # paths returned above, so absence is consistent by construction.
        serialized_request = _serialize_masked_request(request)
        connection.execute(
            "INSERT INTO relevance_attempt_input"
            " (attempt_id, masking_ruleset_version, policy_version,"
            "  masked_request, request_sha256, captured_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                attempt_id,
                MASKING_RULESET_VERSION,
                CLASSIFICATION_POLICY_VERSION,
                serialized_request,
                _request_sha256(serialized_request),
                finished_at,
            ),
        )
    return _AttemptResult(
        attempt_id=attempt_id,
        classified=outcome == "SUCCEEDED",
        decision_count=decision_count,
    )


def _converge_after_failure(
    connection: sqlite3.Connection,
    bucket_id: int,
    attempt_ids: tuple[int, ...],
    budget: int,
) -> BucketOutcome | None:
    """Post-failure lifecycle wiring through the legal edge set only.

    Returns ``None`` — read "skip this bucket, contribute nothing" — when a
    competing writer's committed SUCCEEDED attempt already exists: a
    failure must never mark FAILED over a success (audit F1; the finalize
    convergence advances such a bucket without new attempts). Within the
    remaining relevance-stage budget the bucket pauses in AWAITING_RECOVERY
    (from CLOSED or already-AWAITING — the idempotent same-state no-op);
    once the budget is consumed it is left FAILED for manual re-queue
    (NFR-REL-03). The transition happens only after the attempt records
    are durably committed.
    """
    if _has_success(connection, bucket_id):
        return None
    if _attempt_count(connection, bucket_id) >= budget:
        transition_bucket_processing_state(connection, bucket_id, BucketState.FAILED)
        return BucketOutcome(
            bucket_id=bucket_id,
            status=RelevanceOutcome.EXHAUSTED,
            attempt_ids=attempt_ids,
            decision_count=0,
        )
    transition_bucket_processing_state(
        connection, bucket_id, BucketState.AWAITING_RECOVERY
    )
    return BucketOutcome(
        bucket_id=bucket_id,
        status=RelevanceOutcome.RETRYING,
        attempt_ids=attempt_ids,
        decision_count=0,
    )


def _process_bucket(
    connection: sqlite3.Connection,
    bucket_id: int,
    primary: RelevanceClassifier,
    fallback: RelevanceClassifier | None,
    *,
    budget: int,
) -> BucketOutcome | None:
    """Classify one bucket: primary attempt, then fallback only on failure.

    NFR-REL-07: the fallback never runs while the primary might still
    succeed; both attempts are independently recorded; the successful
    attempt supplies the effective (persisted) result and the other's
    failure record stays visible. Success advances the bucket to
    PROCESSED through the legal edge set only. Returns ``None`` when the
    bucket was already classified by a competing writer between discovery
    and commit (first writer wins — nothing is contributed, nothing is
    advanced here).
    """
    created: list[int] = []
    primary_result = _run_attempt(
        connection, bucket_id, primary, STAGE_RELEVANCE_CLASSIFICATION, budget
    )
    if primary_result.attempt_id is None:
        if primary_result.superseded:
            return None
        return _converge_after_failure(connection, bucket_id, (), budget)
    created.append(primary_result.attempt_id)
    if primary_result.classified:
        transition_bucket_processing_state(
            connection, bucket_id, BucketState.PROCESSED
        )
        return BucketOutcome(
            bucket_id=bucket_id,
            status=RelevanceOutcome.CLASSIFIED,
            attempt_ids=tuple(created),
            decision_count=primary_result.decision_count,
        )
    if fallback is None:
        return _converge_after_failure(
            connection, bucket_id, tuple(created), budget
        )
    fallback_result = _run_attempt(
        connection,
        bucket_id,
        fallback,
        STAGE_RELEVANCE_FALLBACK_CLASSIFICATION,
        budget,
    )
    if fallback_result.attempt_id is None:
        if fallback_result.superseded:
            return None
        return _converge_after_failure(
            connection, bucket_id, tuple(created), budget
        )
    created.append(fallback_result.attempt_id)
    if fallback_result.classified:
        transition_bucket_processing_state(
            connection, bucket_id, BucketState.PROCESSED
        )
        return BucketOutcome(
            bucket_id=bucket_id,
            status=RelevanceOutcome.CLASSIFIED,
            attempt_ids=tuple(created),
            decision_count=fallback_result.decision_count,
        )
    return _converge_after_failure(connection, bucket_id, tuple(created), budget)


def requeue_failed_bucket(
    connection: sqlite3.Connection, bucket_id: int
) -> bool:
    """Manual re-queue of a FAILED bucket (NFR-REL-03, owner operation).

    Moves FAILED → AWAITING_RECOVERY through the legal edge set; the next
    pass resumes it. Returns ``True`` when the transition was applied,
    ``False`` when the bucket was already AWAITING_RECOVERY (idempotent
    same-state no-op, retry safety) and raises loudly for any other state
    — a healthy bucket is never "re-queued".
    """
    row = connection.execute(
        "SELECT processing_state FROM bucket WHERE id = ?", (bucket_id,)
    ).fetchone()
    if row is None:
        raise UnknownBucketError(f"no bucket with id {bucket_id}")
    state = BucketState(str(row["processing_state"]))
    if state is BucketState.AWAITING_RECOVERY:
        return False
    if state is not BucketState.FAILED:
        raise InvalidBucketTransitionError(
            f"manual re-queue expects a FAILED bucket,"
            f" got {state.name} for bucket {bucket_id}"
        )
    return transition_bucket_processing_state(
        connection, bucket_id, BucketState.AWAITING_RECOVERY
    )


def run_relevance_pass(
    connection: sqlite3.Connection,
    primary: RelevanceClassifier,
    fallback: RelevanceClassifier | None = None,
    *,
    max_attempts_per_bucket: int = DEFAULT_MAX_ATTEMPTS_PER_BUCKET,
    budget_overrides: Mapping[int, int] | None = None,
) -> RelevancePassResult:
    """Run one deterministic classification pass over eligible buckets.

    Buckets are discovered, ordered (ascending id), classified and
    lifecycle-advanced per the module decisions; success means validated
    decisions for every message, latency and audit records persisted, and
    the bucket PROCESSSED through a legal transition. A Failure that is
    not bucket-specific aborts the pass and raises
    :class:`RelevancePassError` carrying the committed per-bucket prefix
    and the finalized ids with the original error chained — an
    incomplete pass is never reported as successful. Interrupts
    (``KeyboardInterrupt``/``SystemExit``) propagate unchanged; their
    in-flight attempt leaves nothing written. Already-classified and OPEN
    buckets are untouched; a repeat pass is a no-op over converged work.
    Buckets whose competitor committed a SUCCEEDED attempt between
    discovery and commit are skipped (first writer wins): no attempt, no
    outcome, no lifecycle change from this pass — the finalize
    convergence advances them.

    Terminal-only buckets (approved F4): a CLOSED bucket whose attempts
    hold ``FAILED_PERMANENT`` but neither a success nor a retryable
    failure is converged to FAILED through the existing legal edge and
    reported in ``terminal_failed`` — never retried, never silently
    invisible. AWAITING-only-permanent buckets are owner-authorized
    recovery work (they exist only via a manual re-queue) and are simply
    processed like any requeued work. ``max_attempts_per_bucket`` must be
    a positive
    integer and every ``budget_overrides`` value too; anything else raises
    ``ValueError`` before any state change.

    Authorized recovery (approved F4/R3): a manually re-queued
    budget-exhausted bucket resumes only through an explicit override —
    ``budget_overrides[bucket_id]`` is that bucket's raised TOTAL attempt
    cap for this pass (must exceed its accumulated attempts, or the
    refusal happens before any adapter call and the exhaustion is reported
    honestly — EXHAUSTED, back to FAILED, zero new rows). Buckets without
    an override keep the ordinary budget; history stays append-only.
    """
    if (
        isinstance(max_attempts_per_bucket, bool)
        or not isinstance(max_attempts_per_bucket, int)
        or max_attempts_per_bucket < 1
    ):
        raise ValueError("max_attempts_per_bucket must be a positive integer")
    overrides: Mapping[int, int] = dict(budget_overrides or {})
    for bucket_id, value in overrides.items():
        if isinstance(bucket_id, bool) or not isinstance(bucket_id, int):
            raise ValueError(
                "budget override keys must be integer bucket ids,"
                f" got {bucket_id!r}"
            )
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                "budget override for bucket"
                f" {bucket_id} must be a positive integer, got {value!r}"
            )
    outcomes: list[BucketOutcome] = []
    finalized: list[int] = []
    terminated: list[int] = []
    try:
        for bucket_id in _finalize_candidates(connection):
            if transition_bucket_processing_state(
                connection, bucket_id, BucketState.PROCESSED
            ):
                finalized.append(bucket_id)
        for bucket_id in stalled_terminal_buckets(connection):
            if transition_bucket_processing_state(
                connection, bucket_id, BucketState.FAILED
            ):
                terminated.append(bucket_id)
        for bucket_id in _discovery_targets(connection):
            outcome = _process_bucket(
                connection,
                bucket_id,
                primary,
                fallback,
                budget=overrides.get(bucket_id, max_attempts_per_bucket),
            )
            if outcome is not None:
                outcomes.append(outcome)
    except RelevancePassError:
        raise
    except Exception as error:
        raise RelevancePassError(
            tuple(outcomes),
            tuple(finalized),
            f"relevance pass aborted by a non-bucket-specific error: {error}",
            tuple(terminated),
        ) from error
    return RelevancePassResult(
        finalized=tuple(finalized),
        outcomes=tuple(outcomes),
        terminal_failed=tuple(terminated),
    )
