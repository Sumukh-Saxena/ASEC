# ASEC Phase 2 Plan — Persistent Data Models and Integrity

Status: active roadmap. Derived from `docs/SRS.md` (requirements authority), `AGENTS.md`
(workflow and architecture rules), and the actual repository state. This document is a
plan, not an implementation claim: schema existence never means behavior is implemented.

---

## 1. Purpose and boundary

Phase 2 designs, migrates, and tests the **persistent data layer** of the ASEC MVP: every
entity listed in SRS §4.1 (lines 500–517) with its integrity rules (§4.2, DR-01..DR-07),
implemented as versioned SQLite migrations plus their storage-level tests.

Phase 2 **delivers**: migration infrastructure, roster/organization tables, source
message/reaction storage, bucket storage, processing-attempt records, relevance-decision
records, proposals/revisions, review-state records, tasks/assignees/state
dimensions/history, cross-vertical approvals, notification records, announcement records.

Phase 2 **does not deliver**: real bucket-formation behavior (FR-BKT-03..07 remain
algorithmic), Discord ingestion, AI services or calls, relevance/proposal *model* runs,
review/task/notification *API or UI*, authorization engine, worker orchestration, or the
frontend. Phase 3+ owns ingestion/processing pipeline executable behavior, AI orchestration
(SRS §7), authorization enforcement (FR-AUT-*), and app surfaces (§3.10, FR-UI-*).

## 2. Current status (verified 2026-10-19)

- HEAD: `855ebb75ed3b21a698411a3163ced396aebd7994` — "Bucket schema added"
  (Slices 2.4 and 2.4.1 committed together; includes the FR-BKT-02 composite-FK
  correction and the B1 retry-INSERT fix). Working tree was clean at the time the
  repository state was first verified, before `docs/PHASE2_PLAN.md` itself was created
  (§9 workflow keeps this plan document untracked and unstaged pending explicit
  authorization).
- Committed migrations: `0001_core_roster.sql`, `0002_source_messages.sql`,
  `0003_buckets.sql` (versions 1–3).
- Full suite green at last run: **194 tests collected and passed**
  (`pytest -p no:cacheprovider -q`, exit 0).
- Note: earlier drafts of this plan assumed 2.4 was uncommitted; the repository now shows
  it committed. The committed 0003 enforces FR-BKT-02 (same-group membership) via two
  composite foreign keys plus parent-coverage unique indexes `ux_source_message_id_group`
  and `ux_bucket_id_group` — an approved Slice 2.4.1 correction, applied pre-commit.

## 3. Slice roadmap

Progress markers are factual. Everything past Slice 2.4 is a plan subject to the normal
per-slice approval flow (`/slice n` → approval → tests first → `/implement` → `/review` →
commit authorization). Adding a database CHECK that enumerates a state/outcome vocabulary
(for example the §4.1 line 509 attempt outcomes, FR-REV-01 review states, FR-XV-04
decisions) follows the established **D1 decision process**: the vocabulary must be
SRS-verbatim and explicitly approved by the project owner at that slice's planning step.

### Status legend
- [x] committed · [ ] planned

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
  exactly one bucket — **FR-BKT-01 (SRS line 216) is the eventual guarantee that every
  message belongs to exactly one bucket**, delivered by the future bucketing slice, not by
  this schema; the §10.1 scenario rows (lines 717–736: duplicate delivery → one message;
  message after closure → new bucket, earlier unchanged) corroborate it behaviorally.
- Open blockers: FR-BKT-04, FR-BKT-05 (blank bodies). Raised as a requirements issue for
  the project owner; the bucketing-formation slice cannot be planned until they are filled.

### Slice 2.5 — Processing attempts and relevance decisions [ ] planned
- Purpose/scope (proposed): `0004_processing_attempts.sql` (single migration, possibly split
  at slice planning) —
  - `processing_attempt`: stage, input references (bucket/message), model id + version,
    prompt/configuration version, started/finished times, outcome
    (`SUCCEEDED | FAILED_RETRYABLE | FAILED_PERMANENT` — §4.1 line 509), error text,
    tie-break/skew fields; append-only rows; retry creates a **new** attempt (NFR-REL-01/03).
    Classifier failure (timeout, model unavailable, malformed output, infrastructure
    error — FR-REL-07) is recorded on the **attempt row's outcome/error fields**; this is
    not a relevance decision.
  - `relevance_decision`: bucket FK, message FK, attempt FK, decision restricted to
    `RELEVANT | IRRELEVANT` — FR-REL-02 (no UNCERTAIN decision state), timestamp,
    immutability (FR-REL-06 — no UPDATE path), provenance per FR-REL-05 (message, bucket,
    attempt, model/prompt version, output, timestamp).
    **Proposed design, to confirm at `/slice 2.5` planning:** classifier-failure state is
    NOT duplicated into decision rows. `source_message.flag_unclassified` (0002, FR-REL-07)
    stays the single authoritative UNCLASSIFIED marker; decision rows exist only when the
    classifier produced RELEVANT/IRRELEVANT; human override is a **separate record** type
    (FR-REL-06; the correction *interface* is Post-MVP FR-PM-05 — the record shape may
    land here).
- Dependencies: 2.3, 2.4.
- Tests (required): immutability/reject malformed values; outcome vocabulary; attempts
  never deleted (append-only); one decision per message per attempt; failed attempts keep
  earlier records intact (NFR-REL-01/03/04); classifier failure never stored as
  IRRELEVANT (FR-REL-07, AC-12/13 — recorded marker semantics previously resolved:
  UNCLASSIFIED = classifier-failure marker); duplicate retry creates a new attempt without
  duplicate decisions; DR-07 retention.
- Acceptance: storage-level only. AC-12/AC-13 mapped to historian tests (recorded marker
  survives; no behavior beyond recording).
- Exclusions: any classifier execution, masking calls in pipeline, fallback classifier
  logic (NFR-REL-07 recording fields only), relevance *decisions for real messages*.

### Slice 2.6 — Proposals, revisions, provenance, review state [ ] planned
- Purpose/scope (proposed): `0005_proposals.sql` (+ possibly 0006) —
  - `proposal_generation_run` (per closed bucket; zero+ proposals out — FR-PRP-04/12).
  - `proposal`: type (`TASK | ANNOUNCEMENT` — FR-PRP-06), description, assignment mode
    (`EXPLICIT | OPEN | NONE` — FR-PRP-08), proposed assignee text, people needed,
    source vertical (group-derived, FR-PRP-07/09), suggested responsible vertical,
    affected verticals, task type suggestion, primary source message FK (exactly one —
    FR-PRP-03), context message links, generic attributes (key/value rows — FR-PRP-07),
    structural validation status (FR-PRP-13), provenance (run, model, prompt version —
    FR-PRP-12), review state (`PENDING | EDITED | ACCEPTED | REJECTED` — FR-REV-01),
    placeholder fields visible but unconfirmed (FR-PRP-11, AC-06 "unconfirmed" flag).
  - `proposal_revision`: one immutable revision per edit (FR-REV-03), who changed which
    fields (FR-REV-09), original generated version unchanged.
  - Assignee resolution records per FR-PRP-10 (mention exact; name match one/many/none),
    reviewer assignment (FR-REV-02: reviewer = sender of primary message), accept
    uniqueness (FR-REV-05 / AC-18: repeated/simultaneous accept produces one — schema +
    service responsibility), acceptance gating data (FR-REV-06).
- Dependencies: 2.5 (attempt/provenance FKs), 2.2 (roster), 2.3 (messages).
- Tests: immutability of revisions; review-state vocabulary; one-primary-message rule;
  zero proposals valid (AC-03); independent proposals from one run (AC-02); accept
  idempotency/concurrency shape (FR-REV-05 — exactly-one creation fact is service
  behavior; schema supports at-rest uniqueness); rejection retention (FR-REV-07/AC-04).
- Acceptance: AC-02, AC-03, AC-05 storage aspects.
- Exclusions: model runs, reviewer UI, edit/accept endpoints, assignee resolution
  *execution* (resolution logic = Phase 3), notifications (2.9 records only).

### Slice 2.7 — Tasks, assignee records, state dimensions, history [ ] planned
- **Blocked pending clarification: FR-TSK-06** (body blank) and **FR-TSK-13** (body
  blank). Slice planning needs their content before finalizing the dimension list beyond
  what FR-TSK-02 states.
- Purpose/scope (planned, subject to the blanks): `0007_tasks.sql` (possibly + 0008) —
  - `task` per FR-TSK-01: persistent ID, description, type, source vertical (immutable,
    FR-TSK-09), responsible vertical, affected verticals, creator (= accepting reviewer,
    FR-TSK-01), assignment mode, people needed, attributes.
  - State dimensions per FR-TSK-02 ("independent dimensions; no single field encodes more
    than one") — exact dimension list **blocked on FR-TSK-06**.
  - `task_assignee`: person FK, agreement state (AWAITING initial per FR-TSK-03; accepted
    only by in-app action or authorized on-behalf with that fact recorded — FR-TSK-05;
    never inferred from reactions/silence — AC-09, FR-PM-01 boundary), work status,
    timestamps.
  - `task_history`: append-only per FR-TSK-11 (field, old, new, actor + model version,
    time, reason/evidence, actor role + vertical at that time); NFR-AUD-01/03 (not
    editable via any app interface); optimistic concurrency surfacing (FR-TSK-12, AC-19 —
    version column).
- Dependencies: 2.6 (accepts create tasks — FR-REV-05), 2.2, 2.3, 2.5.
- Tests: dimension independence; EXPLICIT creates AWAITING assignees only (FR-TSK-03);
  claim-competition equal to exactly-one claim shape (FR-TSK-04, AC-08 — live claiming is
  Phase 3; at-rest uniqueness of (task, person) assignee is schema; NFR-CON-02 "simultaneous
  accepts, claims and assignments shall each have exactly one winner" governs the service
  behavior and the at-rest uniqueness shapes); history append-only; stale-version rejection
  data (FR-TSK-12); no merge-on-similarity semantics (FR-TSK-15); AI never mutates
  (FR-TSK-14 — records carry model actors read-only).
- Exclusions: claiming/status/cancel flows (FR-TSK-04/05/08 behavior), escalation rules,
  notifications, any AI involvement.

### Slice 2.8 — Cross-vertical approvals [ ] planned
- Purpose/scope (proposed): `0008_cross_vertical_approvals.sql` — `approval_request` (per
  §4.1 line 514: request, recipients, decision, messages; FR-XV-03: created PENDING and
  not visible to volunteers), `approval_decision` records (first decision wins — FR-XV-04;
  later attempts change nothing), requester/message linkage, task-history integration per
  FR-XV-06.
- Dependencies: 2.7 (tasks), 2.2 (roles/AH/Head).
- Tests: PENDING initial; decision vocabulary; first-decision-wins idempotency;
  decision retained on denial (FR-XV-05, AC-15); hidden-visibility is a *service*
  guarantee (schema records state only).
- Exclusions: visibility enforcement, notification delivery (2.9), any UI.

### Slice 2.9 — Notification records [ ] planned
- Purpose/scope (proposed): `0009_notifications.sql` — `notification` (event type from the
  FR-NOT-01 table, recipient person, linked authoritative record, delivery state, attempts
  and outcomes; per §4.1 line 516). Design contract: a change must commit even when its
  notification fails (FR-NOT-04, AC-17 — same durability pattern as FR-ING-05:
  change first, notification later, never one transaction); retry key prevents duplicate
  send for the same event+recipient (FR-NOT-05). One notification per event, no batching
  (FR-NOT-02).
- Dependencies: 2.2, 2.6, 2.7, 2.8 (event sources).
- Tests: failure-does-not-block-change; retry-without-duplicate unique key; wording/state
  fidelity is service-side (record fields only).
- Exclusions: push delivery, in-app list UI, backend push integration (Phase 3+,
  FR-PM-06 batching is Post-MVP).

### Slice 2.10 — Announcement records [ ] planned
- Purpose/scope (proposed): `0010_announcements.sql` — accepted-announcement record per
  FR-REV-10 ("audience: a Head's vertical, or the sender AH's volunteers; shall not create a
  task") linked to its proposal and acceptance reviewer, audience definition fields,
  FR-NOT-06 push scope fields. Announcements are proposals with type ANNOUNCEMENT
  (FR-PRP-06) that become announcement records on accept — no separate "task-like" state.
- Dependencies: 2.6, 2.2 (audience = vertical / reports-to structure), 2.7 history.
- Tests: no task created on accept (record shape + FR-REV-05 single-object rule); audience
  linkage integrity; retained with proposal lineage (DR-01).
- Exclusions: display surfaces (FR-UI-*), push behavior.

## 4. Migration numbering (derived from the repository, verified)

| Version | File | Slice | Status |
| --- | --- | --- | --- |
| 1 | `0001_core_roster.sql` | 2.2 | committed |
| 2 | `0002_source_messages.sql` | 2.3 | committed |
| 3 | `0003_buckets.sql` | 2.4 (+2.4.1 correction) | committed (HEAD `855ebb7`) |
| 4 | `0004_processing_attempts.sql` (name set at slice planning) | 2.5 | planned |
| 5 | proposal tables | 2.6 | planned |
| 6– | tasks/assignees/history | 2.7 | planned (number finalized at slice planning) |
| … | approvals → notifications → announcements | 2.8–2.10 | planned |

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
| **FR-BKT-04**, **FR-BKT-05** | bodies **blank** (SRS lines 222, 224; Figure 4 is informative) | bucket-formation slice (Phase 3 algorithmic work); Slice 2.4 schema deliberately stores fields only |
| **FR-TSK-06** | body blank (line 348) | Slice 2.7 dimension list is provisional until filled |
| **FR-TSK-13** | body blank (line 362) | Slice 2.7 scope boundary |
| **§6.4 performance targets** | 4 target cells blank (lines 608–611: message→proposal P95, classifier P95, proposal P95, availability) | latency instrumentation schema for NFR-PERF-02 (records shape; do not invent target values) |
| **§7 AI targets** | target cells **blank at lines 670–675** (four blank targets: relevance precision; proposal type/assignment mode; assignee/task type; proposal acceptance overall — plus one blank *measure* cell in the same block); exception: "Description quality" is already supplied as **"Human-rated, no percentage"** (line 675) | evaluation tooling and acceptance by §10.3 "AI targets met" |
| **§3.2 vs §10.1 bucketing parameters** | **unresolved owner decision** — SRS §3.2 parameter table sets W=15, C=2 (lines 209–214), while §10.1 test cases set W=10, C=3 (line 719); G=5 and M=30 agree. Must be resolved by the project owner **before bucket-formation implementation** (and before deciding what `bucket.parameters_used` records). Values are not chosen here. | bucketing-formation slice |
| Deferred 2.3/2.4-era decisions | role vocabulary, unknown-channel ingestion policy, post-recovery UNCLASSIFIED clearing, `parameters_used` scope | future slices owning those behaviors |

## 8. Later-phase boundary (not delivered by Phase 2)

Executable pipeline behaviors, orchestration and serving work all belong outside this
phase, chiefly:

- Bucket formation/join algorithm and close-time concurrency (FR-BKT-03..08; §10.1);
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
