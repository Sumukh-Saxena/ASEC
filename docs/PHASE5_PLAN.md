# ASEC Phase 5 Plan — Proposal Generation & Validation

Status: plan only (no implementation). Derived from `docs/SRS.md` (requirements
authority), `AGENTS.md` (workflow, architecture and safety rules), and the
actual repository state at HEAD `39d694d` (verified 2026-10-21). Storage
existence never implies behavior is implemented; tests-first and the slice
workflow of `AGENTS.md` apply to every step below.

Produced as **one coordinated whole-phase plan** under the owner-directed
pattern used for Phases 3 and 4 (single batch, single consolidated correction
pass at phase end), with internal workstreams instead of sequential
slice-by-slice approval cycles. Awaiting owner approval before any
implementation.

---

## 1. Verified baseline (2026-10-22 re-check, read-only)

- Branch `main`; HEAD `39d694d3ae11736a5ae2363925e4f281a4a5d82c` — "Complete
  Phase 4 relevance: durable masked-input snapshots and terminal-failure
  convergence (F3/F4)". The Phase 4 work is **committed and pushed**; at
  discovery time the tree was clean except this plan file; nothing staged.
- Full battery at this HEAD, re-executed during the 2026-10-21 acceptance
  review (not merely claimed): **755 tests / 0 failures / 0 errors / 0 skips**;
  ruff clean; mypy clean (12 source files); `git diff --check` clean;
  disposable DB probe PASS (integrity ok, FK empty).
- **Owner decisions recorded 2026-10-22 (authorizing implementation):**
  G1 eligible roles = stored tokens `AH`, `Head` (trimmed, case-insensitive,
  active flag required), overridable via config key `PROPOSAL_ELIGIBLE_ROLES`;
  G1b `PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP` flag (default **off**,
  schema/code/tests in both settings, owner may flip the default later);
  C1 opaque mention handles approved **for the proposal stage only** (the
  relevance/0013 retrofit is a plan-only owner-approval item); PB-1–PB-7 are
  plan-only (§15) until the owner writes `APPROVED PB-<n>`.
- Plans in repo: `docs/PHASE2_PLAN.md` (Carries the phase records for Phases
  2–4 in its §3 slices and §10–§13 records). `docs/PHASE5_PLAN.md` (this file).
- `/slice` has no separate command file in the repo; the prescribed format is
  the `AGENTS.md` plan essentials (files, requirements, tests, commands,
  risks, unresolved questions) plus the `PHASE2_PLAN.md` precedent, which this
  document follows.

## 2. Phase 5 authoritative scope (from `docs/SRS.md`, unchanged)

Phase 5 implements the **executable proposal-generation stage**:

- SRS §3.4 **FR-PRP-01..16** (FR-PRP-17 = reviewer notification is deferred
  to the notification/review phase — §2 non-goal 2), with:
  - **FR-PRP-01** run for every closed bucket containing a RELEVANT message
    from an eligible sender; no second relevance filter.
  - **FR-PRP-02** volunteer/unknown/unmapped: stored and classified, no
    proposal, no notification (AC-10).
  - **FR-PRP-03** exactly one primary source message (relevant + eligible);
    other bucket messages as context (Post-MVP: multi-primary FR-PM-07).
  - **FR-PRP-04** zero, one or several proposals; zero is a valid result
    (AC-03). **FR-PRP-05** per-proposal independence (AC-02).
  - **FR-PRP-06..09** types/modes/task types verbatim.
  - **FR-PRP-10** assignees as text; **backend resolves** (mention by user
    ID; name vs active roster names in the source vertical: one match
    resolves, several unresolved, none flagged) (AC-11).
  - **FR-PRP-11** no invented values; missing = missing (DR-04); visible
    placeholder rendering is the review screen's job (FR-UI-06, later phase).
  - **FR-PRP-12** generation run, source and context messages, model and
    version, prompt/config version, timestamp recorded on every proposal.
  - **FR-PRP-13** validation is **structural only** (required fields, types,
    allowed values); valid structure is not correct content.
  - **FR-PRP-14** valid proposals stored and retrievable across worker
    failure; no automatic expiry.
  - **FR-PRP-15** a silence, reaction or "Rahul?" is never anyone's
    acceptance (AC-09 evidence is preserved; nothing inferred).
  - **FR-PRP-16** a proposal never becomes a task without human review
    (Phase 5 writes no task/announcement rows — verified by tests).
- SRS §5 **EIR-03 (proposal half)** and §7 **AI-01/AI-02/AI-03/AI-07-position**
  for the stateless contract; masking NFR-SEC-04 before every call (AC-21).
- Reliability: **NFR-REL-01/02/03/04** (attempt recording, retry-safety,
  recoverability, crash-safety), **NFR-REL-07** (fallback only after primary
  failure; both attempts recorded), **NFR-PERF-02** (proposal latency
  recording), **NFR-AUD-02** (model/version/prompt provenance + reconstructable
  masked input per AI output), **NFR-MNT-01** (model IDs and prompt versions
  as configuration-ish constants — see decision DC-8).
- Data rules DR-01..DR-07 apply (proposal traces back to attempt, bucket,
  source messages; forward to review).
- SRS §10.2 acceptance inside Phase 5's verification scope: **AC-02, AC-03,
  AC-10, AC-11, AC-12 (proposal half), AC-21**; AC-06/AC-07/AC-08 depend on
  review-phase behavior and cannot be fully accepted until that phase.

**Explicit non-goals (standing boundaries; do not implement in Phase 5):**

1. The **proposal-review service** (FR-REV-01..10 execution, edit/accept/
   reject endpoints), **Part B PB-1–PB-7** — their specifications do not exist in
   the repository and require owner-supplied specs and explicit approval
   (owner gate G2).
2. **Notification emission or delivery** (FR-PRP-17, FR-NOT-01..07): the
   notification storage exists (0008); Phase 5 delivers the documented
   hand-off (§8 below) and no notification rows (FR-NOT integration is a
   later phase; L932 of PHASE2_PLAN).
3. **Task/announcement creation** (FR-TSK-*, FR-XV-*, FR-REV-05/10) —
   human-review boundary (FR-PRP-16, §9 Constraints).
4. **Live model integration**: no credentials, transport, real endpoint or
   model workers (§13.8 of PHASE2_PLAN checklist; external gate G3). The
   adapter is an in-process `Protocol` exactly like `RelevanceClassifier`.
5. Post-MVP: FR-PM-01..10 (reactions-as-acceptance, routing, prior-bucket
   context, context processing, promotion, batching, multi-primary,
   reprocessing tools, FC, voice/incidents).
6. Untouched standing decisions: post-recovery UNCLASSIFIED clearing
   (§13.6), effective-correction selection, progress-scheduling authority
   (§7), Piggyback transport; SRS checks 7–8 remain waived and are not run
   or claimed (as established).
7. No changes to ingestion, bucketing, bucket-lifecycle rules, or the
   relevance service beyond reusing its published contracts. No review
   policies or cross-vertical authorization invented.

## 3. Established storage and code to reuse (no rebuild)

Already migrated and tested (storage side complete):

- `0001_core_roster.sql` — `person` (active flag, free-text `role`,
  `discord_user_id` UNIQUE, name), `person_vertical`, `vertical` (UNIQUE
  name), `source_group` (nullable `vertical_id` = unmapped).
- `0002_source_messages.sql` — `source_message` (authoritative content,
  timestamps, flags LATE/UNMAPPED/UNKNOWN_SENDER/UNCLASSIFIED,
  `UNIQUE(source, source_message_id)`), `message_reaction` (evidence only).
- `0003_buckets.sql` — `bucket` + `bucket_message` (at most one bucket per
  message; same-group composite FKs).
- `0004_processing_attempts.sql` — `processing_attempt` (free-text `stage`
  per the AU-2 convention — e.g. `PROPOSAL_GENERATION`; outcome vocabulary
  SRS-verbatim; `error` masked+cap-capped by the relevance service pattern).
- `0005_proposals.sql` — `proposal` (ptype/mode/task_type/verticals/
  people-needed encoding/provenance/review_state default PENDING/
`structurally_valid`), `proposal_attribute`, `proposal_affected_vertical`,
  `proposal_context_message`, `proposal_revision` (+ revision links).
- `0012_resolution_and_mentions.sql` — `proposal_assignee`
  (RESOLVED/UNRESOLVED/FLAGGED + MENTION/NAME_MATCH, CHECK-consistent),
  `proposal_decision`, `message_mention` (opaque user-ID evidence, written
  by ingestion).
- `0008_notifications.sql` (storage only — Phase 5 writes none),
  `0009_announcements.sql`, `0006_tasks.sql` (downstream FK targets: task
  UNIQUE(proposal_id), announcement UNIQUE(proposal_id) — untouched).
- `0011_latency_measurements.sql` — `latency_measurement` (free-text metric
  label; relevance precedent `CLASSIFIER_LATENCY`).
- `0013_relevance_attempt_input.sql` — the F3 artifact pattern Phase 5
  mirrors (see migration 0014).

Executable patterns to reuse (never rewritten; consumed only):

- `backend/bucket_lifecycle.py` — read `bucket_processing_state`; **no
  transitions from Phase 5** (buckets are already PROCESSED; no new edges).
- `backend/orchestration.py::processing_queue` — irrelevant here; Phase 5
  discovers its own work from attempts + decisions (§4).
- `backend/bucketing.py` — untouched (formation/closure/progress authority).
- `backend/masking.py::mask_pii` — applied to message content in every
  request build; originals stay in `source_message` (DR-03).
- `backend/db/connection.py::transaction` — `BEGIN IMMEDIATE`; rollback on
  any `BaseException`; the single transaction per attempt.
- `backend/config.py` — read-only config view; no `.env` loading; secrets
  never read in Phase 5 (no credentials exist in scope).
- `backend/relevance.py` — publishes `STAGE_RELEVANCE_*`,
  `DECISION_VOCABULARY`, the frozen request/response contract, the
  two-valued attempt writer, budget/refusal/supersede semantics, and
  `MASKING_RULESET_VERSION` (reused by the proposal artifact for lineage
  consistency). Phase 5 may read its tables and types but never writes its
  rows or state.

## 4. Data flow, selection and transaction boundaries

### 4.1 Stage trigger (FR-PRP-01 implementation)

A bucket is **proposal work** when, from stored authoritative data only:

```
bucket PROCESSED
AND its group has a vertical (source_vertical_id NOT NULL; FR-PRP-02/ING-09)
AND EXISTS processing_attempt(stage IN (RELEVANCE_CLASSIFICATION,
       RELEVANCE_FALLBACK_CLASSIFICATION), bucket_id, outcome = 'SUCCEEDED')
AND EXISTS relevance_decision(decision = 'RELEVANT') for a message whose
       sender is eligible (sender_person_id NOT NULL, person.active = 1,
       person.role in the owner-approved eligible-role set — gate G1)
AND NOT EXISTS processing_attempt(stage IN (PROPOSAL_GENERATION,
       PROPOSAL_FALLBACK_GENERATION), bucket_id, outcome = 'SUCCEEDED')
```

- Stage-scoped char counts mirror `_attempt_count` (relevance.py:584–600):
  only the two proposal stage tokens count toward the proposal budget, and
  relevance budgets are untouched.
- Buckets with relevant messages **only from volunteers/unknown senders are
  never attempted** (FR-PRP-02, AC-10; no attempt row, no call).
- Public read helper `proposal_pending_buckets(connection, *, budget)` (and
  a plain `proposal_exhausted_buckets(...)` analog of
  `stalled_terminal_buckets` — no state change, honest inventory for admins;
  NFR-REL-06 visibility at the stage level).

### 4.2 Request build (EIR-03 proposal half + NFR-SEC-04 + FR-PRP-03)

Frozen dataclasses in `backend/proposals.py`, mirroring
`RelevanceRequest`/`ContextMessage`:

- `CandidateMessage` — message_id, sender_name, event_timestamp,
  reply_reference, masked_content, per-request mention handles (opaque,
  C1). **Correlation C1 (owner-approved 2026-10-22):** the proposal request
  carries **no real Discord identifiers** — no sender `discord_user_id`
  (sender display name only), and not even the channel id (reconstruction
  goes through `bucket_id`, which IS in the request). Mentions in request
  messages are **per-request opaque handles** `@m1, @m2, …`, assigned in
  order of first appearance scanning bucket members by ascending
  `source_message.id` (each message's own mentions in `message_mention`
  position order — deterministic, tested). The backend keeps the
  handle → Discord user ID map and uses it for the MENTION resolution
  route (§4.5); the model never sees a real snowflake.
- Context = **the whole bucket** (all member messages, masked, with
  handle/reply data per C1) — "Other messages from the bucket may be
  attached as context" (FR-PRP-03).
- `ProposalGenerationRequest` — bucket_id, vertical name, candidate
  messages, context messages, instructions text (`PROPOSAL_PROMPT`
  constant mirroring `CLASSIFICATION_POLICY`, carrying
  FR-PRP-08/11/15 semantics + "no invented values").
- Content masked with `mask_pii` exactly once at build (AC-21).
- Two canonical serializations of one request, both sorted-key JSON
  (decision DC-12, owner-specified by the C1 bullets):
  the **dispatch form** (what the adapter receives: no
  `mention_handle_map`) and the **artifact form** (the dispatch JSON plus
  a `mention_handle_map` member: handle → real Discord user ID, so the
  request is fully reconstructable, NFR-AUD-02). `request_sha256` covers
  the **dispatch** form (the string handed to the adapter); the artifact
  column stores the artifact form.
- **C1 retrofit note (owner-approval item, NOT in this phase):**
  relevance currently sends raw Discord user IDs as mention handles
  (relevance.py:446–457) and real sender IDs in its requests
  (incl. `group_discord_channel_id`); 0013 snapshots them verbatim. Making
  the relevance stage opaque requires a `0013` parity design + migration
  — recorded as an owner-approval item only (the owner declined it for
  Phase 5).

### 4.3 Adapter protocol (stateless, no authority)

```python
class ProposalGenerator(Protocol):
    def generate(self, request: ProposalGenerationRequest) -> ProposalResponse: ...
```

- `ProposalResponse` — model provenance (model_id, model_version,
  prompt_version non-blank) + `proposals: tuple[ProposalDraft, ...]` where
  `ProposalDraft` = primary_message_id (must be one of the presented
  candidates), ptype (TASK|ANNOUNCEMENT), description (str|None),
  assignment_mode (EXPLICIT|OPEN|NONE), proposed_assignees
  (tuple[str, ...]|None), people_needed (int|None),
  people_needed_unlimited (0|1), task_type (INTERNAL|CROSS_VERTICAL),
  suggested_responsible_vertical (str|None — vertical **name**),
  affected_verticals (tuple[str, ...] — names), attributes
  (tuple[(key, value|None), ...]).
- Untrusted until validated (AI-02); the backend validates before persisting.

### 4.4 Structural validation (FR-PRP-13; three tiers — decisions DC-3/DC-4, refined by C2)

- **Tier 1 (envelope)**: response is a well-formed envelope with non-blank
  provenance and a tuple of items — otherwise the **whole attempt is
  FAILED_RETRYABLE** (EIR-03: malformed output is an execution failure,
  never a valid empty result).
- **Tier 2 (item violations) — C2 refinement (owner, 2026-10-22):** a
  Tier-2-invalid item is **never a proposal row** (the service only stores
  structurally valid items, always `structurally_valid = 1`); it is
  recorded — with a documented reason code — in the 0014 artifact's
  `rejected_items` JSON column ({reason_code, item_index, …}), so
  FR-PRP-13's requirement that unrepresentable invalid output survives
  audit is met without poisoning the proposal table. Invalid items never
  block their valid siblings (FR-PRP-05, AC-02). Codes (internal
  annotation, not state vocabulary; fixed set, tested):
  `PRIMARY_NOT_CANDIDATE`, `TYPE_INVALID`, `MODE_INVALID`,
  `TASK_TYPE_INVALID`, `MODE_ASSIGNEE_CONFLICT`, `PEOPLE_NEEDED_CONFLICT`,
  `PEOPLE_NEEDED_INVALID`, `FIELD_TYPE_INVALID`, `FIELD_VALUE_INVALID`,
  `ATTRIBUTES_INVALID`, `DUPLICATE_ATTR_KEY`, `UNKNOWN_HANDLE`.
  UNKNOWN_HANDLE: an assignee token shaped like a request handle
  (`^@m[0-9]+$`) that is not in the request map is a Tier-2 violation,
  never a guess (C1).
- **Tier 3 (content)**: anything else — missing fields (NULL stays NULL),
  an EXPLICIT task nobody names (allowed; reviewer fills at review), an
  unknown vertical name (dropped per §4.5), questionable wording — is
  *content*: stored as generated (FR-REV-03/06, later phase).
- Zero items = a valid result (FR-PRP-04, AC-03) — attempt SUCCEEDED with
  zero proposals. But a non-empty response whose **every** item fails
  Tier-2 is a **failed attempt** (decision DC-3, owner-vetoable): attempt
  FAILED_RETRYABLE whose artifact row still stores `raw_response` (masked,
  capped) and the full `rejected_items` list — never silently presented as
  a legitimate "zero proposals".
- Blank-string values are separator decisions, kept uniform (DR-04
  distinguishability, decision DC-16): NULL = missing, a blank/trimmed
  empty string = `FIELD_VALUE_INVALID` (never coerced to NULL).
- Allowed schemas kept tolerant per 0005's own encoding: `people_needed`
  XOR `unlimited`; OPEN/NONE may not carry names (FR-PRP-08 shall-never →
  Tier-2); a TASK with OPEN + number/UNLIMITED/unfilled — all valid
  shapes. An EXPLICIT task with zero names stays Tier-3 content (stored;
  reviewer decides), per owner refinement of FR-PRP-08's "treat as OPEN"
  directive.

### 4.5 Resolution execution (FR-PRP-10, AC-11) — decision DC-6

Resolved **at storage time**, persisted per `proposal_assignee` (0012):

- **MENTION route (C1)**: assignee token equals a per-request handle in
  the request map → backend resolves via the mapped Discord user ID
  (`person.discord_user_id`, UNIQUE). Exact; no roster search; inactive is
  still RESOLVED (the ID is exact — micro-decision DC-15, owner-vetoable);
  a mapped ID with no roster person → FLAGGED. A handle-shaped token not
  in the map never reaches resolution (Tier-2, §4.4).
- **NAME_MATCH route (DC-6, owner-vetoable; pinned by tests)**: exact
  trimmed + case-folded equality against display names of **active**
  roster persons (`person.active = 1`) who are members of the **source**
  vertical (`person_vertical`); matching happens only inside the source
  vertical, so a same-named colleague outside it is invisible on this
  route → the token stays FLAGGED (C6 explicitly re-pins this). One match
  → RESOLVED; several → UNRESOLVED; none → FLAGGED. Never a guess (the
  CHECK makes an implicit person unrepresentable).
- What is stored on the proposal: `proposed_assignees_text` = the assignee
  tokens joined ", " (single TEXT column); one `proposal_assignee` row per
  token with `assignee_text` verbatim.
- Resolution is not authority: BR-03 (a proposed assignee is not an
  agreeing assignee); acceptance gating stays FR-REV-06 (later phase).
- Vertical-name fields: `suggested_responsible_vertical` resolved by exact
  (case-insensitive) `vertical.name`; unresolvable → NULL (missing stored
  as missing; reviewer decides — FR-REV-03). Unresolvable affected-vertical
  names are dropped (no invented FKs) — reviewer re-adds at review.

### 4.6 Persistence transaction (one transaction; NFR-REL-01/04)

For one discovered bucket, one attempt:

1. Budget pre-check (stage-scoped attempt count < budget) → refuse before
   any adapter call, and report exhaustion honestly (zero rows).
2. Build the masked request; call the primary generator (NFR-PERF-02
   measures the adapter call alone: monotonic window).
3. Validate (§4.4); resolve (§4.5).
4. `with transaction(connection):` — authoritative budget re-check;
   **first-writer-wins** guard: skip with no contribution if a competing
   SUCCEEDED proposal attempt for the bucket already exists
   (superseded paths store nothing, mirroring relevance F1); then insert:
   - the `processing_attempt` row (stage `PROPOSAL_GENERATION`, outcome
     SUCCEEDED for a well-formed response or FAILED_RETRYABLE otherwise),
   - on success: every valid `proposal` row with provenance copied from the
     response (FR-PRP-12), plus `proposal_attribute`,
     `proposal_affected_vertical`, `proposal_context_message` (primary
     message **included**, FR-PRP-03), `proposal_assignee` rows,
   - the `latency_measurement` row (`PROPOSAL_LATENCY`) for recorded runs
     (relevance precedent: the adapter call alone), plus a second
     `PROPOSAL_STAGE_DELAY` row when computable — C5/NFR-PERF-02: delay
     from the latest SUCCEEDED relevance attempt `finished_at` to this
     attempt's `finished_at` (DC-14: bucket-close wall time is not stored
     by 0003, so the latest relevance finish is the earliest available
     post-close anchor; no negative values; skipped when no parseable
     anchor exists; targets never compared in code — NFR-PERF-01),
   - the `0014` artifact row (NFR-AUD-02) — same single transaction, so
     snapshot-iff-commit-accepted-attempt (F3 invariants).
5. Return the per-bucket outcome (GENERATED n | RETRYING | EXHAUSTED), no
   bucket-state change, no task/announcement, no notification rows.

Failure handling: adapter exception, malformed envelope, or validation
failure at Tier 1 → `FAILED_RETRYABLE` attempt row (two-valued writer;
FR-PRP-15/AC-12 half), then fallback only after primary failure
(NFR-REL-07; both attempts recorded; effective result = the successful
one, i.e. the primary's proposals if it succeeded). `FAILED_PERMANENT` is
never written by the service (schema accepts it; externally seeded rows
merely count toward the budget). Interrupts propagate untouched
(`BaseException` → transaction rollback → nothing written; recovery is a
plain re-run of the pass — discovery is state-derived, idempotent).

### 4.7 Pass orchestration (the Phase 5 entry point)

`run_proposal_pass(connection, generator, fallback=None, *,
max_attempts_per_bucket=DEFAULT_PROPOSAL_ATTEMPTS_PER_BUCKET=3,
budget_overrides: Mapping[int, int] | None = None,
policy: ProposalPolicy | None = None) -> ProposalPassResult`

- `ProposalPolicy` (G1/G1b, owner-approved): `eligible_roles = ("AH",
  "Head")` default, matched trimmed + case-insensitively, `active = 1`
  required; `require_source_vertical_membership = False` default (G1b).
  `None → defaults`; `load_proposal_policy(config)` reads the config keys
  `PROPOSAL_ELIGIBLE_ROLES` (comma-separated tokens, non-blank validated)
  and `PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP` (truthy
  `1/true/yes/on`, falsy `0/false/no/off/<unset>`; anything else fails
  loud) — no other config surface.

- Discovery ordered by bucket id (deterministic); per-bucket attempt with
  the mirrors of relevance's budget/refusal/supersede semantics; overrides
  = raised total cap with the same loud key/value validation (ValueError
  before any state change) and the same exhaustion honesty (EXHAUSTED →
  recorded, zero rows) — direct reuse of the owner-approved Phase 4 (R3)
  pattern.
- `ProposalPassResult` (outcomes, regenerated bucket ids, exhausted ids);
  `ProposalPassError` mirrors the relevance abort shape (durable prefix +
  chained cause; an incomplete pass is never reported successful).
- Manual recovery (NFR-REL-03, proposal stage): the bucket stays PROCESSED;
  recovery = an authorized new pass with an override ≥ accumulated count
  (no state to requeue; overrides replace the bucket-state requeue here —
  decision DC-9, documented).
- Every lifecycle-correct property transferred from relevance: append-only
  history (no UPDATE/DELETE on prior rows anywhere), no duplicate proposals,
  no duplicate successful runs (first-writer-wins), retry-safe.
- **C4 backlog visibility (NFR-REL-06, FR-UI-05):** a sibling read-only
  `proposal_backlog(connection, *, budget, policy=None) ->
  ProposalBacklog` — count of buckets awaiting proposals, bucket ids still
  awaiting theirs, the age of the oldest awaiting bucket on the event-time
  convention (`orchestration.pipeline_backlog`: parseable instant, ties
  by id, age anchored to the group's `source_group_progress.progress_to`),
  and the exhausted bucket ids (attempts ≥ budget). Zero writes; no state
  change; `orchestration.PipelineBacklog` itself is untouched (other saved
  tests pin its shape).

## 5. Expected file changes (whole phase)

| Path | Change | Why |
|---|---|---|
| `backend/db/migrations/0014_proposal_attempt_input.sql` | new, additive | NFR-AUD-02 + C1/C2 for the proposal stage: `proposal_attempt_input(attempt_id INTEGER NOT NULL UNIQUE FK→processing_attempt ON DELETE RESTRICT, masking_ruleset_version TEXT NOT NULL, policy_version TEXT NOT NULL, masked_request TEXT NOT NULL, request_sha256 TEXT NOT NULL, raw_response TEXT NULL, response_sha256 TEXT NULL, rejected_items TEXT NULL, captured_at TEXT NOT NULL` + non-blank CHECKs + the raw/sha pairing CHECK), mirroring `0013` with the C1 (handle map inside `masked_request`) and C2 (`raw_response` masked + capped, hash over the stored text, Tier-2 rejections with reason codes) extensions. Only migration planned. |
| `backend/proposals.py` | new | Stage types, constants (`STAGE_PROPOSAL_GENERATION` = "PROPOSAL_GENERATION", `STAGE_PROPOSAL_FALLBACK_GENERATION`, `LATENCY_METRIC_LABEL = "PROPOSAL_LATENCY"`, `STAGE_DELAY_METRIC_LABEL = "PROPOSAL_STAGE_DELAY"` (C5), `DEFAULT_PROPOSAL_ATTEMPTS_PER_BUCKET = 3`, `PROPOSAL_PROMPT_VERSION = "proposal-prompt-v1"`, `PROPOSAL_PROMPT`, `_PROPOSAL_EDGES = ()` — none), ProposalPolicy + `load_proposal_policy` (G1/G1b), serializer + hash (canonical sorted-key JSON; dispatch + artifact forms, DC-12), validation (tiers per §4.4) + documented reason codes, resolution (per §4.5), storage, `proposal_backlog` (C4), pass orchestration. |
| `tests/test_migrations_proposal_attempt_input.py` | new (~10 items) | 0014 schema behavior (table, idempotent re-apply, UNIQUE, FK existence + RESTRICT deletion, non-blank parametrized CHECKs, raw/sha pairing CHECK on both null and both set). |
| `tests/test_proposal_service.py` | new (~75 items) | Workstream tests (§7), incl. every AC pin listed in §8. |
| `docs/PHASE5_PLAN.md` | this file (+§12 record later) | Plan + phase record. |
| `tests/test_migrations_resolution.py` | none planned | The §12 record's baseline bump convention only applies if a migration is added — 0014 IS added, so the idempotency test's `range` assertion must move to `range(1, 15)`; that is a one-line, convention-approved bump. |
| No other file | — | No ingestion/bucketing/lifecycle/relevance changes; no config keys; no endpoints; no dependencies. |

## 6. Requirement traceability matrix (Phase 5 subset)

| Requirement | Source | Existing support | Phase 5 behavior | Tests (planned) |
|---|---|---|---|---|
| FR-PRP-01 | SRS §3.4 | 0005 proposal/attempts; relevance rows | discovery predicate (§4.1) + pass | discovery unit tests; e2e |
| FR-PRP-02 | §3.4 | flags, vertical mapping | volunteers/unknown/unmapped never attempt (AC-10) | eligibility tests |
| FR-PRP-03 | §3.4 | 0005 FKs + context links | candidate set; context = bucket members | request-build tests |
| FR-PRP-04/05 | §3.4 | 0005 multi-proposal shape | zero/one/many; per-item independence | AC-02/AC-03 pins |
| FR-PRP-06/08/09 | §3.4 | 0005 CHECK vocabulary | token validation | validation tests |
| FR-PRP-07 | §3.4 | 0005 (+attributes/verticals) | pass-through storage | storage tests |
| FR-PRP-10 | §3.4/AC-11 | 0012 assignee/mention tables | resolution execution, persisted states | resolution matrix tests |
| FR-PRP-11/DR-04 | §3.4/§4.2 | NULL-able columns | missing = missing; no invention | missing-field tests |
| FR-PRP-12/NFR-AUD-01/02 | §3.4/§6.2 | provenance columns; 0013 pattern | provenance + 0014 artifact | artifact tests (F3-analog) |
| FR-PRP-13 | §3.4 | `structurally_valid` | three-tier validation | tier tests |
| FR-PRP-14/NFR-REL-01/03/04 | §3.4/§6.1 | attempts; transaction() | idempotent retry; recovery; crash-safe | interruption/retry tests |
| FR-PRP-15/BR-03/BR-09 | §3.4/§8 | reactions untouched | no acceptance inference (nothing written) | AC-09-adjacent pin |
| FR-PRP-16 | §3.4 | — | zero task/announcement writes | negative tests |
| NFR-REL-07 | §6.1 | relevance fallback pattern | fallback generator honored | fallback tests |
| NFR-REL-06/FR-UI-05 (C4) | §6.1/§3.9 | attempt tables + `orchestration.pipeline_backlog` convention | read-only `proposal_backlog` sibling | backlog tests |
| NFR-PERF-02 (C5) | §6.4 | 0011 table | `PROPOSAL_LATENCY` + `PROPOSAL_STAGE_DELAY` rows | latency/delay tests |
| EIR-03/NFR-SEC-04/NFR-AUD-02 (C1/C2) | §4.3/§6.5/§6.2 | 0013 pattern + `message_mention` | opaque handles, 2-form serialization, raw retention | C-correction tests |
| NFR-SEC-04/AC-21 | §6.5 | mask_pii | masking-before-call on every request | masking tests |
| AI-01/02/03 | §7 | — | separate stage; advisory; edits later via revisions | architecture pins |
| NFR-MNT-01 | §6.6 | config pattern | version constants documented (DC-8) | constant tests |
| AC-02/03/10/11/21 | §10.2 | — | as above | explicit scenario tests |
| FR-PRP-17, FR-REV-*, FR-TSK-*, FR-NOT-* | §3.5–3.8 | storage ready | **out of Phase 5**; hand-offs documented (§7) | non-goals pins (no rows written) |

Deferred-to-later (not gaps): review execution, notifications, tasks,
authorization enforcement, UI, AI-05/06 evaluation (owner corpus — G4),
NFR-PERF-03 production load benchmark (live data — G5), SRS checks 7–8
(waived by prior owner decision; untouched).

## 7. Downstream hand-offs delivered (no implementation)

- Review phase (Phase 6): reads `proposal` rows with `review_state='PENDING'`
  and `structurally_valid=1`; reviewer is derivable from the primary
  message's `sender_person_id`; provenance + `proposal_assignee` states + the
  0014/0013 artifacts give the review screen its FR-REV-02 view and NFR-SEC-03(b)
  bucket context; revisions (0005) are the edit appendix. Accept-time checks
  (FR-REV-05/06, resolution confirmation) unify against these stored states.
- Notification phase: FR-PRP-17 fires from exactly the stored
  proposal-ready state; `notification.dedupe_key` composition contract noted
  (`"PROPOSAL_READY:{proposal_id}"`) but **never written by Phase 5**.
- Task phase: `proposal.id` is the accepted FK target (0006/0009 UNIQUEs
  intact); Phase 5 verifies it writes none.

## 8. Test strategy and acceptance criteria (whole phase, tests-first)

- **Unit** (new module): request build (candidates, context, order,
  masking-before-call incl. AC-21 shapes), contract freeze, serializer
  canonicalization + hash, validation tiers (envelope/item/content;
  AC-02/03/04 shapes; OPEN+assignees/`NONE`+assignees rejects; foreign
  primary ids; people-number encodings; attribute key rules; vertical-name
  resolution incl. drop-on-unresolvable), resolution matrix (mention exact
  hit/miss; name unique/multiple/none; inactive members excluded; AC-11
  full), storage transaction (atomicity, provenance copy, context rows,
  assignee rows), zero-proposals success, mixed valid/invalid independence,
  all-invalid ≠ zero-proposals, latency on success, artifact-iff-attempt
  (F3 analog: both outcomes, primary+fallback, refused/superseded none,
  interrupt rollback, ruleset-change stability, no-raw-PII).
- **Correction tests (C1–C6):** C1 — handle assignment deterministic,
  map round-trip, unknown-handle Tier-2, no real snowflake in the
  dispatch, artifact stores the map, sender IDs absent; C2 — raw
  retention on success/partial/all-invalid/adapter-exception, interrupt
  rollback, no raw PII in stored text, hash matches the stored text,
  rejected reason codes; C3 — role/membership eligibility matrix
  (AH/Head/Volunteer/unknown/inactive/multi-vertical; G1b flag on and
  off; AC-10 no attempt and no generator call); C4 — backlog counts,
  oldest age, exhausted ids, write-freedom; C5 — delay row present,
  monotonic anchor, skip-on-unparseable; C6 — DC-3/DC-6 pins.
- **Pass**: discovery (eligible-only, unmapped exclusion, PROCESSED-only,
  no re-pick after success), budget/refusal/EXHAUSTED honesty, overrides
  (per-bucket scoping, loud key/value validation, exhaustion honesty,
  repeated-recovery idempotency), first-writer-wins concurrency guard,
  fallback after primary failure only (both attempts recorded), deterministic
  order, repeat-pass no-op, FR-PRP-16 negative (no task/announcement/
  notification/artifact-orphan rows), append-only history.
- **Migration**: 0014 schema suite + `test_migrations_resolution.py`
  `range` bump to 15 (one line, established convention).
- **Integration/e2e**: scripted end-to-end
  ingest → bucketing → relevance pass → proposal pass on a temp DB (tests +
  a Temp-dir probe rerun after any source change: probe asserts stored
  proposals, artifacts, latency rows, zero notifications/tasks,
  `integrity_check` ok, `foreign_key_check` empty, self-cleaning).
- **Acceptance criteria (phase)**: all §6 rows with planned tests go green;
  AC-02/03/10/11/21 pins + AC-12 proposal half pass; NFR-AUD-02 artifact
  guarantees hold; battery ≥ previous total (`755` + 122 = 877) with 0/0/0;
  ruff/mypy/`python -m backend --version` = `0.0.1`; `git diff --check`
  clean; FK probe green.

## 9. Workstreams (executed inside the one approved batch; each tests-first)

- **WS-1 `0014` migration** — files: 0014 + its test file +
  resolution-range bump. Reqs: NFR-AUD-02 (+C1/C2 columns), DR-01/02/07.
  Risk: none (purely additive, mirrors 0013).
- **WS-2 contract module** — `backend/proposals.py` types/constants/
  serializer/hash/protocol + ProposalPolicy + `load_proposal_policy`
  (G1/G1b); tests for shape, freeze, canonicality, masking, C1 handles.
  Reqs: EIR-03, NFR-SEC-04, FR-PRP-03/12. Risk: contract drift vs
  relevance — mitigated by field-mirror tests.
- **WS-3 validation + resolution** — tiers, per-item independence, AC-11
  matrix, C1/C2/C6 pins, eligible-policy matrix (C3). Reqs:
  FR-PRP-02/05/08/10/13, BR-03. Risk: tier boundaries — pinned by
  decision DC-3/DC-4/DC-6 tests (vetoable).
- **WS-4 persistence + provenance + artifact** — attempt writing, guard,
  artifact (dispatch/artifact forms, raw retention, rejected codes),
  latency (both metrics, C5); interrupt/rollback tests. Reqs:
  FR-PRP-12/14, NFR-REL-01/02/04, NFR-PERF-02.
- **WS-5 pass orchestration + recovery + backlog** — discovery, budget,
  overrides, fallback, PW-error semantics, `proposal_backlog` (C4).
  Reqs: FR-PRP-01/04, NFR-REL-03/06/07, FR-UI-05.
- **WS-6 e2e + probe + docs record** — integration test, Temp probe leg,
  PHASE5_PLAN §12 record, final battery + checks.

Commands per workstream (and the phase battery):
`.venv\Scripts\python.exe -m pytest <files> -q`, full `pytest -q`,
`ruff check .`, `mypy`, `python -m backend --version`, `git status --short`
(no git mutations without owner authorization), probe rerun.

## 10. Security, privacy, provenance

- Masking before every adapter call (NFR-SEC-04; AC-21), originals untouched
  (DR-03); masked input reconstruction materialized in `0014` (NFR-AUD-02);
  identity fields transit per the accepted relevance boundary (documented
  in the 0013/0014 headers).
- Treat adapter output as untrusted data and never as instructions or
  authority (AI-02); no state write outside `transaction()`; errors
  redacted + capped exactly as relevance does (`mask_pii` + 500-char bound).
- No credentials, no secret reads, no endpoints; `Config` secrets matrix is
  untouched. No logging of config values. Synthetic roster/message fixtures
  only (no real personal data — minimal synthetic fixtures like the
  relevance suite).
- Provenance (generated_at/model_id/model_version/prompt_version) never
  altered later; revisions/edit history belong to the review phase (AI-03).

## 11. Owner decision gates (genuine authority or missing external inputs)

- **G1 — Eligible-role vocabulary — RESOLVED by owner (2026-10-22):**
  stored tokens `AH` and `Head`, matched trimmed and case-insensitively,
  active flag required, overridable via config key `PROPOSAL_ELIGIBLE_ROLES`
  (comma-separated tokens; default `AH,Head`); G1b
  `PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP` (default **off**) gates an
  optional source-vertical membership requirement on top — tests cover both
  settings. Only the owner may flip the default.
- **G2 — Part B PB-1–PB-7:** specified in §15 of this document (owner-supplied
  statement of work, 2026-10-22). Phase 5 **excludes implementing** them;
  each becomes implementable only after the owner writes `APPROVED PB-<n>`
  in `docs/PHASE5_PLAN.md` (the owner edits the SRS separately — this plan
  never touches `docs/SRS.md`).
- **G3 — Deployed proposal model + prompt/config version registry**
  (external, §13.8 checklist). The in-repo protocol + stub adapters suffice
  for all tests; production calls wait.
- **G4 — AI-05 proposal evaluation corpus + AI-06 comparison** (owner
  corpus/model access). Blocks evaluation verification only, not code.
- **G5 — Latency targets are initial values to be confirmed:** the SRS
  targets exist verbatim (message→notification ≤ 30 s P95; relevance per
  bucket ≤ 1.5 s; **proposal generation per bucket ≤ 8 s**; availability
  ≥ 99% — §6.4; recall ≥ 95%, precision ≥ 80%, type/mode ≥ 95%, assignee/
  task-type ≥ 90%, acceptance ≥ 80% — §7). Running code never asserts them
  (NFR-PERF-01: confirmation is a benchmark on live data); Phase 5 ships the
  *recording* (NFR-PERF-02: `PROPOSAL_LATENCY`, `PROPOSAL_STAGE_DELAY`) so
  the benchmark has data.
- **G6 — Worker scheduling authority** (progress-scheduling authority, §7):
  Phase 5 delivers `run_proposal_pass` as a callable service; worker-loop
  wiring/remains-scheduling is still an open owner decision (same posture
  as relevance).

## 12. Decisions made within approved requirements (routine; vetoable)

- **DC-1** Proposal stage never transitions bucket state (buckets are
  PROCESSED; no new lifecycle edges; proposal work discovered from attempts
  + decisions).
- **DC-2** Validation = the SRS's structural set (§4.4 tiers); mode/assignee
  inconsistency from FR-PRP-08 ("shall never pick a person for an OPEN
  task") treated as an item-invalid contract violation; `ANNOUNCEMENT` items
  still carry a well-formed `task_type` (pass-through, meaningless until
  review). Tolerant alternatives exist if the owner prefers.
- **DC-3** A non-empty all-invalid response is Tier-1 failed (never another
  stored as a legitimate "zero proposals"); a legitimately empty response
  is a valid success (AC-03).
- **DC-4** *(refined by owner correction C2, 2026-10-22)* Tier-2
  violations are **never proposal rows** (the service stores only
  structurally valid items, always `structurally_valid = 1`); each
  rejected item is recorded in the 0014 artifact's `rejected_items`
  column with a documented reason code, so FR-PRP-13's evidence survives
  audit without poisoning the proposal table. They are never reviewable
  or notifiable.
- **DC-5** `proposal_assignees_text` = tokens joined ", "; one assignee row
  per token verbatim (0005 + 0012 coexist by design).
- **DC-6** Resolution executed at storage time (visible provenance for the
  review screen; FR-REV-06 gate still authoritative at accept time, later
  phase). Name matching = trimmed case-insensitive equality (Hinglish chat
  casing); mention route is exact string equality against `message_mention`
  evidence + `person.discord_user_id`.
- **DC-7** Vertical names resolved by exact case-insensitive `vertical.name`;
  unresolvable responsible → NULL, unresolvable affected names dropped
  (DR-04; reviewer edits).
- **DC-8** Version identity: `PROPOSAL_PROMPT_VERSION = "proposal-prompt-v1"`
  and the prompt text as module constants with documented bump discipline
  (mirrors the approved relevance pattern; NFR-MNT-01 config-overrides can
  ride a later hardening, no invented config keys now).
- **DC-9** Proposal-stage recovery = authorized re-pass with budget
  override (no bucket state to requeue; exhausted set is inventory-visible
  without state change); `FAILED_PERMANENT` still never written.
- **DC-10** FR-PRP-11 placeholders: storage keeps missing as missing (NULL),
  rendering is the review screen's job (FR-UI-06) — placeholder tokens are
  *not* generated (no invented content at rest).

Decisions from the owner's 2026-10-22 corrections (all tested; each
individually vetoable without disturbing the rest):

- **DC-11 (C1)** Opaque mention handles for the proposal stage:
  handle assignment `@m1…` in first-appearance order over bucket members
  by ascending message id, per-message mentions in `message_mention`
  position order — deterministic; handles, not real IDs, ride the
  request; the handle → Discord user ID map rides the stored artifact
  form only (never the dispatch); unknown handle-shaped tokens are
  Tier-2 `UNKNOWN_HANDLE`, never guesses. The relevance/0013 retrofit is
  **plan-only, owner-approval item** (relevance still sends raw IDs;
  untouched this phase).
- **DC-12 (C1)** Two canonical serializations of one request: the
  dispatch form (no handle map — what the model sees,
  `request_sha256` covers this) and the artifact form (dispatch JSON +
  `mention_handle_map` member — what audit stores). The generator receives
  the model-facing request only (`ProposalGenerationRequest`); the handle map
  lives in backend `ProposalRequestContext`.
- **DC-13 (C2)** `raw_response` mask + size cap: the cap is 65536 bytes
  (`MAX_RAW_RESPONSE_BYTES`) with `raw_response_truncated` flag; the
  500-character figure applies to attempt error text only (`MAX_ATTEMPT_ERROR_LENGTH`)
  and is superseded for `raw_response`. Capped text is marked with a `…` suffix;
  `response_sha256` is always over the exact stored (masked, capped) text; a failed
  attempt still stores its response when the adapter returned one.
- **DC-14 (C5)** `PROPOSAL_STAGE_DELAY` anchored at the latest SUCCEEDED
  relevance attempt's `finished_at` (not at bucket close — close wall
  time is not persisted by 0003); honest skip when no parseable anchor;
  no comparison to any SRS target in code.
- **DC-15** MENTION route requires no `active` flag (exact-ID resolution
  as specified); mapped ID without a roster person → FLAGGED.
- **DC-16** Distinguishability discipline (DR-04): NULL = missing;
  blank/trimmed-empty strings are never coerced to NULL — they are
  `FIELD_VALUE_INVALID` (Tier-2) for storable fields.
- **DC-17** Malformed proposal response fallback: when canonical JSON
  serialization fails (e.g. non-envelope or malformed items),
  `_raw_response_text` degrades to `repr(response)`, guaranteed to be
  PII-masked and capped at 65536 bytes.
- **DC-18** Discord markup rewriting and fresh handles for unmatched users:
  role mentions `<@&ID>` rewritten to `@role`, channel mentions `<#ID>` rewritten
  to `#channel`, custom emoji `<:name:ID>` and animated `<a:name:ID>` rewritten to
  `:name:`. Unmatched user mentions `<@ID>` receive fresh opaque handles (`@m1`,
  `@m2`, ...) allocated deterministically so Discord snowflakes never reach the generator.
- **DC-19** `reply_reference` resolved to internal id or null: resolved to internal
  `source_message.id` if referenced message is within the same bucket; external or
  unresolved reply references resolve to `None` (`null` in JSON), preventing external
  snowflakes from reaching the generator.
- **DC-20** 64 KB cap, truncation flag, hash over the full masked text:
  `raw_response` cap of 64 KB (65,536 bytes; `MAX_RAW_RESPONSE_BYTES`),
  `raw_response_truncated` column in `proposal_attempt_input`, with `response_sha256`
  computed over the full masked text before truncation to preserve audit integrity.
  The 500-character figure applies strictly to attempt error text (`MAX_ATTEMPT_ERROR_LENGTH`).
- **C6 pins (owner-judged, vetoable):** DC-3 (all-invalid ⇒ failed
  attempt) and DC-6 (exact trimmed+casefold name equality; name matching
  restricted to the source vertical — cross-vertical names FLAGGED) are
  each pinned by dedicated tests.

## 13. Phase-end review and correction pass

- An independent read-only audit subagent over `backend/proposals.py`,
  `0014`, and both new test files: six checks — artifact iff committed
  attempt (all outcome/stage/refusal/supersede/interrupt shapes); fidelity
  of the masked artifact; validation-tier correctness w.r.t. FR-PRP-05/13;
  resolution matrix legality (no invented persons/verticals; DR-04);
  append-only/idempotency/no-duplicate guarantees; no writes outside scope
  (tasks, announcements, notifications, bucket state, relevance rows).
- One consolidated correction pass on findings (tests-first for any
  behavior change); full battery rerun; docs record §14 appended with real
  numbers; then owner review + commit authorization.

## 14. Phase record

**Completed & Stabilized (2026-10-23):**
- **Files delivered / modified:**
  - `backend/db/migrations/0014_proposal_attempt_input.sql`: additive migration for proposal attempt input retention (`proposal_attempt_input` table with SHA-256 hashes, masked `request_artifact_json`, masked/capped `raw_response`, `raw_response_truncated`, `rejected_items`, `captured_at`).
  - `backend/proposals.py`: proposal generation stage types, policy configuration (G1/G1b), request serialization (dispatch vs artifact), validation tiers (Tiers 1, 2, 3), reason codes, resolution (MENTION and source-vertical NAME_MATCH), transactional attempt persistence, `proposal_backlog` (C4), and pass orchestration.
  - `tests/test_migrations_proposal_attempt_input.py`: 14 migration tests covering schema, foreign keys, triggers, and idempotency.
  - `tests/test_proposal_service.py`: 108 service tests covering all functional requirements, edge cases, error conditions, C1–C6 corrections, and pinned markup rewrites.
  - `tests/test_migrations_resolution.py`: updated expected total migration count to 14 (`range(1, 15)`).
  - `.gitignore`: ignored `.tmp_*.xml`.
- **Observed verification results (real runs, no skips, no failures):**
  - Full repo test suite: **877 passed / 0 failed / 0 skipped** in ~80s (baseline: 755; 122 Phase 5 additions).
  - `tests/test_proposal_service.py`: 108 passed.
  - `tests/test_migrations_proposal_attempt_input.py`: 14 passed.
  - `tests/test_migrations_resolution.py`: 19 passed.
  - `ruff check .`: **All checks passed!**
  - `mypy`: **Success: no issues found in 13 source files**.
  - `python -m backend --version`: `0.0.1`.
  - Fresh-database migration: 14 versions applied cleanly (`[1..14]`), rerun applied 0 (idempotent), `PRAGMA foreign_key_check` clean (`[]`), `PRAGMA integrity_check` clean (`['ok']`).
  - `git diff --check`: clean (no whitespace or newline anomalies).

**Decisions recorded & refined (DC-11 onward):**
- **DC-11 (C1)** Opaque mention handles for the proposal stage: handle assignment `@m1…` in first-appearance order over bucket members by ascending message id, per-message mentions in `message_mention` position order; handles ride the request; handle map rides the artifact form only.
- **DC-12 (C1)** The generator receives the model-facing request only (`ProposalGenerationRequest`); the handle map lives in `ProposalRequestContext`. Two canonical serializations: dispatch form (no handle map — model input) and artifact form (dispatch JSON + `mention_handle_map` member — audit storage).
- **DC-13 (C2)** `raw_response` cap is 65536 bytes (`MAX_RAW_RESPONSE_BYTES`) with `raw_response_truncated` flag; the 500-character figure applies to attempt error text only (`MAX_ATTEMPT_ERROR_LENGTH`) and is superseded for `raw_response`. Capped text suffixed with `...`; `response_sha256` over full masked text.
- **DC-14 (C5)** `PROPOSAL_STAGE_DELAY` anchored at latest SUCCEEDED relevance attempt's `finished_at` (`ORDER BY id DESC LIMIT 1`); honest skip when no parseable anchor exists.
- **DC-15** MENTION route requires no `active` flag; mapped ID without a roster person flags as `FLAGGED`.
- **DC-16** Distinguishability discipline (DR-04): NULL = missing; blank/trimmed-empty string = `FIELD_VALUE_INVALID` (Tier-2).
- **DC-17** Malformed proposal response fallback: when canonical JSON serialization fails (e.g. non-envelope or malformed items), `_raw_response_text` degrades to `repr(response)`, guaranteed to be PII-masked and capped at 65536 bytes.
- **DC-18** Markup rewriting and fresh handles for unmatched users: `<@&ID>` -> `@role`, `<#ID>` -> `#channel`, `<:name:ID>` / `<a:name:ID>` -> `:name:`; unmatched `<@ID>` get fresh opaque handles deterministically so snowflakes never reach the generator.
- **DC-19** `reply_reference` resolved to internal id (if in-bucket) or null (if external/unresolved).
- **DC-20** 64 KB cap (65,536 bytes), truncation flag (`raw_response_truncated`), and hash over the full masked text before truncation.
- **Test-expectation triage & corrections:**
  - `test_partial_invalid_keeps_valid_sibling_independent`: asserted `MODE_INVALID` for `_draft(..., assignment_mode="NONE")` on TASK (FR-PRP-08, 0005 check).
  - `test_open_task_count_encodings_store`: passed bucket candidate message ID `con_mid`.
  - `test_artifact_iff_committed_attempt`: `message_count=1` to match single-unpacking.
  - `test_cross_vertical_name_is_flagged_not_silently_resolved`: set `mention_member_active=0` to pin DC-6 (active cross-vertical colleague stays invisible, no active member in source vertical -> FLAGGED).
  - `test_deterministic_order_multiple_buckets` and `test_pass_error_carries_durable_prefix`: candidate message IDs `mids` for drafts.
  - `test_stage_scoped_budget_ignores_other_stages`: asserted total relevance attempts count of 6 (1 scenario + 5 in loop).
  - `test_writer_never_writes_terminal_outcomes`: passed fallback generator and `max_attempts_per_bucket=3` to test fallback attempt and total 3 attempts (1 seeded + 2 in pass).
  - `_bucket`: preserved `end_event=None` as NULL via `_DEFAULT_END_EVENT` sentinel.
  - `_backlog_scenario`: seeded `done_bucket` directly with `_proposal_attempt_seed(conn, done_bucket, "SUCCEEDED")`.

## 15. Part B — PB-1–PB-7 engineering plans (plans ONLY; nothing implemented)

Per the owner (2026-10-22 / 2026-10-23): Part B decisions are designated **PB-1 through PB-7** (not to be confused with routine D-1..D-3 decisions in `PHASE2_PLAN.md`). Each item below becomes implementable only when the owner writes `APPROVED PB-<n>` in this file. `docs/SRS.md` stays untouched (the owner edits the SRS where amendments are needed).

### PB-1 — Bucket clock, settling, late semantics (FR-BKT-04/05/06/08)

- **Problem (reproduced on current code, 2026-10-22, W=15, G=5, M=30, C=2; `assign_messages` + `mark_stream_progress(event_progress = max event time so far)` after each arrival):**
  - arrivals 0, 12, 16 → **one bucket holding `{0, 12, 16}`**;
  - arrivals 0, 16, 12 → **three buckets `{0}`, `{16}`, `{12}`**.
  FR-BKT-06 requires **identical bucket composition within grace**; composition currently depends on call order. Additionally, nothing outside tests calls `mark_stream_progress`, so a live run never closes a bucket (progress advances, buckets stay OPEN).
- **Options:** (a) keep call-order semantics, document; (b) settle + deterministic ordering as below.
- **Recommendation (b):** per-group **clock** = live: receipt time of the newest received message; replay: replay/stored time. A message is **settled** when `clock ≥ t + C`; `assign_messages` considers only settled messages sorted by `(event time, source_message_id)`; a bucket **closes when `clock ≥ e + C`**; a message is **late** only if the bucket it would have joined is already closed (FR-BKT-08); keep persisted per-bucket parameters and inclusive closure (W/G/M/C + C grace on the threshold). Progress marking stays explicit.
- **Files/tests:** `backend/bucketing.py`, `backend/db/migrations/0003*` (parameters persistence), load/late-timing behavior tests; FR-BKT-04/05/06/08 pins.
- **Risks:** changes Phase 3 behavior contract; existing tests pin some current semantics; needs an SRS amendment decision by the owner.

### PB-2 — Missing, malformed or naive event timestamps (FR-ING-03, FR-BKT-01, NFR-REL-05)

- **Problem:** messages without a valid/parseable event timestamp are currently skipped — never bucketed, never classified; naive timestamps have no recorded error path; the importer accepts arbitrary zone text.
- **Options:** (a) skip silently + count in backlog; (b) bucket on receipt time with a stored flag; (c) hard-fail ingest.
- **Recommendation (b):** bucket time = valid event time, else receipt time; a stored `time_source` marker/flag on the message (`EVENT` / `RECEIPT`); the stored event time column is never overwritten; the importer takes an explicit time zone argument; naive *live* timestamps are a recorded error (never invented zone). Backlog (NFR-REL-06) counts receipt-anchored messages as a distinct, visible class.
- **Files/tests:** `backend/ingest.py`, `backend/bucketing.py`, migration for the marker column; malformed/naive/mixed timestamps tests; DR-04 verbatim-value pins.
- **Risks:** schema addition; ambiguity if receipt time is also missing (still fails loud); reconciliation rules touch NFR-REL-05 wording.

### PB-3 — Task table rebuild (FR-TSK-*)

- **Problem:** `people_needed` must be explicit; `UNIQUE(task_id, person_id)` blocks re-adding a person after a denial; assignee cancellation has no dedicated representation; INTERNAL tasks with NULL responsible vertical are only implicitly allowed.
- **Options:** (a) amend CHECKs/UNIQUEs in place; (b) new-table rebuild + copy migration.
- **Recommendation (b):** additive rebuild (SQLite cannot alter constraints) — explicit non-NULL `people_needed`; assignee rows keyed with a state column and `UNIQUE(task_id, person_id, state)` so a denied person can be re-added; cancelled assignees keep a row with canceller provenance; INTERNAL keeps a NULL responsible vertical explicitly + CHECK interplay.
- **Files/tests:** new migration + `backend/tasks.py`; UNIQUE/re-add matrix tests; FR-TSK-05/06/09/11/13 pins.
- **Risks:** copy migration must preserve task/entity/announcement FK history (append-only); audit tests must keep provenance intact.

### PB-4 — SQLite vs PostgreSQL (figures 1 and 3; NFR-CON-01/02)

- **Problem:** figures 1/3 name PostgreSQL; the repo standardizes on SQLite (single-writer, WAL); multi-process workers + 60/120 msg/min load make concurrency behavior the real question.
- **Options:** (a) stay SQLite + documented single-writer discipline; (b) migrate to PostgreSQL.
- **Recommendation:** owner decision with a benchmark; short term, document SQLite as authoritative per NFR-CON-02 or mint an SRS amendment; the centralized `backend/db/connection.py` keeps both open.
- **Files/tests:** load probe + connection-pattern tests under concurrency if kept.
- **Risks:** figure text vs code mismatch remains until the SRS is amended; concurrency issues (locked DB) surface only under real load.

### PB-5 — Roster integrity (role vocabulary, per-vertical roles, `reports_to`, required fields)

- **Problem:** `person.role` is free text; §1.4 eligibility needs a controlled vocabulary; vertical-scoped roles rely on `reports_to` chains with no enforcement; phone/roll-number requiredness is unenforced.
- **Options:** (a) enforce at write time (vocabulary CHECK + FK); (b) validation helpers + import-time enforcement only.
- **Recommendation:** G1 (`AH`/`Head` eligibility for proposals) is the Phase 5 slice of PB-5; full integrity = new migration — role vocabulary table or CHECK, per-vertical role rows, self-referential `reports_to` FK, required-field CHECKs — plus import-side loud errors.
- **Files/tests:** migration + roster modules; vocabulary matrix tests.
- **Risks:** actual roster data may violate current assumptions (owner data unknown); a state-vocabulary decision follows the standard approval process.

### PB-6 — Announcement audience structure (FR-PRP-06, FR-NOT-05)

- **Problem:** audiences are free text on the announcement row; there is no stable per-recipient audience record for notification fan-out (FR-ANN-*, FR-NOT-*).
- **Options:** (a) audience table (announcement_id, person_id, source); (b) keep free text + resolve at notification time.
- **Recommendation (a):** additive audience rows written at announcement acceptance (reviewer-confirmed), source-tagged (vocabulary owner-decided); read-only until then.
- **Files/tests:** migration + announcement module; audience resolution tests.
- **Risks:** audience vocabulary is undefined in the SRS — needs an owner decision before any code.

### PB-7 — Predecessor bucket chain limits (FR-BKT-08)

- **Problem:** `predecessor_bucket_id` is populated by `backend/bucketing.py` for every new bucket formed in a group, chaining all buckets sequentially, whereas the SRS specifies predecessor linkage only when a late message creates a new bucket post-closure.
- **Options:** (a) justify and retain full sequential chaining as audit lineage; (b) limit `predecessor_bucket_id` population strictly to late-arrival buckets per FR-BKT-08.
- **Recommendation:** owner decision on whether to retain full bucket chaining or restrict to late arrivals; document the chosen invariant.
- **Files/tests:** `backend/bucketing.py`; bucket chain audit tests.
- **Risks:** changing to late-only affects tests asserting current chain behavior.

