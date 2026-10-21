# ASEC Phase 2 Plan — Persistent Data Models and Integrity

Status: active roadmap. Derived from `docs/SRS.md` (requirements authority), `AGENTS.md`
(workflow and architecture rules), and the actual repository state. This document is a
plan, not an implementation claim: schema existence never means behavior is implemented.

---

## 1. Purpose and boundary

Phase 2 designs, migrates, and tests the **persistent data layer** of the ASEC MVP: every
entity listed in SRS §4.1 (Entities) with its integrity rules (§4.2, DR-01..DR-07),
implemented as versioned SQLite migrations plus their storage-level tests.

Phase 2 **delivers**: migration infrastructure, roster/organization tables, source
message/reaction storage, bucket storage, processing-attempt records, relevance-decision
records, proposals/revisions, review-state records, tasks/assignees/state
dimensions/history, cross-vertical approvals, notification records, announcement records,
and the NFR-PERF-02 latency measurement records (Slice 2.11).

Phase 2 **does not deliver** (original scope boundary): Discord ingestion, AI services or
calls, relevance/proposal *model* runs, review/task/notification *API or UI*, authorization
engine, worker orchestration, or the frontend. Phase 3+ owns ingestion/processing pipeline
executable behavior, AI orchestration (SRS §7), authorization enforcement (FR-AUT-*), and
app surfaces (§3.10, FR-UI-*).

Scope note (working-tree reality, 2026-10-19): bucket formation — originally algorithmic
later-phase work (FR-BKT-03..08, §10.1) — was implemented ahead of Phase 3 by the approved
Revision 4 slice (`backend/bucketing.py`, migration `0010_source_group_progress.sql`,
`tests/test_bucketing_service.py`; see §8). That slice and Slice 2.11 sit alongside the
Phase 2 data layer in the working tree; the Phase 2 boundary itself is unchanged.

## 2. Current status (data layer complete at `f61aad7`; normalized-event ingestion implemented, verified and uncommitted — §10–§11; AI orchestration, services, authorization, API, notification delivery and app still not started)

- HEAD: `4e4aae2cb91b9d1f8293a732e6fc31281f9cdfff` — "Complete Phase 2: data-layer
  slices 2.5-2.11 and bucket formation". All migrations `0001`–`0011` (roster,
  source messages, buckets, processing attempts, proposals, tasks, approvals,
  notifications, announcements, source-group progress, latency measurements)
  are committed at this HEAD; nothing in versions 4–11 is uncommitted.
  `backend/bucketing.py` (bucket formation, approved Revision 4 semantics,
  review fix M1 applied: closure uses each bucket's persisted `parameters_used`;
  missing/malformed records fail loudly) and the latency/bucketing test suites
  are part of the same commit.
  In the working tree, `docs/SRS.md` carries the owner's replacement with the
  faithful SRS v2.0 conversion, and `session-ses_eb9c.md` is untracked and out
  of scope (never read, never staged). The same working tree carries the
  owner-directed Slice A4 correction, uncommitted: migration
  `0012_resolution_and_mentions.sql` with its tests
  (`tests/test_migrations_resolution.py`, `tests/test_migrations_fk_check.py`;
  see §3 and §4).
- Full suite green at last verified run (read-only pre-commit audit,
  2026-10-20): **566 tests passed**. The pre-correction state was last verified
  at 486 tests (historical; prior committed baseline: 194). The committed 0003
  enforces FR-BKT-02 (same-group membership) via two
  composite foreign keys plus parent-coverage unique indexes
  `ux_source_message_id_group` and `ux_bucket_id_group` — an approved Slice 2.4.1
  correction, applied pre-commit.
- **Update 2026-10-21 (current state):** HEAD is `f61aad7e1e4f56a6fff0c8d7dbea66492e8085e0`
  — "Complete Phase 2 data-layer implementation" (contains 0012 and the owner's
  SRS replacement). Uncommitted in the tree: normalized-event ingestion
  (`backend/ingest.py`, `tests/test_ingestion_service.py`) including the review
  corrections and the FR-ING-08 replay entry point (§10, §11), plus these plan
  updates themselves. Last verified full suite: **617 tests passed** (51 in the
  ingestion module) with the F-01–F-04 review corrections applied (2026-10-21); ruff, mypy, `git diff --check` and the FK probe all clean.
  Nothing is staged or committed (awaiting owner authorization, §9).

## 3. Slice roadmap

Progress markers are factual. Slices 2.5–2.10 were implemented in directed owner rounds; Slices 2.11 and the
bucket-formation slice were implemented under the Phase 2 completion authorization
(2026-10-19) — each tests-first. All of that Phase-B work is committed at HEAD
`4e4aae2`. Adding a database CHECK that enumerates a state/outcome vocabulary
(for example the §4.1 Processing attempt outcomes, FR-REV-01 review states, FR-XV-04
decisions) follows the established **D1 decision process**: the vocabulary must be
SRS-verbatim and explicitly approved by the project owner at that slice's planning step.

### Status legend
- [x] committed · [x] implemented (storage/schema + tests, uncommitted) · [ ] planned

### Slice 2.1 — Database connection and migration infrastructure [x committed, `ef15418`]
- Purpose/scope: SQLite connection factory (`connect`, WAL, `PRAGMA foreign_keys = ON`
  enforced), explicit `transaction()` context manager, versioned SQL migration runner
  (`apply_migrations`, tracking table `schema_migrations`, checksum verification,
  per-migration transaction with tracking row on success).
- SRS: §9 constraints (SQLite, enforced references, DR-01), NFR-REL-04, NFR-CON-01.
- Tests: idempotency, ordering, failure isolation, checksums.
- Exclusions: any application schema (arrived in 2.2+).

### Slice 2.2 — Organizational entities and memberships [x committed, `702cb5c`]
- Purpose/scope: `0001_core_roster.sql` — `vertical`, `source_group` (guild/channel →
  vertical mapping, nullable `vertical_id`), `person` (identifier-discipline columns,
  `role` free text, `active` flag), `person_vertical` (membership join).
- SRS: FR-ROS-01/02, §2.3, FR-ING-09, AC-07 (roster precondition), DR-01/DR-02/DR-04.
- Known deferred decisions: `role` vocabulary, identifier mandatory-ness, `reports_to`
  free-text vs FK (§2.3 role words only: Admin/Head/AH/Volunteer as display tokens).

### Slice 2.3 — Source messages and reactions [x committed, `6bac7ba`, `8c9fafa` masking]
- Purpose/scope: `0002_source_messages.sql` — `source_message` (normalized record per
  FR-ING-02: source, source_message_id, group, sender person link + raw Discord sender id,
  event/received timestamps, content, reply_reference, raw_metadata JSON, four persisted
  flags LATE/UNMAPPED/UNKNOWN_SENDER/UNCLASSIFIED with initial 0), `message_reaction`
  (evidence only). `UNIQUE (source, source_message_id)` idempotency key.
- SRS: FR-ING-02..07, FR-ING-09, FR-ING-10, AC-21, DR-01..05, masking decision
  (NFR-SEC-04: mask phone/roll/email before AI calls — implemented in backend masking
  utils; originals stored unchanged, verified by tests).
- Committed together: 8c9fafa masking, 4fe4f66 phone-masking fix, 6bac7ba schema.

### Slice 2.4 — Bucket schema and integrity [x committed, `855ebb7`]
- Purpose/scope: `0003_buckets.sql` — `bucket` (group FK NOT NULL RESTRICT, predecessor
  self-FK nullable, window-bound timestamps nullable, `parameters_used` JSON-text
  nullable, `processing_state` recording-only with the five FR-BKT-12 tokens CHECK,
  default OPEN — approved decisions D1/D2, D3), and `bucket_message`
  (`PRIMARY KEY (bucket_id, message_id)`, `UNIQUE (message_id)` = at most one bucket per
  message, FR-BKT-02 enforced by composite FKs + parent indexes — approved 2.4.1 entry).
- SRS: FR-BKT-01, FR-BKT-02, FR-BKT-08, FR-BKT-10, FR-BKT-12 (recording), §4.1 Bucket,
  DR-01/02/04, NFR-REL-04 (retry shape only).
- **Not implemented and not to be inferred**: formation/join rules (FR-BKT-03/04/05/06),
  close-time concurrency behavior (FR-BKT-07), state transitions, a message "always having"
  exactly one bucket — **FR-BKT-01 (§3.2) is the eventual guarantee that every
  message belongs to exactly one bucket**, delivered by the bucket-formation slice (now
  implemented; see §1), not by this schema; the §10.1 scenario rows (Duplicate
  delivery → one message; Message after closure → stored, new bucket, earlier
  bucket unchanged) corroborate it behaviorally.
- Open blockers: FR-BKT-04, FR-BKT-05 (blank bodies). Raised as a requirements issue for
  the project owner; the bucketing-formation slice cannot be planned until they are filled.

### Slice 2.5 — Processing attempts and relevance decisions [x] implemented (storage/schema + tests)
- Purpose/scope (implemented): `0004_processing_attempts.sql` (single migration, version 4) —
  - `processing_attempt`: stage free text (no SRS stage vocabulary), input reference =
    `bucket_id` NOT NULL (approved Slice 2.5 design choice, FR-REL-01; a future
    non-bucket-scoped stage would require a new migration — see open items; this does
    not change the current schema), model_id/model_version/config_version nullable
    (may be absent when a stage failed before the model ran; missing stored as missing,
    DR-04), started/finished times stored verbatim (no format CHECK), outcome
    (`SUCCEEDED | FAILED_RETRYABLE | FAILED_PERMANENT` — §4.1 Processing attempt row verbatim,
    owner-approved set), error text nullable and never empty. Classifier failure
    (timeout, model unavailable, malformed output, infrastructure error — FR-REL-07) is
    recorded on the attempt row's outcome/error fields only; it is not a relevance
    decision, and decision rows exist only for actual classifier outputs.
  - `relevance_decision`: composite FK `(bucket_id, message_id)` → `bucket_message` PK
    (the decision's message must be an actual member of the named bucket — FR-REL-01/
    FR-REL-05); composite FK `(attempt_id, bucket_id)` → `processing_attempt (id,
    bucket_id)` covered by the vacuous `ux_processing_attempt_id_bucket` (the attempt
    belongs to the same bucket as the decision — FR-REL-01); `UNIQUE (attempt_id,
    message_id)` (retry safety — NFR-REL-01; retry creates a **new** attempt, NFR-REL-03
    recording shape); decision restricted to `RELEVANT | IRRELEVANT` — FR-REL-02 verbatim
    (no UNCERTAIN decision state), timestamp verbatim; provenance copied per decision
    row (model_id, model_version, prompt_version — owner-approved; FR-REL-05).
  - `relevance_correction`: human override as a **separate record** type (FR-REL-06),
    referencing the original decision (FK RESTRICT) and the roster person (FK RESTRICT;
    deactivation keeps attribution — FR-AUT-07); `corrected_decision` uses the
    FR-REL-02 vocabulary verbatim; `corrected_at` stored verbatim. The correction
    *interface* is Post-MVP (FR-PM-05). Storage only: the database does **not** enforce
    append-only writes, determine which correction is effective, order corrections by
    recency, or authorize correction creation.
- Open items (recorded at `/review`; approved choices vs unresolved future behavior):
  - AU-1 (open): §4.1 "input reference" does not name its entity for every future
    stage. Approved now: bucket-only, NOT NULL. A future non-bucket-scoped stage
    would need a rebuild migration; no schema change until it exists.
  - AU-2 (resolved for storage): `stage` remains free text because the SRS does not
    define a closed stage vocabulary; naming conventions are service-side only.
  - AU-3a (open): FR-REL-06 "the corrected result is the one the system would use
    from that moment" — whether history/exports must retroactively honor corrections
    is unresolved.
  - AU-3b (open): several corrections per decision are permitted (no UNIQUE rule);
    recency-win selection is an interpretation, not schema-enforced.
  - AU-3c (open): effective-correction selection requires a consistent timestamp
    representation plus the selection rule; a future service-level slice must define
    both. Correction timestamps are stored verbatim; no format CHECK exists.
  - AU-4 (open): the correction-creation path in the MVP is undefined (the correction
    interface is Post-MVP, FR-PM-05); FR-AUT-02 does not list relevance correction
    among protected operations.
  - AU-5 (open): whether a correction equal to the original decision is meaningful is
    unspecified by the SRS; the schema permits it.
  - Q1 (accepted): a `SUCCEEDED` attempt with a non-null `error` is permitted; the SRS
    does not prohibit it and no CHECK was added.
- Dependencies: 2.3, 2.4.
- Tests (delivered, 39 functions / 41 cases; one outcome test parametrized ×3):
  idempotency/failure isolation; attempt nullability, outcome vocabulary including
  unknown/NULL/whitespace rejections, free-text stage, error domain, verbatim
  timestamps, model/config optionality; decision vocabulary and provenance
  rejections; duplicate (attempt, message) pair rejected; retry via a new attempt
  accepted; second-connection concurrency; multiple messages per attempt; composite-FK
  reject sides (message not a member; message of another bucket; attempt of another
  bucket) and accept sides; unknown message/attempt; deletion RESTRICT in every
  direction; FAILED_RETRYABLE attempt with zero decisions and untouched message
  (FR-REL-07/AC-12 shape); `PRAGMA foreign_key_check` clean on a mixed workload;
  corrections: both tokens, invalid/NULL/whitespace rejected, unknown decision/person,
  decision row unchanged after a correction is inserted (documents service-level
  immutability), multiple corrections per decision permitted.
- Acceptance: storage-level only. Service-level behavior — retry orchestration,
  classifier execution, effective-correction selection, correction authorization,
  UNCLASSIFIED flag propagation — is **not** implemented by this slice and is not
  claimed anywhere. AC-12/AC-13 are covered as recording-shape tests only.
- Exclusions: any classifier execution, masking calls in pipeline, fallback classifier
  logic (NFR-REL-07 recording fields only), relevance *decisions for real messages*,
  correction UI/workflow/authorization.

### Slice 2.6 — Proposals, revisions, provenance, review state [x committed, `4e4aae2`]
- Purpose/scope (implemented): `0005_proposals.sql` —
  - Generation-run representation: **`processing_attempt` rows reused** (0004; `stage`
    is free text per AU-2, e.g. a service-side `proposal_generation` convention) —
    bucket-scoped per FR-PRP-01. One run yields **zero or many** proposals (FR-PRP-04;
    no proposal rows is a valid outcome) and one source message may yield **several
    proposals in the same run** (AC-02: "one message with two tasks"), so proposal
    candidate multiplicity carries **no UNIQUE rule** on (attempt, message); retry
    duplication avoidance (NFR-REL-01, retried run = a **new** attempt row, NFR-REL-03)
    is a service-level discipline, not a schema guarantee.
  - `proposal`: type (`TASK | ANNOUNCEMENT` — FR-PRP-06), description (NULL =
    absent/placeholder per FR-PRP-11/DR-04), assignment mode
    (`EXPLICIT | OPEN | NONE` — FR-PRP-08), proposed assignee text (model output
    verbatim, FR-PRP-10), people needed (positive integer or `people_needed_unlimited`
    flag — UNLIMITED verbatim from FR-REV-06; mutually exclusive CHECK), task type
    (`INTERNAL | CROSS_VERTICAL` — FR-PRP-09), source vertical NOT NULL (group-derived,
    unmapped groups never propose — FR-PRP-02), suggested responsible vertical
    (nullable), primary source message FK (exactly one, real `bucket_message` member —
    FR-PRP-03), context message links (bucket-mates only, FR-PRP-03/12), generic
    attributes (key/value rows — FR-PRP-07), structural validity stored as a **boolean
    column** (`structurally_valid 0|1`; FR-PRP-13 — no invented status-token vocabulary,
    per the D1 rule), provenance copied per candidate (run attempt, bucket, message,
    model, prompt version, generated timestamp — FR-PRP-12, NFR-AUD-01/02), review
    state (`PENDING | EDITED | ACCEPTED | REJECTED` — FR-REV-01 verbatim; mutable
    column, transitions are service-level).
  - `proposal_revision`: one immutable revision per edit (FR-REV-03), who changed which
    fields (FR-REV-09: `changed_fields` JSON-as-supplied), full field snapshot on each
    revision (including attribute and affected-vertical child rows); original generated
    version unchanged (storage permits UPDATE — immutability is a service-level
    discipline, no triggers).
  - "Unconfirmed" markers (AC-06/FR-UI-06) are **derived at the service layer** from
    `changed_fields`; no stored flag column exists in this schema.
  - Acceptance integrity (FR-REV-05 "Accepting shall create exactly one task or
    announcement. Two simultaneous accepts, or a repeated accept, shall produce one";
    AC-18) — analyzed against the project's constraints (SQLite declarative
    constraints only; no triggers; no new dependencies):
    - Alternatives considered and rejected: (a) triggers serializing an accept
      across the task and announcement tables — prohibited by the no-trigger
      invariant (§5 item 7); (b) cross-table CHECKs — SQLite CHECK expressions
      cannot reference other tables and cannot contain subqueries; (c) one merged
      accepted-record table keyed `UNIQUE (proposal_id)` with a kind
      discriminator — the only declarative XOR shape, but it collapses the §4.1
      Task and Announcement entities (distinct state semantics: FR-TSK-02
      dimensions vs announcement audience) and is a storage-design change that
      requires owner approval, not a verification fix; (d) a proposal-side
      outcome-kind marker or a separate acceptance-registry table that both task
      and announcement would reference — adds derived state with no declarative
      enforcement (nothing forces the marker/registry row to match the outcome
      row, or to exist at all), rejected as derived-authoritative duplication
      (DR-03/DR-06 discipline).
    - Strongest schema guarantees under these constraints: 2.7's `task` and
      2.10's `announcement` each carry `UNIQUE (proposal_id)` with RESTRICT
      FK chains into `proposal`. Combined, the two tables hold **at most one task
      and at most one announcement per proposal**; a proposal holding one of each
      simultaneously is NOT excluded by the schema.
    - Remaining service-level guarantees (precise): an accept decision writes
      exactly one of the two kinds, never both, inside one transaction (this
      serialization is what satisfies AC-18); no task/announcement row may be
      created except as part of an accept decision; and there is no acceptance
      without either kind (FR-REV-06 blocking, FR-REV-07 rejection retention are
      also service-level).
- Dependencies: 2.5 (attempt/provenance FKs), 2.2 (roster), 2.3 (messages).
- Tests (delivered, 46 tests): migration
  application/idempotency/ordered versions/failure isolation (out-of-band
  0900/0901 fixtures); ptype, assignment-mode and task-type vocabularies with
  unknown/whitespace/NULL rejections (FR-PRP-06/08/09); FR-PRP-08 directional rule
  tested directly (TASK+NONE rejected; ANNOUNCEMENT+NONE and ANNOUNCEMENT+OPEN
  stored), including the deliberately unenforced converse; review-state default
  PENDING + token/invalid domain (FR-REV-01); description/assignee-text
  NULL-vs-blank domains with verbatim storage (FR-PRP-11/DR-04); people-needed
  domain (positive number, 0/1 UNLIMITED encoding, mutual exclusion, unfilled
  allowed; FR-REV-06); structurally_valid boolean (FR-PRP-13); source-vertical
  required and suggested responsible vertical optional; provenance required,
  verbatim, non-blank (FR-PRP-12/NFR-AUD-01/02); composite FK pairings accepted
  and mispaired/mismembership rejected (FR-PRP-12/FR-REL-01 chains); multiplicity
  — several proposals from one message in one run (AC-02), zero proposals valid
  (AC-03), retry via a new attempt (NFR-REL-01/03 shape); context/attribute/
  affected-vertical link tables (member-only membership, per-proposal message
  uniqueness, duplicate/unknown rejections); revisions — minimal row, sequence
  numbering + duplicate revision_number rejection, unknown proposal/person,
  changed_fields non-blank, snapshot values independent from the proposal row,
  proposal row unchanged after a revision insert, multiple revisions, per-revision
  attribute and affected-vertical children; deletion RESTRICT in every direction;
  `PRAGMA foreign_key_check` clean on a mixed workload; duplicate revision insert
  via a second connection keeps one row (NFR-CON-02 shape).
- Acceptance: AC-02, AC-03, AC-05 storage aspects.
- Exclusions: model runs, reviewer UI, edit/accept endpoints, assignee resolution
  *execution* (resolution logic = Phase 3), notifications (2.9 records only).

### Slice 2.7 — Tasks, assignee records, state dimensions, history [x committed, `4e4aae2`]
- Purpose/scope (implemented): `0006_tasks.sql` — `task` per FR-TSK-01 (persistent ID,
  description, task type, source vertical, responsible vertical, affected verticals,
  creator = the accepting reviewer, assignment mode, people needed, attributes) with
  `version` for FR-TSK-12 stale-edit detection; `task_assignee` per FR-TSK-03/05 and
  §4.1 ("Person, agreement, work status, timestamps"); `task_history` append-only per
  FR-TSK-11/NFR-AUD-01 (`field_name`, old/new values, `changed_at`, actor = person **or**
  model-with-version as FR-TSK-11 words it, `actor_role`, actor's vertical, reason);
  `task_attribute`; `task_affected_vertical`. FR-TSK-06/13 bodies were restored in
  `docs/SRS.md` (verbatim per the original DOCX) before this slice was implemented.
- State dimensions per FR-TSK-02 are independent fields: assignment mode / cancellation /
  approval state on `task`; agreement / work status / cancelled flag on each assignee
  record — no single field encodes more than one dimension. Vocabularies are SRS-verbatim:
  EXPLICIT|OPEN; AWAITING|ACCEPTED|DENIED; NOT_STARTED|IN_PROGRESS|COMPLETED;
  ACTIVE|CANCELLED; NOT_REQUIRED|PENDING|APPROVED|DENIED.
- Schema-expressible integrity: task type consistency per FR-XV-01 (CROSS_VERTICAL iff
  responsible exists and differs from source; INTERNAL has no responsible value or keeps
  the source vertical); people encoding mirrors owner-approved 0005 (`people_needed`
  NULL ⇔ UNLIMITED with `people_needed_unlimited` 0/1; EXPLICIT always 1); task links
  at most one task to a proposal (`UNIQUE (proposal_id)` — the task side of the FR-REV-05/
  AC-18 single-object rule); reasonable-value CHECKs (version ≥ 1, non-blank description/
  created_at, timestamps non-blank); `agreed_at` records the agreement decision time and
  exists iff agreement ≠ AWAITING; RESTRICT FKs across the task→proposal→attempt→bucket
  chain and roster (DR-01/DR-02/DR-04).
- Transition enforcement (FR-TSK-06/13, resolved): under the project's no-trigger storage
  pattern (SQLite CHECKs cannot compare a row across time) the schema cannot encode
  transition legality; the schema contributes (a) SRS-verbatim vocabularies on the
  current values, (b) the shape invariant that every assignee record starts
  NOT_STARTED/AWAITING, and (c) the append-only `task_history` where every recorded
  change (including each work-status and agreement transition) is stored with field,
  old, new, time, reason and actor. Legality (NOT_STARTED → IN_PROGRESS → COMPLETED,
  COMPLETED final; AWAITING → ACCEPTED/DENIED, FR-TSK-05/06/13) is the accepted service
  layer's guarantee — same classification as the FR-REV-05/AC-18 task/announcement XOR.
  FR-TSK-14 (AI never changes tasks) stays service-level; the history table's actor
  fields store what was recorded (FR-TSK-11 wording) without authorizing model actors.
- Claim capacity (FR-TSK-04, AC-08: two claims for the last place → one winner) is a
  cross-row rule; the schema provides `(task_id, person_id)` uniqueness and
  `people_needed`; the cap is service-level (NFR-CON-02).
- Dependencies: 2.6 (FR-REV-05), 2.2, 2.3, 2.5.
- Tests (delivered, 73 tests): migration creation/idempotency/ordered versions/failure
  isolation (0900/0901 fixtures); minimum row + defaults (approval NOT_REQUIRED,
  cancellation ACTIVE, version 1); task-type↔verticals consistency (valid shapes incl.
  same-vertical INTERNAL; INTERNAL-mismatch / CROSS_WITHOUT_RESPONSIBLE / CROSS_SAME
  rejected); people-encoding exclusivity across assignment modes (EXPLICIT=1 always;
  OPEN count or NULL-only-with-UNLIMITED; 0/-1/count-with-1 rejected); approval-state,
  cancellation and version domains; verbatim non-blank text; at-most-one task per
  proposal (`UNIQUE`) — duplicate task row via a second connection keeps one row
  (AC-18 schema side); two proposals → two coexisting tasks (AC-02 at task level);
  assignees — two records on one task, defaults (AWAITING / NOT_STARTED / not-cancelled),
  duplicate rejected, unknown task/person rejected, agreement + work-status token domains
  (invalid include None/whitespace), `agreed_at`-iff-decided shape, cancelled flag domain,
  task-vs-assignee cancellation independence (FR-TSK-02), removal and reassignment
  (FR-TSK-05/16 storage shape), `added_at` required; attributes/affected-verticals —
  storage incl. NULL attribute value, per-task uniqueness, unknown member rejection;
  history — verbatim row, append-only two-edits shape (FR-TSK-11/NFR-AUD-03 storage
  half), model actor stored with version, actor-shape violations rejected (both actor
  kinds / neither / model without version), required fields non-blank, NULL old-value,
  unknown task FK, person deletion restricted; recorded work-status + agreement change
  sequence (schema contribution to FR-TSK-06/13); full-link workload `PRAGMA
  foreign_key_check` clean (NFR-AUD-01/DR-01); deletion RESTRICT in every direction
  (DR-02); duplicate-assignee cross-connection shape (NFR-CON-02).
- Acceptance: AC-06/AC-16 storage aspects, AC-18 schema side, AC-19 data (version +
  history).
- Exclusions: claiming/status/cancel/cancel-notification *flows* (FR-TSK-04/05/07/08/16
  behavior), stale-edit *rejection* transaction (FR-TSK-12 service), notifications
  (2.9), approval requests/decisions (2.8), any AI involvement (FR-TSK-14 service).

### Slice 2.8 — Cross-vertical approvals [x committed, `4e4aae2`]
- Purpose/scope (implemented): `0007_cross_vertical_approvals.sql` — `approval_request`
  (one per task: `task_id NOT NULL UNIQUE`; FR-XV-03 requester, optional requester
  message, `created_at`), `approval_recipient` (request-time snapshot of "every AH and
  the Head of the responsible vertical" — person rows referenced at request time;
  role/time eligibility is roster-state, service-level per FR-ROS-01),
  `approval_decision` (`UNIQUE (request_id)` — the schema half of first-decision-wins,
  FR-XV-04/AC-15; `decided_by`, APPROVED|DENIED vocabulary per FR-TSK-02, optional
  never-blank decision message, `decided_at`). A Head's own cross-vertical task has no
  request row (FR-XV-02, AC-14). Denial keeps the decision (FR-XV-05); recording
  requests/decisions/messages in `task_history` with actor role/vertical (FR-XV-06) is
  a service write into the 0006 table.
- Deliberately separate: approval state is the FR-TSK-02 Approval dimension on `task`.
  Approval decisions are never conflated with assignee agreement (FR-TSK-13) or work
  status (FR-TSK-06) — disjoint tables/columns; the schema cannot check that
  `decided_by` was a recipient at decision time (service guarantee), and visibility
  (PENDING tasks hidden from volunteers) is app behavior.
- Dependencies: 2.7 (FK into `task`), 2.2 (roster), 2.6 (proposal chain).
- Tests (delivered, 25 tests): migration creation/idempotency/ordered versions/failure
  isolation; minimum request row (NULL message default); one request per task
  (`UNIQUE task_id` duplicate, unknown task FK); requester message verbatim; blank
  request/decision messages rejected (each rejection is the first insert in scope so
  CHECK is never shadowed by UNIQUE); `created_at` non-blank; unknown requester +
  person deletion restricted; Head's cross-vertical task has no request row (AC-14);
  recipients — snapshot rows, duplicate pair rejected, unknown person/request rejected,
  recipient person deletion restricted; decision vocabulary APPROVED/DENIED (unknown/
  whitespace/None rejected), decision message optional-verbatim, required fields,
  unknown decider; first decision wins (`UNIQUE` — second decision rejected; second-
  connection attempt keeps one row, NFR-CON-02); DENIED decision + task state kept
  (FR-XV-05 storage half); task approval-state updates PENDING→APPROVED follow the
  vocabulary (FR-TSK-02); full chain `PRAGMA foreign_key_check` clean; task/proposal/
  attempt/request deletion RESTRICT (DR-02).
- Acceptance: AC-14/AC-15 schema aspects.
- Exclusions: visibility enforcement, decision-time recipient re-check, requester
  notification (2.9 records), history recording of approval events (service), any UI.

### Slice 2.9 — Notification records [x committed, `4e4aae2`]
- Purpose/scope (implemented): `0008_notifications.sql` — `notification` per FR-NOT-01/
  §4.1 ("Event, recipient, delivery state"): `event_type` carries the service's event
  label as free text (the FR-NOT-01 table lists events in prose; no invented token
  vocabulary), `recipient_person_id` FK, exactly one subject FK — proposal, task,
  approval request or announcement — enforced by a CHECK sum, so every notification
  connects to its authoritative record chain (DR-01); `delivery_state` free text with
  a `PENDING` default (the SRS names no token set — recording only); `created_at`.
  `notification_attempt` per FR-NOT-04/NFR-REL-02/AC-17: append-only attempt rows
  ordered by `attempt_number` (UNIQUE per notification, ≥ 1), each storing `outcome`,
  optional never-blank `error`, `recorded_at` — a failure is recorded as its own row
  and can never undo the committed change that raised it.
- Deduplication (FR-NOT-05, NFR-REL-01): `dedupe_key NOT NULL UNIQUE` — the service
  composes the key from event + recipient + subject, so a retry inserts nothing and
  therefore sends no duplicate. Two different events for the same proposal coexist
  (FR-NOT-02: one notification per event, no batching).
- Storage does not deliver: sending/push channels/workers (FR-NOT-04 retry *execution*,
  FR-NOT-06 push capability, FR-NOT-07 phone/in-app surfaces — Phase 3/app),
  wording fidelity (FR-NOT-03, presentation), masking/value vocabularies beyond the
  non-blank rule.
- Dependencies: 2.2, 2.6, 2.7, 2.8 (subject FKs), 2.10 (announcement subject).
  Cross-slice ordering note: `0008` declares `announcement_id ... REFERENCES
  announcement`, but the `announcement` table is created by `0009` (2.10), which
  applies immediately *after* `0008`. SQLite resolves FK targets lazily at DML
  time, so the ordered fresh-DB apply works (probe: versions `[1..9]`, 0 FK
  violations, and all four subject types record). Limitation: a database migrated
  only through version 8 cannot accept announcement-subject notification inserts
  ("no such table") until `0009` is applied — always apply through the full
  manifest before any service writes. Renumbering would churn committed names,
  tests, and documentation for no behavioral gain; the runner applies the full
  ordered set every time. Standing probe: a fresh database migrated through
  version 11 (pre-A4 schema) is PRAGMA foreign_key_check clean - recorded by
  tests/test_migrations_fk_check.py.
- Tests (delivered, 22 tests): migration creation/idempotency/ordered versions/failure
  isolation; minimum task-subject row with `PENDING` default; each of the four subject
  types records exactly one FK; zero/two subject combos rejected; unknown recipient or
  subject rejected; required text fields non-blank; two events for one proposal coexist
  (FR-NOT-02); retry with the same event/recipient does not duplicate a notification
  (`UNIQUE` — plus a cross-connection variant, NFR-CON-02); same event to two recipients
  and on two tasks coexist; `delivery_state` stores the service's word verbatim; attempt
  rows stored and ordered, duplicate `attempt_number`/zero/unknown notification rejected;
  blank outcome/error-domain/time-format rejections; AC-17 failure-shape (committed task
  untouched while SUCCEEDED/FAILED attempt rows accumulate); full-chain
  `PRAGMA foreign_key_check` clean; deletion RESTRICT for recipient/subjects/notification.
- Acceptance: AC-17 (recording half).
- Exclusions: delivery, retry workers/scheduling, in-app list UI, push integration
  (Phase 3+; FR-PM-06 batching is Post-MVP).

### Slice 2.10 — Announcement records [x committed, `4e4aae2`]
- Purpose/scope (implemented): `0009_announcements.sql` — accepted-announcement record per
  FR-REV-10 ("shall be shown to its audience (a Head's vertical, or the sender AH's
  volunteers) and shall not create a task") linked to its proposal and acceptance
  reviewer, with audience definition fields. FR-NOT-06 ("Heads shall be able to push
  announcements and notifications to their vertical; AHs ... to the volunteers under
  them") is the *push capability* — its enforcement and delivery belong to Phase 3
  (§3.9 authorization row "Push announcements", §8 later-phase boundary); it is not a
  field of this schema. Announcements are proposals with type ANNOUNCEMENT (FR-PRP-06)
  that become announcement records on accept — no separate "task-like" state.
- As implemented: one `announcement` row per accepted proposal — `proposal_id NOT NULL
  UNIQUE` (the announcement's schema side of the FR-REV-05/AC-18 single-object rule, per
  the Slice 2.6 acceptance-integrity analysis), `accepted_by`/
  `accepted_at` verbatim (FR-REV-01/02), audience as `audience_vertical_id` FK (nullable;
  a Head's vertical) plus `audience_scope` free text NOT NULL (the SRS defines no audience
  vocabulary). "Shall not create a task" is service-level. Deletions RESTRICT the
  proposal/person/vertical chain (DR-01/DR-02).
- Dependencies: 2.6 (proposal and review-acceptance chain), 2.2 (vertical roster for
  `audience_vertical_id`). No FK into task tables — `0009` depends only on `0005` tables
  and applies alongside them (gap-tolerant runner); the task/announcement XOR itself is
  a service-level guarantee completed by 2.7's `task`.`UNIQUE (proposal_id)`.
- Tests (delivered, 12 tests): migration creation/idempotency/ordered versions (gap-tolerant
  set assertions — 0009 applies without the 2.7–2.9 tables existing); minimum fields stored
  verbatim with NULL `audience_vertical_id`; named-vertical audience row; unknown
  proposal/person/vertical rejections; NOT NULL and blank-field rejections; one announcement
  per proposal (UNIQUE — duplicate insert rejected); two separately accepted proposals each
  keep their own announcement row; deletion RESTRICT (person/vertical/proposal);
  `PRAGMA foreign_key_check` clean.
- Exclusions: display surfaces (FR-UI-*), push delivery and authorization (FR-NOT-06/07,
  Phase 3+).

### Slice 2.11 — Latency measurement records [x committed, `4e4aae2`]
- Purpose/scope (implemented): `0011_latency_measurements.sql` (version 11) —
  `latency_measurement`, the NFR-PERF-02 recording shape (§6.4, NFR-PERF-02: "The system
  shall record ingestion delay, bucket delay, classifier latency, proposal latency and
  review waiting time, as P50, P95 and P99"). One row per raw measurement: `metric`
  non-blank free text (the SRS names the five kinds in prose; no invented token
  vocabulary per the D1 rule — same rationale as 0008's `event_type`), `value_seconds`
  REAL NOT NULL ≥ 0 (unit named by the column; the SRS defines no unit vocabulary),
  `recorded_at` verbatim non-blank, and **exactly one subject FK** — `source_message_id`,
  `bucket_id`, `processing_attempt_id` or `proposal_id` — via the 0008 CHECK-sum
  pattern, keeping every measurement traceable to its authoritative record chain
  (DR-01); all FKs RESTRICT (DR-01/DR-02).
- Deliberately not in the schema: P50/P95/P99 rows — NFR-PERF-02's "as P50, P95 and P99"
  is the reporting form of the recordings; DR-03 (derived data never replaces source
  content) and DR-06 (derived stores must be rebuildable from authoritative records) make
  the raw measurement rows the authoritative record, and no SRS text requires persisting
  percentile snapshots (the raw rows support later percentile computation at
  read time; this slice implements no computation); the metric↔subject-type
  pairing and per-metric value derivation (service-level, Phase 3); any §6.4 target values
  (§6.4 targets are filled in the SRS; recording does not depend on them
  and none is invented). No `metric` vocabulary CHECK: the SRS names the five kinds in
  prose, not as tokens, and the D1 rule forbids a vocabulary-enumerating CHECK without
  explicit owner approval at a slice's planning step (0008 `event_type` / 0009
  `audience_scope` / 0004 `stage` precedent).
- Dependencies: 2.3 (source_message), 2.4 (bucket), 2.5 (processing_attempt),
  2.6 (proposal).
- Tests (delivered, 15 tests): migration creation through v11/idempotency/failure
  isolation (0900/0901 fixtures); minimum row with verbatim storage; each of the four
  subject types records exactly one FK; zero/two-subject combos rejected; unknown
  subject rejections; metric label domain (blank/whitespace/NULL); value domain
  (NULL/negative rejected, 0 accepted); `recorded_at` required non-blank; many
  measurements per subject coexist (percentile grain); same metric label across two
  subjects coexists (no UNIQUE on metric); deletion RESTRICT for all four subjects and
  free deletion of measurement rows; `PRAGMA foreign_key_check` clean on a mixed
  workload; cross-connection rows coexist (NFR-CON-02 shape).
- Acceptance: recording shape only. Computing percentiles, deriving each metric from
  source timestamps, benchmarking against §6.4 targets (NFR-PERF-01/03) are later-phase.

### Slice A4 — Review resolution and mention storage [x] implemented (schema + tests, uncommitted; owner-directed correction)

- Provenance: owner-directed correction slice (2026-10-20), implemented after
  the read-only pre-commit audit of the committed Phase 2 set. One additive
  migration, `0012_resolution_and_mentions.sql` (version 12); migrations
  `0001`–`0011` are untouched.
- Purpose/scope (implemented): three tables completing the review/assignment
  resolution data surface (vocabulary CHECKs per the D1 rule, per the
  migration header).
  - `proposal_decision` (FR-REV-05, FR-REV-07, AC-04) — the sender's single
    review outcome per proposal: `decision` ACCEPTED|REJECTED, `decided_by`
    person FK RESTRICT, verbatim `decided_at`, optional never-blank `note`
    (the sender's comment made with the decision). `UNIQUE (proposal_id)` is
    the schema side of "one decision per proposal" — the table records the
    current outcome; who may decide is service-level (FR-REV-09). The live
    review state (PENDING/EDITED included) stays on `proposal.review_state`
    (0005).
  - `proposal_assignee` (FR-PRP-10) — each assignee exactly as written in the
    proposal text plus the backend's resolution state: RESOLVED carries the
    resolved person FK and the route MENTION|NAME_MATCH; UNRESOLVED (several
    plausible matches) and FLAGGED (no match) carry neither — the CHECK makes
    an implicit guess unrepresentable. Human confirmation happens at review
    (FR-REV-01/06, service level). Proposal-scoped by design: the proposal's
    current text is what is resolved, and each revision already snapshots its
    values (0005).
  - `message_mention` (FR-ING-02, EIR-03) — mentions as opaque Discord user
    IDs only, never resolved roster identity; `position` optional (NULL or
    ≥ 0); no UNIQUE, so a repeated mention of the same user in one message is
    two rows of preserved evidence (DR-03); source_message FK RESTRICT.
  - All FKs RESTRICT (DR-01/DR-02/DR-04); no additional indexes.
- Deliberately not in the schema (service-level, per the migration header):
  decision authorization (the sender-only reviewer, FR-REV-09), decision
  history itself (owner decision 2026-10-20: latest decision only, no
  history requirement — see §7), that REJECTED proposals are never
  converted into tasks
  (the FR-REV-05 XOR, service level plus the 0006/0009 UNIQUE keys), how
  assignees are resolved, and mention-extraction timing.
- Dependencies: 2.2 (person), 2.3 (source_message), 2.6 (proposal).
- Tests (delivered, 19 cases from 18 functions; the decision-token test is
  parametrized ×2): migrations — tables created in the default manifest,
  reapplication idempotent with versions 1–12; decision — both tokens
  recordable, unknown token rejected, second decision for the same proposal
  rejected (`UNIQUE`; the docstring records the owner decision that a later
  permitted decision overwrites the row — latest decision only, no history),
  decider must be a known person, proposal and person deletion both RESTRICT
  a stored decision, note optional and never blank (whitespace rejected);
  assignees — all three resolution states recordable, RESOLVED without
  person/route rejected, FLAGGED/UNRESOLVED with person or route rejected,
  invalid route token rejected, unknown person rejected; mentions — stored
  with optional position, duplicate mention of the same user preserved as
  two rows (DR-03), blank ID rejected, unknown message rejected; AC-02
  restated — a task and an announcement proposal from the same primary
  message in one generation run, both stored independently, and the workload
  ends `PRAGMA foreign_key_check` clean. Standing probe recorded separately
  by `tests/test_migrations_fk_check.py`: a fresh database migrated through
  version 11 (every Phase 2 data-layer table, before the A4 additions) has
  zero `PRAGMA foreign_key_check` violations, with `0008` untouched (see the
  Slice 2.9 ordering note).
- Acceptance: recording-shape aspects of AC-04 (a reject stores its decision)
  and AC-02. Ownership/authorization, assignee-resolution execution,
  mention-extraction timing, review flows, accept/reject execution and any
  notification are **not** implemented and are not claimed by this slice.

## 4. Migration numbering (derived from the repository, verified)

| Version | File | Slice | Status |
| --- | --- | --- | --- |
| 1 | `0001_core_roster.sql` | 2.2 | committed |
| 2 | `0002_source_messages.sql` | 2.3 | committed |
| 3 | `0003_buckets.sql` | 2.4 (+2.4.1 correction) | committed (HEAD `855ebb7`) |
| 4 | `0004_processing_attempts.sql` | 2.5 | committed at `4e4aae2` |
| 5 | `0005_proposals.sql` | 2.6 | committed at `4e4aae2` |
| 6 | `0006_tasks.sql` | 2.7 | committed at `4e4aae2` |
| 7 | `0007_cross_vertical_approvals.sql` | 2.8 | committed at `4e4aae2` |
| 8 | `0008_notifications.sql` | 2.9 | committed at `4e4aae2` |
| 9 | `0009_announcements.sql` | 2.10 | committed at `4e4aae2` |
| 10 | `0010_source_group_progress.sql` | bucket-formation slice (Revision 4) | committed at `4e4aae2` |
| 11 | `0011_latency_measurements.sql` | 2.11 | committed at `4e4aae2` |
| 12 | `0012_resolution_and_mentions.sql` | A4 correction slice (owner-directed) | implemented (schema + tests, uncommitted) |

Rules: strictly increasing, one logical group per migration, never edit a committed
migration (checksums enforce); later discoveries that would require editing a committed
migration must use a rebuild migration instead (the 2.4.1 pre-commit correction is the
only exception and was made before commit for that reason).

## 5. Architecture invariants (from SRS §2.2, §5, §9 and AGENTS.md)

1. The backend owns authoritative state (proposals, tasks, states, history,
   notifications). AI functions are stateless request/response; they **never** touch the
   database (AI-02: output is advisory, never written directly to authoritative state).
2. The frontend talks only to backend APIs; never to Discord or model services directly.
3. Source messages and evidence are preserved unmasked, verbatim (DR-03, AC-21); derived
   data never replaces source content. No message silently truncated or deleted
   (FR-BKT-11).
4. Foreign keys enforced on every connection (`PRAGMA foreign_keys = ON`, verified);
   RESTRICT semantics; no orphans (DR-01). Person/group/vertical referenced records are
   never hard-deleted away (DR-02, FR-AUT-07: deactivation keeps attribution).
5. History and processing records are append-only (DR-07, NFR-AUD-03, FR-TSK-11).
6. Idempotency/retry at every stage (NFR-REL-01..04); a failed later stage never erases
   earlier results; message/notification storage is durable before dependent processing
   (FR-ING-05, FR-NOT-04 pattern "commit the change first").
7. No new runtime dependency without explicit approval; no triggers, no agents, no vector
   stores, no queues unless an approved slice and the SRS justify them.
8. Buckets are processing boundaries only (FR-BKT-09/10); reactions and silence are never
   acceptance (FR-PRP-15, FR-TSK-05, AC-09, AC-22); voice/incidents are Post-MVP
   (FR-PM-10) and must not appear.

## 6. Privacy

Masking scope is fixed by NFR-SEC-04: **phone numbers, email addresses, institute roll
numbers** masked before every AI call; originals backend-only. The committed masking
module proactively implemented phone-number masking (8c9fafa, 4fe4f66). Do **not** add
Aadhaar/PAN or other masking — not in the SRS. Seed content for AI calls is data-minimized
synthetic fixtures (AGENTS.md), and Discord/model output is untrusted data, never
instructions.

## 7. Open requirements (do not invent; owner decisions pending)

| Requirement | State | Blocks |
| --- | --- | --- |
| **FR-BKT-04**, **FR-BKT-05** | bodies now filled (§3.2, owner-approved restorations). **Implemented 2026-10-19** in `backend/bucketing.py` with the approved Revision 4 semantics: strict FR-BKT-04 inequalities; `e = min(s + M, max(s + W, l + G))` (FR-BKT-05); closure only via accepted per-group event-time progress, `e + C <= P` inclusive; assignment never closes buckets or advances progress; late-message and predecessor-graph rules per Revision 4 | closed for this slice; see bucket-formation slice report for pending SRS amendments (progress authority, recovery-link fallback, cross-call order sensitivity) |
| **FR-TSK-06**, **FR-TSK-13** | **resolved 2026-10-19** — bodies restored verbatim per the original SRS DOCX (owner-verified outside this repository; now §3.6) | none (Slices 2.7–2.9 implemented against the restored texts) |
| Deferred 2.3/2.4-era decisions | role vocabulary, post-recovery UNCLASSIFIED clearing, `parameters_used` scope (the unknown-channel ingestion policy was decided 2026-10-20 — see §10, Slice 3.1) | future slices owning those behaviors |
| **Piggyback → ASEC transport and event contract** | **Unresolved — external.** This repository contains no producer, adapter, transport or delivery path; `ingest_message`/`ingest_events` are called only by their tests. README documents the split ("the backend should receive ASEC events, not Discord objects"), confirms a Discord bot exists **outside** this repository, and states its event schema is "still under development" — including payload fields (`sender.asec_user_id/role/vertical`) that conflict with the backend's authority model (roster lookup by Discord ID stays backend-owned, FR-ING-10). Required before any transport slice: production schema vs `MessageEvent`, timestamp format, mention-ID opacity, reaction direction tokens, transport choice (owner decision; none exists by design), concurrency/connection lifecycle, and redelivery identity guarantees. Backend-side contract and integration checklist: §11.3 | blocks the FR-ING-01 producer half and any transport slice; does not block further backend or replay work |
| **Replay batch failure semantics (FR-ING-08)** | **Owner decision recorded 2026-10-21 — partial success implemented (service-level, no schema change).** The earlier all-or-nothing choice (2026-10-20) was superseded by an explicit owner decision in favor of partial success. `ingest_events` (§11.2) now runs **one transaction per record** — exactly the live path: each record lands as CREATED, DUPLICATE or REJECTED; a record-specific validation or constraint failure reports REJECTED and rolls back only that record (no trace, not even D-1 registration), so records before and after a rejected record stay durable; duplicate delivery remains idempotent per record; a non-record-specific (systemic) failure aborts the batch and raises `IngestionBatchError` carrying the durable prefix of outcomes with the original error chained — never reported as successful durable ingestion. The batch return contract changed from `tuple[StoreResult, ...]` to `tuple[RecordOutcome, ...]` (in-repo callers verified: tests only; the external Piggyback transport contract is untouched, no new transport assumptions) | none (owner-decided; no schema change; single-event `ingest_message`/`ingest_reaction` contracts unchanged) |
| **`proposal_decision` outcome history (0012)** | **Owner decision recorded 2026-10-20 (documentation only — no schema, code, or test change).** Store **only the latest review decision per proposal**; do not preserve a history of earlier decisions. Already supported by the existing schema, nothing to build: `0012_resolution_and_mentions.sql` admits at most one `proposal_decision` row per proposal (`UNIQUE (proposal_id)`), and the table carries no append-only constraint or trigger, so a changed decision is recorded by updating the existing row (service-level discipline). The proposal and its review decision remain **separate records** (`proposal` in 0005 holds content; `proposal_decision` in 0012 holds the outcome); recording a decision must not silently modify the proposal's content — content changes stay in the separate edit path (FR-REV-03, FR-REV-04: each edit its own revision, edit is not accept). **Status consistency:** when a decision is recorded, the service must bring `proposal.review_state` (0005: FR-REV-01 vocabulary PENDING/EDITED/ACCEPTED/REJECTED, NOT NULL, CHECK-enforced, default PENDING, mutable column) into the SRS-supported state matching that decision — the outcome tokens ACCEPTED/REJECTED are FR-REV-01 states and the accept/reject semantics are FR-REV-05/FR-REV-07 text. The decision write and the matching `review_state` update must be **atomic** (one transaction) when the review workflow is implemented, using the established single-connection `transaction()` pattern (NFR-CON-02 discipline). No new fields, statuses, transitions, or migration are authorized or required by this decision | A4-era review service slice (implements the flow; atomicity requirement recorded here) |
| **Review-state transition edges (Figure 6)** | **Unresolved — resolved only by the SRS DOCX.** FR-REV-01 fixes the status vocabulary and concrete outcomes in text, but the complete legal transition set (which edges exist, including into/out of EDITED, and whether ACCEPTED or REJECTED can ever move again) appears only in the review-states diagram; the Markdown SRS carries it solely as an image placeholder ("Figure 6 — Proposal review states", section 3.5). Per AGENTS.md the SRS v2.0 DOCX is the visual source for diagrams. The exact edges to enforce must be read from Figure 6 at the review service slice's planning step; they are not invented here | none (blocks transition-enforcement tests in the review service slice) |
| **Bucket lifecycle transition edges (FR-BKT-12 service)** | **Implementation choice — owner-vetoable.** The SRS fixes the five-state vocabulary only (FR-BKT-12); migration `0003`'s header explicitly defers transitions to later processing slices. Phase 3's `backend/bucket_lifecycle.py` enforces: CLOSED → PROCESSED / FAILED / AWAITING_RECOVERY; FAILED → AWAITING_RECOVERY (NFR-REL-03 manual re-queue); AWAITING_RECOVERY → PROCESSED / FAILED (NFR-REL-05, AC-13 resume); same-state re-calls are idempotent no-ops (NFR-REL-01); PROCESSED is terminal; OPEN ↔ CLOSED is never a service edge (closure stays owned by accepted event-time progress, FR-BKT-05). Every illegal edge fails loud with nothing written | owner may veto any edge at review/commit time without a schema change (states are CHECK-enforced, edges are service code) |
| **Changed decision after accept-side effects** | **Unresolved — SRS is silent; do not guess.** Latest-decision storage means a recorded decision can be overwritten by a later one. If an ACCEPTED decision has already produced its task or announcement (FR-REV-05/FR-REV-10) and a new decision is then recorded, the SRS defines no reconciliation: there is no un-accept flow, and FR-REV-05's single-object guarantees plus FR-REV-07's record-retention wording do not describe reversal. Whether a re-decision may target a decided-and-accepted proposal at all (versus only undecided/edited ones), and what happens to already-created records, must be defined by the owner at the review service slice's planning step. Schema impact of the storage decision itself: none | none (service-slice design question; would need a new migration only if the owner later requires new records) |

Note on parameter sets: §3.2 fixes the production set (W=15, G=5, M=30, C=2) and §10.1
declares its own test parameter set (W=10, G=5, M=30, C=3). These are different roles
(test matrix vs production defaults), not a conflict; the former §3.2-vs-§10.1
"conflict / owner amendment P6" item is removed accordingly, and the §6.4 and §7
target cells are filled in the SRS, so their former "blank owner decisions" items are
removed as well.

## 8. Later-phase boundary (not delivered by Phase 2)

Bucket formation itself was implemented after Phase 2 (approved Revision 4 slice,
2026-10-19): `backend/bucketing.py`, migration `0010_source_group_progress.sql`,
`tests/test_bucketing_service.py`. Proposed SRS amendments recorded there are pending
owner approval; the original requirement texts are preserved. Review fix M1 is applied
(closure uses each bucket's persisted `parameters_used`; missing or malformed records
fail loudly).

Latency instrumentation: the NFR-PERF-02 *recording* shape is Phase 2 (Slice 2.11,
migration `0011`). Scope boundary, grounded in the SRS: NFR-PERF-02 (§6.4) requires
the system to record the five named latency kinds "as P50, P95 and P99" — the recordable
atomic data is one row per raw measurement, and the percentile form is how those
recordings are reported. DR-03 (§4.2: derived data never replaces source content) and
DR-06 (§4.2: any derived store shall hold references to authoritative records and be
rebuildable from them) make the raw measurement rows the authoritative record; stored
percentile snapshots would be derived data with no SRS requirement anywhere to persist
them. Computing percentiles, deriving each metric from source timestamps, and
benchmarking against the §6.4 targets (NFR-PERF-01, NFR-PERF-03; the §10.3
"A latency benchmark is recorded" milestone) are later-phase work. The `metric` column
carries free-text labels because the SRS names the five kinds in prose, not as token
constants, and the established D1 rule forbids adding a vocabulary-enumerating CHECK
without explicit owner approval at a slice's planning step (same rationale as 0008's
`event_type`, 0009's `audience_scope`, and 0004's `stage` per AU-2); the service layer
owns using the SRS's five kinds as labels.

Executable pipeline behaviors, orchestration and serving work all belong outside this
phase, chiefly:

- Discord ingestion (live adapter, export replay), pipeline orchestration and workers
  (NFR-REL-01..06 recording *records* is Phase 2; running them is not);
- AI: relevance classification and proposal generation execution (SRS §7, AI-01..08),
  fallback model logic, evaluation sets/harness;
- Authorization enforcement (FR-AUT-01..08: every protected operation server-side,
  permission matrix with per-cell automated tests before production);
- Proposal review flows and task claiming/status/cancel execution (FR-REV, FR-TSK-04/05/08,
  FR-XV-03..05 visibility/decision effects);
- Notifications delivery (push + in-app) and announcement surfacing (FR-NOT-06/07, FR-UI-01..03,
  FR-REV-10 display);
- Post-MVP features (FR-PM-01..10 and §10.2 post-MVP scenarios) — reactions as acceptance,
  volunteer routing, context processing (episodes/relationships), human promotion
  interface, notification batching, multi-primary proposals, source edit versions,
  attachments/tooling, FC role, voice/incidents.

## 9. Workflow (every remaining slice)

1. `/slice <number>` produces a **plan only** — files, SRS IDs, tests, commands, risks,
   unresolved questions — and nothing is implemented without explicit approval.
2. Tests written first and shown failing for the expected reason before the migration is
   added (the 2.3/2.4/2.4.1 pattern).
3. Full check suite after each slice: full `pytest -p no:cacheprovider -q`,
   `ruff check . --no-cache`, `mypy --no-incremental`, `python -m backend --version`;
   plus focused per-file runs; plus targeted integrity probes (`PRAGMA foreign_key_check`
   clean after the bucket/membership workloads).
4. Read-only `/review` before any commit; nothing fixed inside a review.
5. Commit only after review is green **and** the project owner explicitly authorizes the
   commit. Never claim unrun tests passed; report exact commands and outcomes.
6. Working tree stays unstaged/uncommitted until authorization; never fetch/pull/push
   without instruction; never destructive Git operations.

## 10. Slice 3.1 — Backend ingestion of normalized events [x] implemented (uncommitted; awaits review and commit authorization)

Owner-directed next-phase slice recorded here per instruction (2026-10-20). Files: `backend/ingest.py` (new), `tests/test_ingestion_service.py` (new). **No migration, schema change, dependency, config key, transport or API surface was added** — the slice lives entirely on the existing 0001/0002/0012 storage. Scope boundary: the adapter, Discord transport, relevance classification, proposal generation, review workflow and bucket-lifecycle orchestration stay in their later phases.

*Partial update 2026-10-21: the review corrections to this slice and the FR-ING-08 replay work are recorded in §11; the check battery below reflects the state at original slice delivery.*

### Owner decisions recorded for this slice (explicit approval, 2026-10-20)

- **D-1 = Option A**: an event from a Discord channel with no `source_group` row is stored after auto-registering the group with `vertical_id NULL`; the message is flagged UNMAPPED (FR-ING-09). A rejected event leaves no trace, including the auto-registration.
- **A1**: `event_timestamp` stored verbatim; format validation stays downstream (bucketing), never at ingestion.
- **A2**: when the adapter omits `received_at`, the backend stamps UTC receipt time (`datetime.now(UTC).isoformat()`); a supplied value is stored verbatim (FR-ING-03).
- **A3**: `message_mention.position` stored only as supplied (NULL when absent; never inferred).
- **A4**: `raw_metadata` text stored verbatim; Mapping values serialized deterministically (`json.dumps(..., sort_keys=True)`); other types rejected.

### Implemented behavior

- Typed normalized records `MessageEvent`/`MentionEvent`/`ReactionEvent`, `InvalidMessageEventError` (fail-loud, pre-write), `IngestionError` base class.
- `ingest_message(conn, event) -> StoreResult(message_id, created)`: one transaction covers group resolution/auto-registration (D-1), sender lookup (FR-ING-10), the `source_message` row and all embedded mention/reaction evidence — full atomicity (FR-ING-05, NFR-REL-04). Duplicate delivery (same `source` + `source_message_id`) returns the existing identity with `created=False` and writes nothing (FR-ING-04, NFR-CON-01). **Only** that UNIQUE conflict is converted to an idempotent result; every other integrity error re-raises unchanged.
- `ingest_reaction(conn, reaction, message_id) -> row_id`: later-arriving reaction evidence appended (FR-ING-07, BR-09 — evidence only, never authoritative state); an unknown message fails through the FK contract.
- Verbatim-storage contract: content (including phone-like text), reply/reference, timestamps and metadata are never masked or rewritten at ingestion (AC-21 first half; masking belongs at the pre-AI boundary). Newly stored rows are immediately consumable by the existing bucketing read path with no bucketing code changes.
- This slice contains the first-ever writers of `flag_unmapped` and `flag_unknown_sender`; `flag_late`/`flag_unclassified` remain unset everywhere (LATE: pending SRS amendments; UNCLASSIFIED: future relevance stage).

### Check battery (all actually run; exact outcomes)

1. `pytest tests/test_ingestion_service.py -q` RED first: `ModuleNotFoundError: No module named 'backend.ingest'` (intended reason) — then 38 tests pass after implementation.
2. Full `pytest`: **604 passed** (566 pre-existing + 38 new).
3. `ruff check . --no-cache`: clean after fixing one E501 pair and four style findings (UTC alias per py312 target, one unused test variable).
4. `mypy --no-incremental backend`: clean, 9 source files.
5. `git diff --check`: clean; tree holds only the two new untracked files.
6. Direct FK probe (fresh `:memory:` database, all twelve migrations, mixed auto-register/unknown-sender/mention/reaction/duplicate workload including a later `ingest_reaction`): `PRAGMA foreign_key_check` empty; duplicate idempotent (`created=False`, same id); auto-registered channel flagged UNMAPPED with the raw sender ID preserved. Every new test also asserts `PRAGMA foreign_key_check` emptiness (file-backed database).
7. `python -m backend --version`: `0.0.1`.

### Implementation notes and limitations

- A message with no Discord sender ID at all is stored with `sender_discord_user_id NULL` and flagged UNKNOWN_SENDER — no roster lookup is possible, so it falls under the same FR-ING-10 flag.
- Whole-event pre-write validation prevents reaching a DB failure after the message row is inserted; the all-or-nothing property itself is enforced by the single `transaction()` (existing, already-tested infrastructure).
- Uncommitted by design: staging/commit awaits read-only review and explicit owner authorization (§9 workflow).

## 11. Phase 2 completion record (2026-10-21 batch: review corrections, FR-ING-08 replay, integration readiness)

### 11.1 Review corrections to Slice 3.1 (all three findings resolved; tests written first)

1. **A1 — side-effect-free duplicates (FR-ING-04).** `ingest_message` now looks up `(source, source_message_id)` *before* resolving or registering the channel, so a duplicate delivery — including one naming a channel with no `source_group` row — is a pure no-op: no group registration, no evidence writes. The regression test `test_duplicate_delivery_with_unseen_channel_creates_no_side_effects` failed RED against the previous behavior (`assert 2 == 1` — phantom group row) and passed after the fix. The `UNIQUE`-conflict conversion remains as a defensive fallback only; foreign-key and other integrity errors still surface unchanged, and the two-connection concurrent-duplicate test is untouched.
2. **A2 — FR-ING-06 persistence assertion.** Direct test that a supplied reply/reference is stored byte-for-byte (no normalization, inference or rewrite).
3. **A3 — A4 text branch assertion.** Direct test that a valid, non-canonical JSON *string* survives verbatim; mapping inputs keep the deterministic `sort_keys=True` serialization and all existing rejections.

Refactor strictly in service of replay: the pre-checked, transaction-scoped insert path was extracted into `_persist_validated_message`, shared by live ingestion and replay; `ingest_message` remains `validate → one transaction → persist`. No behavioral change beyond A1.

### 11.2 FR-ING-08 — export/replay ingestion (`ingest_events`)

SRS FR-ING-08 ("ingest an exported chat history through the same path as live messages, for replay and testing only; exports that lack second-level timestamps, message IDs or reactions give indicative results only") is implemented as `backend.ingest.ingest_events(connection, events) -> tuple[RecordOutcome, ...]` (batch semantics updated 2026-10-21 per the §7 owner decision; see the bullets below):

- **Same path as live messages**: records reuse live-message validation, flag semantics, D-1 auto-registration, sender lookup and `(source, source_message_id)` idempotency by sharing one internal persistence core with `ingest_message`; replay introduces no store effects of its own ("for replay and testing only").
- **Partial-success batch (owner decision 2026-10-21, superseding the earlier all-or-nothing choice):** one ``transaction()`` per record — the live-path discipline (NFR-REL-04, the established `transaction()` pattern). Each record lands as CREATED, DUPLICATE or REJECTED in `RecordOutcome.index` order; a record-specific validation or constraint failure reports REJECTED and rolls back only that record (no trace, not even D-1 registration), so records before and after a rejected record stay durable; a non-record-specific failure aborts the batch and raises `IngestionBatchError` with the durable prefix in `.outcomes` and the original error chained — a systemic failure is never reported as successful durable ingestion. Retrying a partially successful batch is idempotent.
- **Idempotency across runs**: records already stored (live delivery or an earlier replay) and duplicates within one batch return `DUPLICATE` with the existing identity and without duplicating evidence (tested: repeated replay, live-then-replay overlap, in-batch duplicate, second-connection replay).
- **Indicative-quality exports**: records lacking second-level timestamps, message IDs or reactions stay as missing exactly where the store allows missing (e.g. `event_timestamp NULL` accepted; bucketing later reports it via its documented skip reasons — indicative only). Records lacking message IDs cannot enter the store (idempotency is impossible without identity); such records are REJECTED outcomes, never fabricated entries. Nothing is fabricated.
- **Outcome shape (2026-10-21)**: `ingest_events` returns `tuple[RecordOutcome, ...]` (`index`, `status`, `message_id` — `None` exactly for REJECTED — and `error`, where REJECTED carries the `record <index>: …` reason). `StoreResult` remains the `ingest_message` contract; `InvalidMessageEventError` remains the single-event failure. A systemic abort raises `IngestionBatchError` (`IngestionError` subclass) instead of returning any result.

Replay tests were written first and kept test-first through the semantics change: the original all-or-nothing run started RED (`ImportError: cannot import name 'ingest_events' from 'backend.ingest'`); the partial-success change ran RED again (`cannot import name 'IngestionBatchError' from 'backend.ingest'` — the two all-or-nothing tests were rewritten to carry the new owner decision, P-2). The 13-test replay suite covers: persistence order and verbatim missing-field handling; repeated replay with evidence uniqueness; live-overlap deduplication; in-batch duplicates; a rejected record straddled by two created records (including no channel registration for the rejected record and a non-empty `record <index>` reason); a second rejected record keeping the earlier one created; a record-scoped persistence constraint failure rolling back only that record (simulated, catch-outside-the-transaction boundary); a systemic failure (dropped table) aborting via `IngestionBatchError` with the durable prefix verified in the database; retry after partial success (DUPLICATE identities, REJECTED re-evaluated, corrected record created afterwards); outcome/`index` alignment incl. an in-batch duplicate; second-connection replay deduplication; empty and single-record batches. A direct FK/integrity probe additionally exercised a mixed live+replay workload: `PRAGMA foreign_key_check` clean through all twelve migrations.

### 11.3 Piggyback integration readiness (external dependency — not implemented in this repository)

Verified by inspection: there is **no Piggyback producer, adapter, transport or delivery path** in this repository; `ingest_message`/`ingest_events` are called only by their tests. README documents the architectural split ("the backend should receive ASEC events, not Discord objects"), shows a **Discord bot exists outside this repository** (connected to a test server; message/sender/channel/mention/reaction/edit detection ticked), while the Piggyback blocks (event schema, normalizer, event persistence, backend communication) are all unticked and the event schema is "still under development" — with sample payload fields (`sender.asec_user_id/role/vertical`) that conflict with the backend's authority model.

**Verified backend contract (what exists today, all test-anchored):**

- `MessageEvent(source, source_message_id, discord_channel_id, sender_discord_user_id?, event_timestamp?, received_at?, content?, reply_reference?, raw_metadata?(text|mapping), mentions, reactions)`; mentions/reactions carry opaque Discord user IDs only (NFR-SEC-04/EIR-03 discipline at the AI boundary; roster identity is never inside events).
- Validation is whole-event, pre-write and fail-loud (`InvalidMessageEventError`, no partial writes, no side effects); the backend stamps UTC `received_at` when the adapter omits it (A2).
- Idempotent duplicate no-ops keyed on `(source, source_message_id)` — the mechanism answering EIR-01's "tolerates duplicate, late and out-of-order delivery without loss" on the backend side; a redelivery must reuse the same message identity for this to hold.
- Channel resolution with D-1 auto-registration (vertical NULL → UNMAPPED flag, FR-ING-09); sender roster lookup by `person.discord_user_id` (UNKNOWN_SENDER flag, FR-ING-10); reply reference, content (including phone-like text) and metadata stored verbatim (AC-21 first half — masking belongs to the pre-AI boundary, not ingestion); reactions are evidence only (FR-ING-07, BR-09).
- Batch replay via `ingest_events` (§11.2). All writes run through `backend.db.connection` (`connect()`, WAL, FK ON, one `transaction()` per event/batch). Timestamps are stored verbatim (A1) and are only bucketable as ISO-8601 with offsets (Z normalized by `parse_event_time`).

**Unresolved external contract (must be reconciled before any transport work — §7 row added):** (a) production event schema vs `MessageEvent` (incl. whether payloads carry roster-derived identity — they must not be trusted; FR-ING-10 lookup stays backend-owned); (b) timestamp format; (c) mention-ID opacity; (d) reaction direction token vocabulary (backend stores verbatim, no CHECK-enforced vocabulary by design); (e) transport choice — in-process call, HTTP endpoint, file drop or bot-process bridge (**owner decision**; no endpoint/bot/worker exists here and none was built); (f) concurrency model and connection lifecycle (`connect()` per delivery vs held connection; WAL single-writer semantics with `busy_timeout` 5 s); (g) retry/delivery guarantees (identical message identity on redelivery); (h) the export format/mapping for FR-ING-08 replay inputs (adapter-side; the backend accepts only `MessageEvent`-shaped records).

**Integration checklist (single harness to prove end-to-end once the contract exists):** (1) schema reconciliation table — every adapter field → `MessageEvent` field or explicit drop; (2) a thin adapter shim `payload → MessageEvent` (pure mapping, no content interpretation); (3) round-trip harness: N sample payloads → shim → `ingest_message` on a file-backed DB → assert persisted row/flags/evidence; duplicate redelivery → `created=False`; malformed payload → loud failure with zero writes; (4) ingested rows consumed by the untouched `assign_messages`; (5) `PRAGMA foreign_key_check` clean; (6) failure-path review: do adapter retries redeliver identical identities?

### 11.4 Phase 2 requirements-completion matrix (verified status, 2026-10-21)

| Requirement | Status | Evidence / gap |
| --- | --- | --- |
| **FR-ING-01** receive via adapter producing normalized records | **Partial — backend side complete; producer/transport blocked externally** | in-repo: `ingest.py` contract + tests; external: no adapter in repo (§11.3; README bot is external) |
| **FR-ING-02** fields kept verbatim, traceability | **Complete** | verbatim round-trip tests: supplied fields, reply reference (11.1-A2), content incl. phone-like text (AC-21), mentions, reactions, metadata text/mapping |
| **FR-ING-03** receipt vs event timestamps kept separate; event time drives processing | **Complete** | distinct columns; A2 backend stamp when absent; `event_timestamp` consumed by bucketing downstream |
| **FR-ING-04** idempotent receipt | **Complete** | `UNIQUE` + pre-checked no-op duplicates — live, replay- and batch-duplicates, two-connection concurrency test, side-effect-free anomalies (11.1-A1) |
| **FR-ING-05** every valid message stored on receipt; failure does not lose or half-persist | **Complete at backend-function level** | pre-write validation + single-transaction atomicity + rollback tests; system-level proof requires the external adapter (§11.3) |
| **FR-ING-06** reply/reference preserved as supplied | **Complete** | 11.1-A2 direct assertion (previously a coverage gap) |
| **FR-ING-07** reactions stored as evidence only (add/remove; reactor, emoji, time) | **Complete** | embedded + later-arriving evidence (`ingest_reaction`); no state effects anywhere |
| **FR-ING-08** exported history through the same path, replay/testing only | **Complete in-repo** | `ingest_events` + 7 replay tests + §11.2; batch-failure semantics = documented vetoable decision; export format parsing is adapter-side (external) |
| **FR-ING-09** UNMAPPED when group has no vertical | **Complete** (D-1 A decided 2026-10-20) | flag follows exactly `vertical_id IS NULL`; auto-registration; no side effects on rejected/duplicated events |
| **FR-ING-10** UNKNOWN_SENDER flag, raw ID preserved | **Complete** | roster lookup by Discord ID; unmatched **and absent** sender IDs both flagged (author-noted reading, vetoable); raw ID always kept |
| **FR-ING-11/12** edits/deletions, attachments | **Not implemented — Post-MVP by SRS** | out of MVP scope (§8 boundary) |
| **NFR-CON-01** concurrent ingestion loses/duplicates/mis-assigns nothing | **Complete (ingestion scope)** | two-connection duplicate test; `BEGIN IMMEDIATE` write serialization; "mis-assign" belongs to bucketing, which is unchanged and green |
| **NFR-CON-02** simultaneous accepts/claims/assignments | **Not started — later phase** | review/task semantics; the `transaction()` discipline used here is the same pattern recorded for the future review service (§7) |
| **NFR-REL-01** stages safe to retry without duplicates | **Partial — ingestion-stage retry-safety complete; attempt recording deferred** | delivery retry with the same identity → `created=False` is the ingestion-stage aspect this matrix tracks; NFR-REL-01's "stages shall be recorded processing attempts" clause belongs to the pipeline stages (the 0009 attempt storage), not to receipt |
| **NFR-REL-03** failed stage leaves earlier results; manual re-queue recovers | **Complete (ingestion stage)** | committed events survive any later failure; batch rollback leaves zero partial rows; replay (`ingest_events`) is the re-queue at storage level; queue/worker tooling is external/absent |
| **NFR-REL-04** crash recovery without loss or duplicates | **Complete (ingestion stage)** | commit-atomic writes; uncommitted batches roll back whole; duplicates deduplicate; worker infrastructure is external |
| **BR-01** message is evidence, not an authorized decision | **Complete** | ingestion never creates/mutates proposals, tasks or authoritative state |
| **BR-02** author/group/vertical/decision-maker are different things | **Complete** | `source_group` vs `vertical` vs `person` kept distinct; UNMAPPED when vertical missing, never guessed |
| **BR-06** later message never erases earlier evidence | **Complete** | append-only ingestion; duplicates are no-ops, never updates |
| **BR-09** reaction is evidence, never acceptance | **Complete** | reactions never touch proposals/tasks (FR-ING-07) |
| **AC-10** relevant message from unknown sender stored and classified | **Partial — storage half complete** | stored + flagged UNKNOWN_SENDER; classification/relevance stages are the next phases |
| **AC-21** phone-like content: original unchanged (AI inputs masked) | **Partial — first half complete** | originals stored byte-for-byte (incl. phone-like text); masking is the pre-AI boundary (later phase) |
| **Piggyback integration (FR-ING-01 producer half, transport)** | **Blocked — external contract unavailable** | §11.3 + §7 row; no adapter/transport exists or may be invented (rule: no fabrication) |
| **Bucketing integration** | **Complete** | ingested rows consumed by the untouched `assign_messages` end-to-end test; no bucketing changes |

**Phase 2 conclusion (record):** every requirement implementable from inside this repository is implemented, verified and green; the phase is **incomplete overall only because FR-ING-01's producer half and the transport (plus the FR-ING-08 export format) are externally blocked** by the missing Piggyback contract (§7, §11.3). Nothing was fabricated to simulate integration. Staging/commit awaits explicit owner authorization.

## 12. Phase 3 record — bucket lifecycle and pipeline orchestration (2026-10-21 batch, uncommitted, awaits review and owner commit authorization)

Owner-directed Phase 3 delivered as one coordinated batch (single-instruction run; no intermediate approval cycles). Files: `backend/bucket_lifecycle.py` (new), `backend/orchestration.py` (new), `tests/test_bucket_lifecycle.py` (new, 30 tests), `tests/test_orchestration_service.py` (new, 12 tests). **No migration, no schema change, no new dependency, no config key, no endpoint** — the bucket table already carries the full FR-BKT-12 vocabulary CHECK (0003, committed), and all lifecycle/orchestration state is service-level over 0003/0004/0010/0012.

### Implemented behavior

- **Lifecycle service (FR-BKT-12).** `bucket_processing_state(conn, bucket_id) -> BucketState` (fail-loud on unknown ids) and `transition_bucket_processing_state(conn, bucket_id, target) -> bool` — True = applied, False = idempotent no-op (retry safety, NFR-REL-01). Legal edges and their SRS anchors are in the module docstring and §7 (owner-vetoable row): CLOSED → PROCESSED / FAILED / AWAITING_RECOVERY (FR-REL-01: the classifier runs on each closed bucket), FAILED → AWAITING_RECOVERY (NFR-REL-03 re-queue), AWAITING_RECOVERY → PROCESSED / FAILED (NFR-REL-05, AC-13 resume). Same-state calls are no-ops; PROCESSED is terminal; no edge ever reopens a CLOSED bucket or calls OPEN → CLOSED (FR-BKT-05/08; closure belongs exclusively to accepted progress). Illegal edges, unknown buckets and non-enum targets fail loud with nothing written; every transition re-reads state and updates with a rowcount guard inside one `transaction()` (BEGIN IMMEDIATE, single writer at a time, FR-BKT-07 discipline).
- **Orchestration pass.** `run_bucketing_pass(conn, params=None, group_id=None)` is the sanctioned one-call formation pass over ingested messages via the untouched `assign_messages` (all groups or one). It never closes a bucket and never writes progress — FR-BKT-05 authority (and its pending "progress authority" amendment, §7) is untouched; `mark_stream_progress` remains the only closure path.
- **Backlog visibility (NFR-REL-06, FR-UI-05 data half).** `pipeline_backlog(conn) -> PipelineBacklog` reports per group: unassigned message count (messages bucketing must skip stay visible — never silent loss), per-state bucket counts over all five FR-BKT-12 tokens (zero counts included), unprocessed count (every bucket not PROCESSED, incl. FAILED/AWAITING_RECOVERY), the oldest unprocessed bucket id with its raw anchor (end else start event timestamp) and an event-time `age_event_seconds` = accepted progress − anchor when both parse and the anchor is not ahead of accepted progress (a not-yet-meaningful age is reported as None, never negative). Age is expressed in the event-time domain (the domain buckets live in); no wall-clock value is derived. Undatable anchors (missing/unparsable) rank as the oldest unresolvable backlog with age None; ranking is parse-based, never lexicographic (verbatim timestamps are free text, DR-04).
- **Classification hand-off queue (§4.1, FR-REL-01, NFR-REL-03/05).** `processing_queue(conn) -> ProcessingQueue` reads, orders by bucket id: `never_started` (CLOSED with zero 0004 `processing_attempt` rows), `awaiting_recovery` (AWAITING_RECOVERY — resume after outage, NFR-REL-05/AC-13), `failed` (FAILED awaiting the manual re-queue, NFR-REL-03). Attempt-row **creation and execution are the relevance phase's work**; this module only exposes the queues. **Coverage note: this is a handoff queue, not a complete inventory of all outstanding work** — a CLOSED bucket whose existing `processing_attempt` rows all failed with FAILED_RETRYABLE appears in none of the three queues while still representing outstanding work. Assessing outstanding work therefore combines the queue, the buckets' lifecycle states and the `pipeline_backlog` diagnostics; wiring processing-attempt outcomes into lifecycle states belongs to the downstream relevance/processing phase.
- **Recovery/restart shape.** No dedicated code path: everything observed is committed in SQLite; a fresh `connect()` + pass loses and duplicates nothing (tested end-to-end incl. lifecycle states and reopen). Post-recovery UNCLASSIFIED-flag clearing stays a recorded deferred owner decision (§7) and is not implemented here.

### Interfaces delivered for later phases (no premature coupling)

Relevance/classification phase: consume `processing_queue`, create its own `processing_attempt` rows, and call `transition_bucket_processing_state` on outcomes. Admin/API phase (FR-UI-05): `pipeline_backlog` is the authoritative backlog data source; the API layer should expose it without duplicating the queries. Formation/progress ownership unchanged — `assign_messages`/`mark_stream_progress` keep their committed contracts.

### Check battery (all actually run; exact outcomes)

1. New tests: 30 lifecycle + 12 orchestration = 42. Implementation and tests were co-developed inside this single owner-directed batch (the batch was not permitted to pause for a separate RED demonstration; the correction pass below was test-first: the mixed-format/undatable-anchor defect was demonstrated in review, fixed, then pinned by `test_undatable_anchor_is_oldest_unprocessed`).
2. Full `pytest -q` (JUnit XML observed): **659 tests, 0 failures, 0 errors, 0 skipped** (566 data-layer baseline + 51 Slice 3.1 ingestion + 42 Phase 3; net Phase 3 = +42: +1 undatable-anchor test, −1 duplicate parametrize entry removed in review).
3. `ruff check .`: clean (two style findings fixed after first run: `Mapping` import source, import sort order).
4. `mypy`: clean, 11 source files (9 prior + `ingest` was counted in the 9-file battery at Slice 3.1; now 9 + lifecycle + orchestration = 11).
5. FK/integrity probe (`%TEMP%\opencode\fk_probe_phase3.py`, fresh file DB, all twelve migrations, full workload: D-1 auto-registration, skipped message, pass + retry, accepted progress closures in two groups, PROCESSED transition, AWAITING_RECOVERY/FAILED/re-queue chain, duplicate delivery no-op): `PRAGMA foreign_key_check` empty, `PRAGMA integrity_check` `ok`, state persists across reopen; backlog/queue outputs verified step-by-step (event-time age 65 s = progress 10:03:20 − anchor 10:02:15).
6. `python -m backend --version`: `0.0.1`.

### Review findings and correction pass (one consolidated review of the whole batch)

- F-1 `parse_event_time` raises `_TimestampError`, not `ValueError` — the backlog age guard caught the wrong exception and would crash on malformed anchors. Fixed (import of the bucketing error type; the single source of truth for event-time parsing stays bucketing).
- F-2 anchor ordering was lexicographic over verbatim text — not chronological for mixed formats. Fixed: parse-based ranking in Python, undatable anchors first, documented in `_oldest_unprocessed`.
- F-3 lifecycle UPDATE had no rowcount guard — zero-row updates could theoretically report success. Fixed: fail-loud guard inside the transaction (defense in depth, matching `ingest`'s UNIQUE-fallback pattern).
- F-4 duplicate parametrize entry in lifecycle tests removed (net test count unchanged).

### Boundaries and status

- Relevance classification execution, attempt creation, proposal generation, review, emissions and UI remain later phases (§8). Bucket formation/closure/progress semantics were **not rewritten** — zero changes to `backend/bucketing.py`.
- Phase-record status: the FR-BKT-12 lifecycle beyond OPEN/CLOSED, NFR-REL-06 backlog visibility and the formation pass are **implemented, reviewed, and green in-repo**; execution-level orchestration (scheduling, workers, transport) and the UNCLASSIFIED clearing decision remain open per §7/§8. Nothing staged, nothing committed; the tree carries the Phase 2 batch (§10–§11) plus this batch, all unstaged.

## 13. Phase 4 record — relevance classification (2026-10-21 batch, uncommitted, awaits review and owner commit authorization)

One coordinated batch implementing the approved Phase 4 plan: the relevance-classification service with its classifier boundary, persistence, retry/fallback and lifecycle wiring, plus the regression suite. No bucketing, ingestion, lifecycle-rule or migration changes; Phase 2/3 contracts are consumed, never rewritten. In-repo requirements are implemented and green; see §13.9 for what remains genuinely external or deferred.

### 13.1 Requirement traceability (docs/SRS.md)

| ID | Requirement | Implementation | Tests (tests/test_relevance_service.py) |
|---|---|---|---|
| FR-REL-01 (§3.3) | Classify every closed bucket, whole bucket as context, one decision per message incl. volunteers/unknown senders | `_bucket_request` builds the whole-bucket request; discovery takes CLOSED/AWAITING_RECOVERY/queue sets only; `_process_bucket` | `test_request_carries_whole_bucket_context`, `test_never_started_closed_bucket_is_classified`, `test_open_bucket_is_never_attempted`, `test_unknown_sender_carries_absent_sender_information` |
| FR-REL-02 | Only RELEVANT/IRRELEVANT; no UNCERTAIN | `DECISION_VOCABULARY` + per-decision vocabulary check in `_validated_response`, CHECK-enforced at store | `test_vocabulary_violation_rejected`, `test_lowercase_decision_is_not_the_accepted_vocabulary`, `test_vocabulary_constant_is_exact` |
| FR-REL-03 | Doubt → RELEVANT | Policy carried through the request contract (`CLASSIFICATION_POLICY`); semantic compliance is explicitly *not* claimable by schema validation — documented boundary (AI-05/AI-06 evaluation) | `test_policy_contract_carried_on_every_request`, `test_policy_carried_on_fallback_request_too` |
| FR-REL-04 | Per-message independence | Decisions are per-message rows over the whole coverage set; no aggregate state; nothing links messages' outcomes | `test_valid_output_persists_provenance_and_timestamps` (mixed RELEVANT/IRRELEVANT accepted), `test_ac10_volunteer_and_unknown_messages_classification_only` |
| FR-REL-05 | Decision records message, bucket, attempt, model+version, prompt/config version, output, timestamp | `relevance_decision` rows with copied provenance and backend-stamped `decided_at` (0004 FKs pin attempt/bucket/membership) | `test_valid_output_persists_provenance_and_timestamps` |
| FR-REL-06 | Decisions immutable; correction separate (interface Post-MVP) | Insert-only writes; never updated or deleted; `relevance_correction` untouched (effective-selection stays AU-3) | `test_decision_rows_are_attempt_scoped_and_prior_attempt_intact`, `test_succeeded_bucket_is_never_reprocessed` |
| FR-REL-07 | Failure never IRRELEVANT; UNCLASSIFIED + retry + optional fallback | All execution failures → `FAILED_RETRYABLE`, zero decision rows, `flag_unclassified = 1` (column from 0002); retry via next pass; fallback after primary failure | `test_invalid_output_failure_marks_messages_unclassified`, `test_missing_message_in_output_rejected_whole`, `test_duplicate_message_in_output_rejected`, `test_foreign_message_id_rejected`, `test_missing_provenance_rejected`, `test_blank_model_version_rejected`, `test_classifier_failure_with_raw_exception_is_retryable`, `test_invalid_output_failure_marks_messages_unclassified` |
| EIR-03 (§5) | Stateless request/response; masking before every call; complete-output schema-check; provenance validated by backend; AI layer never writes the DB | `RelevanceClassifier` Protocol + `RelevanceRequest/RelevanceResponse`; `mask_pii` on content in every request build; `_validated_response` full-coverage check; adapter has no DB handle by construction | `test_masking_applied_before_every_invocation`, `test_masking_applied_before_fallback_invocation`, `test_mention_handles_stay_opaque`, `test_vocabulary_violation_rejected`, `test_missing_provenance_rejected`, `test_from_empty_bucket_is_a_valid_no_message_pass` |
| NFR-REL-01 (§6.1) | Stages recorded as attempts; retry-safe, no duplicates | Attempt row per execution; `(attempt_id, message_id)` uniqueness; budget re-checked inside the transaction | `test_retryable_failed_gap_bucket_is_retried`, `test_succeeded_bucket_is_never_reprocessed`, `test_pass_is_idempotent_for_processed_and_open_states` |
| NFR-REL-02 | Only execution failures retry; quality errors do not | Nothing classifies quality automatically; every recorded failure is an execution failure; document nothing else exists | (narrative; no automatic quality retry path exists to test — see 13.8) |
| NFR-REL-03 | Failed stage never erases earlier results; permanent failure recoverable by manual re-queue | Append-only attempt history; budget exhaustion → FAILED; `requeue_failed_bucket` FAILED→AWAITING_RECOVERY only | `test_decision_rows_are_attempt_scoped_and_prior_attempt_intact`, `test_retry_budget_exhaustion_transitions_failed`, `test_manual_requeue_moves_failed_to_awaiting`, `test_failed_bucket_is_not_auto_attempted` |
| NFR-REL-04 | Crash recoverable without loss or duplicates | Pre-write attempt design (crash mid-call records nothing); crash between commit and transition converged by the next pass (`_finalize_candidates`) | `test_untransitioned_success_converges_to_processed`, `test_interrupt_during_call_writes_nothing`, `test_partial_success_persistence_rolls_back_the_whole_attempt` |
| NFR-REL-05 | Pipeline keeps working during AI outage; messages processed after recovery without re-entry | AWAITING_RECOVERY pause + automatic resume of AWAITING buckets | `test_awaiting_recovery_bucket_resumes_after_outage`, `test_outage_recovery_full_cycle_ac13` |
| NFR-REL-07 | Fallback only after primary fails; both attempts recorded; effective = successful | Primary stage / fallback stage tokens; strict sequencing in `_process_bucket` | `test_primary_fails_fallback_succeeds_both_recorded`, `test_both_fail_within_budget_marks_awaiting_recovery`, `test_fallback_budget_respected_inside_one_pass` |
| NFR-AUD-02 (§6.2) | Model+version, prompt/config version, masked-input reconstructability | Provenance on attempt + every decision; masked request content deterministic from stored sources (rebuildable; identical replay output not required) | `test_valid_output_persists_provenance_and_timestamps`, `test_masking_applied_before_every_invocation` |
| NFR-PERF-02 (§6.4) | Classifier latency recorded | `CLASSIFIER_LATENCY` measurement row, subject = successful attempt, in the same transaction | `test_classified_attempt_records_latency`, `test_failed_attempt_records_no_latency` |
| NFR-SEC-04 (§6.5) | Mask phones/emails/gov-IDs before every call (roll no. as project safeguard) | `backend.masking.mask_pii` applied in `_bucket_request` for every call, primary and fallback | the two masking tests |
| AC-10 | Volunteer/unknown messages stored and classified, no proposal/notification | Classification only; Phase 5 tables untouched | `test_ac10_volunteer_and_unknown_messages_classification_only` |
| AC-12 | Failure marked failure, source kept, retry no duplicates | Whole suite around validation + retention | `test_ac12_failure_never_irrelevant_and_retry_creates_no_duplicates`, `test_failure_keeps_source_content_verbatim` |
| AC-13 | Outage messages flagged UNCLASSIFIED, processed after recovery, none lost, none marked irrelevant | Flag set on failure; recovery passes classify; **flag clearing deferred (see 13.6)** | `test_outage_recovery_full_cycle_ac13`, `test_invalid_output_failure_marks_messages_unclassified` |
| AI-01..04 (§7) | AI for relevance only, logical separation; stateless; advisory-until-validated; per-function evaluation | This module is the relevance function only; injected stateless boundary; backend validation gates state | `test_request_carries_whole_bucket_context`, protocol structure, validation suite |

### 13.2 Interfaces introduced

- `backend/relevance.py` (new): `DECISION_VOCABULARY`, `STAGE_RELEVANCE_CLASSIFICATION`, `STAGE_RELEVANCE_FALLBACK_CLASSIFICATION`, `LATENCY_METRIC_LABEL`, `DEFAULT_MAX_ATTEMPTS_PER_BUCKET`, `CLASSIFICATION_POLICY`; `RelevanceServiceError` / `UnknownBucketError` / `ClassifierContractError` / `RelevancePassError` (committed-prefix carrier, mirroring `IngestionBatchError`); `ContextMessage`, `RelevanceRequest`, `MessageDecision`, `RelevanceResponse`; `RelevanceClassifier` Protocol; `RelevanceOutcome`, `BucketOutcome`, `RelevancePassResult`; `requeue_failed_bucket`, `run_relevance_pass`.
- `tests/test_relevance_service.py` (new): 52 tests with deterministic scripted classifier doubles.
- Migrations: **none** — `flag_unclassified` already exists (0002:16); 0004/0011 storage contracts reused exactly as committed.

### 13.3 Attempt, retry, fallback and lifecycle semantics (implemented; owner-vetoable choices marked)

- Attempt rows carry `started_at`/`finished_at` (backend-stamped UTC, A2 pattern), outcome, and validated provenance. **All automatic execution failures are `FAILED_RETRYABLE`; `FAILED_PERMANENT` is reserved and never written by this phase** (owner-vetoable: exhaustion is accumulation policy at bucket level, so a failed attempt's own outcome does not change).
- Fallback (`RELEVANCE_FALLBACK_CLASSIFICATION`) runs only after a primary failure; both attempts stay recorded; the fallback's success supplies the persisted decisions.
- Retry budget `max_attempts_per_bucket` (default **3**, autonomous choice): each would-be attempt re-checks the committed count inside its `BEGIN IMMEDIATE` transaction; a bucket that reaches the budget while outstanding is transitioned FAILED by the pass (CLOSED→FAILED or AWAITING_RECOVERY→FAILED, both legal edges); manual re-queue is the only path back.
- Success advances CLOSED (or AWAITING_RECOVERY) → PROCESSED only after the attempt+decisions+latency transaction committed. Transitions use `backend.bucket_lifecycle` exclusively; same-state no-ops are tolerated.
- Crash convergence: buckets with a SUCCEEDED attempt but stale CLOSED/AWAITING state are finalized to PROCESSED by the next pass with no new attempts (NFR-REL-04).
- Quality errors (NFR-REL-02) never enter this machinery: the backend has neither judge nor retry for them; a human correction route is FR-PM-05 (Post-MVP) — the attempt history makes such reviews possible without modifying decisions.

### 13.4 Queue coverage behavior

Discovery is the union of `processing_queue`'s `never_started` and `awaiting_recovery` plus the §12 gap — CLOSED buckets with only-`FAILED_RETRYABLE` attempts and no success — realized now by `_discovery_targets` (§12's coverage note is implemented-faithful, not abandoned). FAILED buckets are never auto-attempted; OPEN buckets are unreachable. Ordering is ascending bucket id; repeated passes converge (no new attempts on completed work). `processing_queue` itself is unchanged.

### 13.5 Masking and provenance guarantees

- `mask_pii` (phones incl. international, emails, government-ID shapes, roll numbers) is applied to message content in every request build — no adapter call path bypasses `_bucket_request`, verified for both primary and fallback calls. Originals stay verbatim in the store (DR-03).
- Mention handles are passed opaque (Discord snowflakes), unmapped and unmasked at this boundary; the backend maps them only from stored mention rows (EIR-03).
- Response provenance is validated before write (non-blank, trimmed); attempt provenance is stored only for fully validated responses, and `config_version` carries the validated prompt/config version — the only configuration identity in the contract.
- `raw_metadata` is excluded from the request by EIR-03's contract list (adapter envelope data, not message content).

### 13.6 Deferred UNCLASSIFIED clearing — explicit AC-13 acceptance gap (recorded, not silently resolved)

On any failed classification the bucket's messages get `flag_unclassified = 1` (FR-REL-07). Recovery processing works: the next pass reclassifies AWAITING_RECOVERY buckets and none are lost (AC-13's operative semantics; pinned by `test_outage_recovery_full_cycle_ac13`). However, **clearing the flag after a successful classification is deferred by standing owner decision** (§8/§9 standing boundaries), so a message classified after recovery keeps `flag_unclassified = 1` until that policy exists. This is the exact reconciliation gap with AC-13's "processed after recovery" presentation semantics: *processing* is implemented and tested; *flag clearing* is deferred. No clearing code was written, and no new column/migration was introduced.

### 13.7 Decisions resolved autonomously (all recorded here for veto)

1. Stage tokens as above (free text; SRS defines none).
2. All automatic failures → `FAILED_RETRYABLE`; `FAILED_PERMANENT` reserved (owner may direct exhaustion→`FAILED_PERMANENT` later).
3. Budget default 3 attempt rows per bucket; refused retry leaves the bucket FAILED.
4. Systemic (non-bucket-specific) failures abort the pass loudly: `RelevancePassError` carries the committed per-bucket prefix and finalized ids with the original chained — mirrored from the P-2 ingest decision, never a reported success.
5. `CLASSIFIER_LATENCY` metric label; latency recorded for successful attempts only (a failed attempt never ran a completed classifier call).
6. Empty-member buckets: vacuous coverage is a valid successful attempt (zero decisions) — missing staged as missing.
7. Backend-stamped UTC timestamps for `started_at`/`finished_at`/`decided_at`.
8. Adapter exceptions are text-logged into `processing_attempt.error` as `f"{type.__name__}: {message}"`; interrupts propagate raw with nothing written.

### 13.8 External model integration checklist (blocked externally, like §11.3 for Piggyback)

The in-repo boundary is complete; production classification needs, from the owner side:

1. A deployed relevance model reachable behind a stateless `RelevanceClassifier` adapter (one bucket in; validated decisions + provenance out; **no DB access from the adapter**).
2. A prompt/config version registry feeding `prompt_version` (goes verbatim into attempt `config_version` and every `relevance_decision.prompt_version`).
3. The AI-05 labelled corpus (real Hinglish/romanized-Hindi festival messages with hard negatives per the §7 list) and the AI-06 candidate comparison — both owner-sourced; the relevance targets table (recall ≥ 95%, precision ≥ 80%) is then evaluated per AI-04/AI-06 and NFR-PERF-01/NFR-PERF-02/NFR-PERF-03. Until then 'policy compliance' (FR-REL-03) is contract-carried and review-checked, never schema-proven.
4. Error taxonomy from the real client: timeouts/availability map onto the same `FAILED_RETRYABLE` + budget path (no code change expected); a dedicated degraded-model fallback adapter slots into `run_relevance_pass(fallback=…)` unchanged.

### 13.9 Phase-record status

- Implemented, reviewed, green in-repo: the whole relevance-classification function over closed buckets (FR-REL-01..07, EIR-03, NFR-REL-01..05/07, NFR-AUD-02, NFR-PERF-02 recording, NFR-SEC-04 at the boundary, AI-01..04 function-level; AC-10/AC-12 in full, AC-13 processing semantics with the documented 13.6 flag-clearing gap).
- Not implemented here (per standing boundaries): proposal generation (FR-PRP-01 gates Phase 5), notifications/dashboards, review service, Part B D1–D7, Piggyback transport, progress-scheduling authority, post-recovery UNCLASSIFIED clearing, effective-correction selection, AI-05/AI-06 production evaluation gates (external: owner corpus/model access).
- Verification: `pytest` full suite 729 tests / 0 failures / 0 errors / 0 skips (JUnit XML; baseline 666 + 63 in `tests/test_relevance_service.py` after the §13.10 correction pass); `ruff check .` clean; `mypy` clean (12 source files); `python -m backend --version` → 0.0.1; `git diff --check` clean; Temp integrity probe (`relevance_probe_phase4.py`, rerun after the correction pass): end-to-end ingest → bucket → close → classify → PROCESSED on a real file DB (2 buckets / 2 attempts / 2 decisions), `integrity_check` ok, `foreign_key_check` empty, probe artifact removed. Migration suites unchanged and green. Final Phase-4 numbers after the owner-approved F3/F4 corrective batch (§13.11, audit correction pass included): 755 tests / 0 failures / 0 errors / 0 skips; ruff clean; mypy clean (12 source files); version 0.0.1; `git diff --check` clean; probe re-run PASS with the F3/F4 legs, `integrity_check` ok, `foreign_key_check` empty.

### 13.10 Consolidated audit correction pass (2026-10-21)

An independent read-only audit of `backend/relevance.py` + `tests/test_relevance_service.py` returned **PASS WITH NON-BLOCKING FINDINGS** (all five explicit checks confirmed: no IRRELEVANT from failure; no erasure/overwrite of prior attempts; only legal lifecycle edges; masking before every adapter call path; no nested `transaction()`). The single consolidated correction pass, tests first, resolved the in-scope findings:

1. **Race guard, first writer wins (NFR-REL-01/04):** the attempt transaction now refuses a second `SUCCEEDED` result for a bucket whose committed attempts already hold one (`_has_success` check inside the serialized write window); the competing pass contributes no attempt, no decisions, no outcome. Failure convergence likewise never marks FAILED over a committed success (`_converge_after_failure` returns a skip when a success exists — the finalize convergence advances that bucket). Tested with a second-connection competitor committing inside `classify()` (both the success-refused and fail-over-success halves). Concurrency remains unreachable in the sequential MVP; this is defense in depth, not scheduler behavior.
2. **Stage-scoped budget:** `_attempt_count` counts only the two relevance stage tokens — a later phase writing the same attempts table cannot silently consume relevance retries. Forward-compat note, autonomously chosen; owner may veto toward a bucket-wide budget.
3. **Budget-argument validation:** `max_attempts_per_bucket` must be a positive integer; anything else raises `ValueError` before any state change (a caller typo must not mass-fail the backlog).
4. **Error-text redaction (NFR-SEC-04 reach):** stored `processing_attempt.error` is masked (`mask_pii`) and capped (`MAX_ATTEMPT_ERROR_LENGTH = 500`, ellipsis-marked) — the error channel is a review surface, and an adapter's own message may embed personal data.
5. **Latency semantics (NFR-PERF-02):** `CLASSIFIER_LATENCY` now measures the adapter call alone (monotonic window around `classify`), excluding backend validation time.
6. **Test-adequacy repairs from the audit:** real assertions replacing a vacuous one (processed bucket keeps exactly its attempt; OPEN bucket untouched and attempt-less); a regression guard that a later *successful* classification still never clears `flag_unclassified` (§13.6 gap stays closed until an owner decision); loud-rejection tests for `requeue_failed_bucket` on CLOSED/PROCESSED; characterization tests for structural rejection branches (non-iterable decisions, items without contract attributes), `SystemExit` propagation, and an interrupt landing inside the persistence transaction (whole in-flight attempt rolls back).

Recorded as owner-vetoable open items, no code invented: (a) NFR-AUD-02 reconstruction currently relies on un-versioned masking ruleset/policy text — persisting a ruleset version or request hash touches attempt-row semantics and is deferred to the owner; (b) a CLOSED bucket holding only `FAILED_PERMANENT` attempts would be invisible to both queue and gap discovery — unreachable through this module (it never writes `FAILED_PERMANENT`), documented for when such attempts exist. **Both resolved by the owner-approved F3/F4 corrective batch (§13.11): (a) via the self-contained `relevance_attempt_input` artifact, (b) via terminal-failure convergence.**

### 13.11 F3/F4 corrective batch (2026-10-21, owner-approved with three refinements)

Owner-approved resolution of §13.10(a)/(b): (1) durable canonical masked-request snapshot per committed adapter-executing attempt; (2) explicit discoverability and convergence of terminal-failure buckets, retryable-dominates mixed policy, never auto-retry permanent-only failures; (3) validated per-bucket budget overrides so authorized recovery can actually execute new attempts, exhaustion reported honestly.

Implementation:

- **F3 — snapshot:** migration `0013_relevance_attempt_input.sql` (additive; attempt_id UNIQUE FK→processing_attempt ON DELETE RESTRICT; version columns; canonical `masked_request`; `request_sha256`; `captured_at`). `_serialize_masked_request`/`_request_sha256` in relevance.py; the artifact is written inside the single attempt transaction, so "snapshot iff commit-accepted attempt row". The artifact is self-contained: it equals the exact `RelevanceRequest` the adapter received, so a later masking-ruleset edit changes what a *new* attempt sends, never what an old attempt stored — a version string alone was rejected as insufficient (owner criterion). New lineage constants `MASKING_RULESET_VERSION = "masking-rules-v1"`, `CLASSIFICATION_POLICY_VERSION = "classification-policy-v1"` identify the ruleset/policy that produced each snapshot's masking (bump = owner-approved change; they do not gate reconstruction — the artifact does). Tests: canonical round-trip `test_snapshot_persists_canonical_masked_input_on_success/_on_failure/_for_primary_and_fallback_attempts`, raw-PII exclusion `test_no_unmasked_sensitive_input_in_snapshot` (phone/email/Aadhaar/PAN/roll shapes masked; artifact equals what the adapter saw), rules-immunity `test_snapshot_is_stable_when_masking_rules_change_later`, invariant `test_snapshot_absent_without_an_attempt_row`, empty-bucket artifact `test_snapshot_written_for_empty_members_bucket`. Schema: `tests/test_migrations_relevance_attempt_input.py`.
- **F4 — terminal convergence:** public read `stalled_terminal_buckets` (data-only predicate); the pass converges exactly those buckets to FAILED via the existing legal CLOSED→FAILED edge and reports them in `RelevancePassResult.terminal_failed` (new default-empty field; also carried by `RelevancePassError`). The writer stays two-valued — no outcome-taxonomy invention.
- **F4 — budget overrides / recovery:** `run_relevance_pass(..., budget_overrides: Mapping[int, int] | None)`; every key and value is validated before any state change (keys: integer non-bool bucket ids; values: positive non-bool ints — invalid entries raise ValueError with zero rows written, audit correction pass); an override is the bucket's *raised total attempt cap* for that pass. Requeued exhausted work without a matching override is refused pre-adapter-call and reported EXHAUSTED→FAILED (honest, zero rows). History append-only; prior attempt rows byte-identical after recovery.

Autonomous decisions recorded for veto:

1. Snapshot scope: **every** committed adapter-executing attempt — including `FAILED_RETRYABLE` executions (their error strings are debuggable only against the input) — not successes only. Refused (budget) and superseded paths create neither attempt nor snapshot; interruptions roll the whole transaction back, snapshot included.
2. Terminal sweep is **CLOSED-only** (refinement-letter deviation, verified against the recovery path): converging an AWAITING_RECOVERY-only-permanent bucket would make manual recovery structurally impossible — the probe demonstrated a requeued terminal bucket being swept back to FAILED inside the very next pass before any recovery attempt. In this system AWAITING+only-permanent has exactly one producer — an authorized manual requeue — so that state is owner-authorized recovery work, processed through the ordinary recovery queue (ordinary budget; the override matters for retryable-exhausted buckets, whose accumulated count exceeds the default). Note the boundary the audit pinned: a requeued bucket whose accumulated relevance-stage attempts already meet the ordinary cap is refused EXHAUSTED→FAILED (exhaustion-reported, zero rows) and needs an override to recover, regardless of outcome mix — reachable for permanent-only work only via seeded/external `FAILED_PERMANENT` writes, since this service never produces them (§13.10(a) disposition). The CLOSED sweep remains: such buckets are never auto-retried and gain the one state NFR-REL-03 acts on.
3. Mixed-outcome policy: retryable-dominates (explicitly pinned by `test_mixed_retryable_and_permanent_stay_retriable`).
4. No read-API for snapshots in this slice (SQL-direct; a review-surface accessor is future work); no backfill (pre-0013 attempts — none in production — carry no artifact, recorded as a compatibility note).

Acceptance mapping: masking-rule-immutability → stability test; exact input+versions → round-trip tests; primary/fallback distinguishability → per-attempt artifacts; no unmasked input → PII test; retry/immutability/duplicate-prevention intact → full suite; permanent-only discoverable+converged+not-retried → `test_terminal_only_closed_bucket_converges_to_failed`; mixed → `test_mixed_retryable_and_permanent_stay_retriable`; prior-success-with-failures → `test_prior_success_excluded_from_terminal_convergence`; exhaustion+honest recovery → `test_recovery_without_override_reports_exhaustion_honestly` / `test_scoped_budget_override_lets_requeued_bucket_execute`; repeated recovery idempotent → `test_repeated_recovery_pass_is_idempotent`; worker interruption during recovery → `test_interrupt_during_recovery_writes_nothing_and_recovers`; discoverability pre/post → `stalled_terminal_buckets` asserts.

Verification (pre-audit): tests-first RED observed — collection failed with `ImportError: cannot import name 'CLASSIFICATION_POLICY_VERSION' from 'backend.relevance'` (and the new public function/fields unimportable) before implementation; focused suites green after implementation (test_relevance_service + test_migrations_relevance_attempt_input); full battery 754 tests / 0 failures / 0 errors / 0 skips after the migration-count baseline bump (`test_migrations_resolution.py:184`, the established convention); ruff clean (2 unused-variable findings fixed); mypy clean (12 files); version 0.0.1; `git diff --check` clean; Temp integrity probe extended with F3 artifact assertions (hash/versions/no-raw-PII) and an F4 leg (terminal convergence, `processing_queue.failed` visibility, requeue+override recovery, invalid-override ValueError) — PASS, `integrity_check` ok, `foreign_key_check` empty, artifacts self-cleaned.

Independent audit (read-only background subagent, post-implementation): **PASS WITH NON-BLOCKING FINDINGS**, all six mandated checks CONFIRMED — snapshot-iff-committed-attempt across success/exception/contract-error/primary+fallback/budget-refused/superseded/interrupt paths; artifact fidelity (the frozen request sent is exactly what is stored); sweep never fires on retryable-dominant, succeeded or AWAITING buckets and every transition stays within the committed lifecycle edges; per-bucket override scoping with append-only history and first-writer-wins intact; no nested `transaction()`; exhaustion without an override reported honestly (EXHAUSTED→FAILED, zero rows). The CLOSED-only sweep deviation was independently assessed SOUND (no undiscoverable state; the literal CLOSED/AWAITING wording would create a requeue→sweep→requeue loop). Working tree unchanged by the audit.

Consolidated correction pass on the audit's three non-blocking findings: (1) §13.11 precision note that a requeued bucket whose accumulated attempts already meet the ordinary cap is refused EXHAUSTED→FAILED and needs an override regardless of outcome mix (decision 2 above); (2) migration 0013 header note that sender display names / Discord user ids persist unmasked per the NFR-SEC-04 field list, exactly as the adapter received them; (3) new tests-first behavior: non-int `budget_overrides` keys now raise `ValueError` before any state change (`test_budget_override_key_validation_fails_loud` — RED observed: `DID NOT RAISE ValueError`; then implemented in the same validation loop). Re-verification after the corrections, all observed: focused suites 89 tests / 0 failures / 0 errors / 0 skips; full battery 755 / 0 / 0 / 0 (JUnit XML); ruff clean; mypy clean (12 source files); `python -m backend --version` → 0.0.1; `git diff --check` clean; probe re-run after the module change — PASS with `integrity_check` ok, `foreign_key_check` empty, artifacts self-cleaned.
