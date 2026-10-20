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

## 2. Current status (data layer complete at `4e4aae2`; ingestion, AI orchestration, services, authorization, API, notification delivery and app are not started)

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
| Deferred 2.3/2.4-era decisions | role vocabulary, unknown-channel ingestion policy, post-recovery UNCLASSIFIED clearing, `parameters_used` scope | future slices owning those behaviors |
| **`proposal_decision` outcome history (0012)** | **Owner decision recorded 2026-10-20 (documentation only — no schema, code, or test change).** Store **only the latest review decision per proposal**; do not preserve a history of earlier decisions. Already supported by the existing schema, nothing to build: `0012_resolution_and_mentions.sql` admits at most one `proposal_decision` row per proposal (`UNIQUE (proposal_id)`), and the table carries no append-only constraint or trigger, so a changed decision is recorded by updating the existing row (service-level discipline). The proposal and its review decision remain **separate records** (`proposal` in 0005 holds content; `proposal_decision` in 0012 holds the outcome); recording a decision must not silently modify the proposal's content — content changes stay in the separate edit path (FR-REV-03, FR-REV-04: each edit its own revision, edit is not accept). **Status consistency:** when a decision is recorded, the service must bring `proposal.review_state` (0005: FR-REV-01 vocabulary PENDING/EDITED/ACCEPTED/REJECTED, NOT NULL, CHECK-enforced, default PENDING, mutable column) into the SRS-supported state matching that decision — the outcome tokens ACCEPTED/REJECTED are FR-REV-01 states and the accept/reject semantics are FR-REV-05/FR-REV-07 text. The decision write and the matching `review_state` update must be **atomic** (one transaction) when the review workflow is implemented, using the established single-connection `transaction()` pattern (NFR-CON-02 discipline). No new fields, statuses, transitions, or migration are authorized or required by this decision | A4-era review service slice (implements the flow; atomicity requirement recorded here) |
| **Review-state transition edges (Figure 6)** | **Unresolved — resolved only by the SRS DOCX.** FR-REV-01 fixes the status vocabulary and concrete outcomes in text, but the complete legal transition set (which edges exist, including into/out of EDITED, and whether ACCEPTED or REJECTED can ever move again) appears only in the review-states diagram; the Markdown SRS carries it solely as an image placeholder ("Figure 6 — Proposal review states", section 3.5). Per AGENTS.md the SRS v2.0 DOCX is the visual source for diagrams. The exact edges to enforce must be read from Figure 6 at the review service slice's planning step; they are not invented here | none (blocks transition-enforcement tests in the review service slice) |
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
