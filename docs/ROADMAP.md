# ASEC Master Roadmap — Phases 7 to 12

Status: planning document, 2026-10-29. Planning only: no code, migrations or tests are
changed by this document. Authority: `docs/SRS.md` (requirements), `AGENTS.md` (workflow),
`docs/DECISIONS.md` §A/§B (owner-approved decisions), `docs/ASEC_Context_Prompt.md`
(context). Where this roadmap conflicts with the SRS or DECISIONS, those win and the
conflict is listed in §10 of the affected phase plan.

Scope of this roadmap: the six remaining MVP phases (7 foundations/tasks, 8 cross-vertical
approvals, 9 pipeline liveness, 10 AI integration/evaluation, 11 notification delivery,
12 API/auth/dashboards backend). Phase 6 is complete and committed; earlier phases are
mapped for coverage only.

---

## 1. Verified baseline (2026-10-29, read-only; re-verify at execution)

Real command output observed at the start of this planning session:

| Check | Observed |
|---|---|
| `git log --oneline -n 5` | `fb5263d Phase 6 planning complete` · `d83511d Beginning of phase 6` · `7e0d77c Phase 5 implemented :)` · `ab94b40 Implementation of Phase 5 begun…` · `39d694d Complete Phase 4 relevance…` |
| `git status --short` | empty (clean tree, nothing staged) |
| `.venv\Scripts\python.exe -m pytest` (plain) | `1016 passed in 165.69s (0:02:45)`, exit 0 |
| `.venv\Scripts\python.exe -m ruff check .` | `All checks passed!`, exit 0 |
| `.venv\Scripts\python.exe -m mypy` | `Success: no issues found in 14 source files`, exit 0 |
| `.venv\Scripts\python.exe -m backend --version` | `0.0.1`, exit 0 |
| `Get-ChildItem backend/db/migrations` | `0001` … `0015` (15 files) |
| Phase 6 committed | yes — `fb5263d` contains `backend/review.py`, `0015`, both test files and §15 |
| Phase 6 implementation record | §15.1–§15.8 filled (files, totals, mutation battery, audit, closing fixes) |

Documentation observation (resolved 2026-10-30): §15.8's heading said "uncommitted tree at
`d83511d`"; it now says "committed in `fb5263d`". Phase 6 is committed; §15 is
filled. Every phase plan repeats a "re-verify at execution" baseline.

---

## 2. Phase dependency graph

```
Phase 6 (done, committed)
   │
   ├── Phase 7  Foundations + tasks
   │      ├── relevance retrofit (opaque handles, markup rewrite)   [no dependency]
   │      ├── eligibility default ON                                [no dependency]
   │      ├── schema window 0016/0017/0018 (PB-5, PB-3, PB-6)       [no dependency]
   │      ├── shared authz + notification-staging extraction        [no dependency]
   │      ├── task lifecycle FR-TSK-01..16                          [all 7 slices]
   │      └── single visibility filter (hidden PENDING, cancelled)  [shared by 8, 12]
   │
   ├── Phase 8  Cross-vertical approvals (needs 7 visibility + task state)
   │      └── approval decisions FR-XV-04..06, AC-14/15
   │
   ├── Phase 9  Pipeline liveness (needs 7 schema window, not 8)
   │      ├── PB-1 clock/settle-then-assign + scheduler/worker
   │      ├── PB-2 time basis + replay importer
   │      ├── PB-7 late-only links
   │      └── Discord adapter (inside backend host)
   │
   ├── Phase 10 AI integration + evaluation (needs 9 for live/replay data)
   │      ├── HTTP model adapter over existing protocols
   │      ├── masking + opaque-handle contract (7 retrofit)
   │      └── evaluation harness, benchmark, model-selection rule
   │
   ├── Phase 11 Notification delivery (needs 7/8 staging; independent of 10)
   │      ├── delivery worker, retries, in-app list
   │      ├── FCM push behind interface
   │      ├── OTP delivery behind interface (used by 12)
   │      └── announcement fan-out from PB-6 audience
   │
   └── Phase 12 API/auth/dashboards backend (needs 7, 8, 11; 9/10 for data views)
          ├── HTTP API + session auth (roll number + OTP)
          ├── FR-AUT matrix per operation, denial consolidation
          ├── NFR-SEC-03 hierarchy visibility
          └── dashboards/backlog/admin data endpoints (mobile UI out of repo)
```

Hard edges: 7 → 8, 7 → 12, 8 → 12, 11 → 12 (OTP sender and notification list),
9 → 10. Soft edges: 9 can run while 10's harness is written against fixtures; 11 can
run while 10 benchmarks. Nothing in 8 depends on 9 or 10.

---

## 3. Shared contracts later phases rely on

These are real, current signatures (verified in the repository on 2026-10-29). Plans may
extend them additively but must not silently change names or meanings.

### 3.1 Existing foundation contracts

| Owner module | Contract (real) | Used by |
|---|---|---|
| `backend/db/connection.py` | `connect(path: str \| Path) -> sqlite3.Connection`; `transaction(connection)` context manager (`BEGIN IMMEDIATE`, rollback on raise) | every phase |
| `backend/db/migrations.py` | `apply_migrations(connection, directory=MIGRATIONS_DIR) -> list[int]`; each file one transaction, checksum-pinned | every migration |
| `backend/config.py` | `Config.get(name, default=None) -> str \| None`; `Config.require(name) -> str`; `load_config(environ=None) -> Config`; secrets never rendered | every phase |
| `backend/ingest.py` | `MessageEvent(source, source_message_id, discord_channel_id, sender_discord_user_id=None, event_timestamp=None, received_at=None, content=None, reply_reference=None, raw_metadata=None, mentions=(), reactions=())`; `ingest_message(connection, event) -> StoreResult(message_id, created)`; `ingest_reaction(connection, reaction, message_id)` | 9 adapter/replay, 12 admin views |
| `backend/bucketing.py` | `BucketParams(window_w=15, burst_gap_g=5, max_duration_m=30, close_grace_c=2)`; `assign_messages(connection, *, group_id=None, params=None) -> AssignmentResult`; `mark_stream_progress(connection, *, group_id, event_progress) -> ProgressResult`; `parse_event_time(value)` | 9 PB-1/PB-2/PB-7 |
| `backend/orchestration.py` | `run_bucketing_pass(connection, *, params=None, group_id=None) -> AssignmentResult`; `pipeline_backlog(connection) -> PipelineBacklog`; `processing_queue(connection) -> ProcessingQueue` | 9 worker, 12 admin |
| `backend/bucket_lifecycle.py` | `bucket_processing_state(connection, bucket_id)`; `transition_bucket_processing_state(connection, bucket_id, target)` | 9 worker |
| `backend/relevance.py` | `class RelevanceClassifier(Protocol): classify(request: RelevanceRequest) -> RelevanceResponse`; `run_relevance_pass(connection, primary, fallback=None, *, max_attempts_per_bucket=…, budget_overrides=None) -> RelevancePassResult`; `requeue_failed_bucket(connection, bucket_id)` | 9, 10 |
| `backend/proposals.py` | `class ProposalGenerator(Protocol): generate(request: ProposalGenerationRequest) -> ProposalResponse`; `run_proposal_pass(connection, primary, fallback=None, *, max_attempts_per_bucket=3, budget_overrides=None, policy=None) -> ProposalPassResult`; `proposal_pending_buckets`, `proposal_exhausted_buckets`, `proposal_backlog` | 9, 10, 12 |
| `backend/review.py` | `get_proposal_review(connection, proposal_id, *, actor_person_id, policy=None, config=None)`; `save_proposal_edit(connection, proposal_id, *, actor_person_id, expected_revision_number, changes: ProposalEdit, …)`; `accept_proposal(connection, proposal_id, *, actor_person_id, expected_revision_number, note=None, request_message=None, …)`; `reject_proposal(connection, proposal_id, *, actor_person_id, note=None, …)`; `notify_proposal_ready(connection, proposal_id) -> int \| None`; `sweep_unnotified_proposals(connection) -> tuple[int, …]`; `list_reviewed_proposals_for_eval(connection, *, actor_person_id, …)` | 8, 11, 12 |
| `backend/masking.py` | `mask_pii(text) -> str` | 7 retrofit, 10 adapter |

### 3.2 Contracts established by Phases 7–12 (plans must pin these)

| Origin | Contract (planned) | Consumers |
|---|---|---|
| Phase 7 | `backend/authorization.py`: `record_denial(connection, *, actor_person_id, operation, subject_type, subject_id, reason)` (own transaction, never raises on success); `require_active_actor(connection, actor_person_id, *, operation, subject_type, subject_id) -> sqlite3.Row`; token constants for reasons/operations | 7, 8, 9, 11, 12 |
| Phase 7 | `backend/notification_events.py`: `stage_notification(connection, *, event_type, recipient_person_id, dedupe_key, …, created_at) -> bool`; event constants and dedupe-key builders (`PROPOSAL_READY:<id>`, `TASK_ASSIGNED:<task>:<person>`, `OPEN_TASK_AVAILABLE:<task>:<person>`, `CROSS_VERTICAL_REQUEST:<request>:<person>`, `HEAD_CROSS_VERTICAL_TASK:<task>:<person>`) | 7, 8, 11 |
| Phase 7 | `backend/tasks.py`: `visible_tasks(connection, *, viewer_person_id, include_cancelled=False, include_hidden=False)` (the single filter: `approval_state IN ('NOT_REQUIRED','APPROVED')` AND `cancellation='ACTIVE'` unless included; authority derived from `person`/`person_vertical` inside, never passed in); `claim_open_task(connection, task_id, *, actor_person_id) -> ClaimResult`; `set_agreement(connection, task_id, *, actor_person_id, assignee_person_id, agreement)`; `update_work_status(connection, task_id, *, actor_person_id, status)`; `cancel_task(connection, task_id, *, actor_person_id, reason)`; `edit_task(connection, task_id, *, actor_person_id, expected_version, changes)`; `reassign_task(...)`; `task_progress(connection, task_id, *, viewer_person_id)` | 8, 12 |
| Phase 7 | `backend/ai_context.py`: `MentionHandles`, `rewrite_markup`, `reply_to_message_id`, `masked_request_content` shared by relevance and proposals (opaque handles, no Discord IDs) | 10 |
| Phase 8 | `backend/approvals.py`: `decide_request(connection, request_id, *, actor_person_id, decision, message=None) -> DecisionResult` (first decision wins); `list_pending_approvals(connection, *, actor_person_id) -> tuple[...]` | 12 |
| Phase 9 | `backend/scheduler.py`: `advance_group_clock(connection, group_id, *, now, mode) -> ClockResult`; `run_pipeline_tick(connection, *, classifiers, generators, now, mode) -> TickResult`; `backend/worker.py`: `run_worker(connection, *, tick, stop_event, interval_seconds) -> None`; `backend/replay.py`: `import_export(connection, path, *, timezone, source)`; `backend/discord_adapter.py`: `DiscordSource` protocol producing `MessageEvent`/`ReactionEvent` | 10, 12 |
| Phase 10 | `backend/ai/http_client.py`: `HttpModelAdapter` implementing `RelevanceClassifier` **and** `ProposalGenerator`; `backend/evaluation.py`: `load_labelled_set(path)`, `score_relevance`, `score_proposals`, `run_evaluation`, `write_report` | 12 (admin eval views) |
| Phase 11 | `backend/notification_delivery.py`: `deliver_pending(connection, *, push: PushSender, sms: OtpSender, limit) -> DeliveryBatch`; `list_notifications(connection, *, person_id, unread_only=False)`; `fan_out_announcement(connection, announcement_id)`; protocols `PushSender.send(recipient, title, body, deep_link)`, `OtpSender.send(phone, code)` | 12 |
| Phase 12 | `backend/auth.py`: `request_otp(connection, *, roll_number, sender) -> None`; `verify_otp(connection, *, roll_number, code) -> Session`; `authenticate(connection, token) -> AuthenticatedPerson`; `backend/api/routes.py` dispatching to the services above | mobile app (out of repo) |

Import direction stays acyclic: domain services never import the API; `review.py` never
imports `proposals.py`; `authorization.py` and `notification_events.py` import only
`db.connection`.

---

## 4. Migration numbering plan (0016 onward)

One line each. No committed migration is edited. Every number is reserved here so
parallel work cannot collide.

| Version | File (planned) | Implements | Notes |
|---|---|---|---|
| 0016 | `0016_roster_integrity.sql` | PB-5 | Rebuild `person`: role CHECK `('Admin','Head','AH','Volunteer')`, `reports_to` FK to `person(id)`, `phone` and `roll_number` NOT NULL, unique kept. Runs in the pre-roster window; no defaults invented. |
| 0017 | `0017_task_schema.sql` | PB-3 | Rebuild `task` (EXPLICIT `people_needed` NULL; INTERNAL `responsible_vertical_id` NOT NULL = source) and `task_assignee` (surrogate `id`; partial unique `ux_task_assignee_active` on `(task_id, person_id) WHERE agreement != 'DENIED' AND cancelled = 0`). |
| 0018 | `0018_announcement_audience.sql` | PB-6 | Rebuild `announcement`: `audience_type` CHECK `('VERTICAL','AH_VOLUNTEERS')`, `audience_vertical_id` NOT NULL, nullable `audience_ah_person_id` FK; maps Phase 6 `audience_scope` tokens one-to-one. |
| 0019 | `0019_source_message_time_basis.sql` | PB-2 | Additive `source_message.time_basis TEXT` (`EVENT`/`RECEIPT`, NULL for legacy rows); event time is never overwritten. |
| 0020 | `0020_group_clock.sql` | PB-1 | `source_group_clock(group_id PK, clock_at TEXT NOT NULL, mode TEXT NOT NULL, updated_at TEXT NOT NULL)`; durable clock for live/replay restarts. |
| 0021 | `0021_replay_batches.sql` | FR-ING-08 | `replay_batch(id, source, file_sha256, time_zone, started_at, finished_at, outcome, counts_json)` for replay provenance; ingestion itself still uses `ingest_message`. |
| 0022 | `0022_evaluation.sql` | AI-04/05/07/08 | `evaluation_run` (model/version/prompt, dataset sha256, times, metrics JSON) and `evaluation_case_result` (run, case id, task, expected, predicted, correct); append-only. |
| 0023 | `0023_push_endpoints.sql` | FR-NOT-07, EIR-04 | `push_endpoint(id, person_id FK, provider CHECK ('FCM'), token TEXT UNIQUE, active, created_at, last_seen_at)`; token is a secret, never logged. |
| 0024 | `0024_auth_sessions.sql` | FR-ROS-03, FR-AUT-01 | `otp_challenge` (roll/person, code hash, expires, attempts, consumed) and `auth_session` (token hash, person, issued/expires/revoked) plus `auth_attempt` for rate-limit/audit. |

Not migrated: Phase 8 uses existing `approval_request`/`approval_recipient`/
`approval_decision`; PB-7 is a code change inside 0020's work; notification delivery uses
`notification`/`notification_attempt` from 0008.

---

## 5. Cross-phase risks

1. **Rebuild migrations on a non-empty database.** `person`, `task`, `task_assignee` and
   `announcement` are referenced by RESTRICT foreign keys. Use the SQLite table-rebuild
   procedure inside the migration transaction with `PRAGMA defer_foreign_keys = ON;`,
   copy rows only when they satisfy the new constraints (fail loudly otherwise), and test
   on a populated fixture. If a pre-window database holds invalid roster rows, stop — the
   window is defined as pre-roster-load.
2. **Hidden-task leakage.** `task_assignee` rows already exist for `approval_state =
   'PENDING'` (Phase 6). Any ad-hoc query in Phases 7, 8 or 12 can expose them. Mitigation:
   one `visible_tasks` filter, used by every volunteer/dashboard/API query, with a
   mutation check that removing the approval filter turns a test red.
3. **Authorization-divergence drift.** Phase 6's denial recorder is private. Phase 7 must
   extract it (and notification staging) without changing behaviour; regression is
   covered by the full Phase 6 suite staying green and by denial-persistence tests.
4. **Long transactions around network calls.** SQLite has one writer. Model and push calls
   must happen outside `transaction()`; commit the state change first, then deliver.
   Otherwise a slow model stalls every writer and can exceed the P95 targets.
5. **The 30 s P95 target includes bucket wait.** With M=30 s and C=2 s the bucket alone can
   consume 32 s. This is an SRS ambiguity (DECISIONS §D); benchmark results must report it,
   not hide it. O10-6 answered: keep the 30 s target in code, document the wait, and the
   owner later edits the SRS to measure from bucket close.
6. **Duplicate notifications.** Retries must key on `notification.dedupe_key`. Any new
   event type added in 7/8/11 must go through `notification_events` so one key format
   exists.
7. **Announcement audience drift.** PB-6 replaces free-text scope. Phase 11 fan-out must
   use `audience_type`/`audience_ah_person_id`; Phase 6's `AH_VOLUNTEERS` vertical
   approximation must not survive into delivery.
8. **Adapter/framework dependencies.** No runtime dependency exists today. The dependency
   policy in `docs/OWNER_ANSWERS.md` allows only optional extras (`push`, `api`), exact-pinned,
   confined to one module, with core tests green without them; anything else needs an OWNER
   decision.
9. **Evaluation data is real personal data.** Labelled sets stay out of the repository or
   are synthetic/redacted; masking applies before any hosted or self-hosted call; the
   archive-only retention default (`DECISIONS` §B) governs training use.
10. **Migration numbering collisions.** Numbers 0016–0024 are reserved here; a phase that
    needs another migration appends 0025+ and updates this table before writing it.
11. **Test typing debt.** `mypy` covers `backend` only; 89 pre-existing errors in tests are
    known. A cleanup slice is optional and never blocks a phase.
12. **Framework choice.** Answered by O12-1: FastAPI-class framework in an optional `api`
    extra confined to `backend/api`. Phase 12's §4.2 must be rewritten at phase start before
    W2; Phase 11's OTP interface and Phase 10's admin eval views stay framework-free.

---

## 6. Full SRS requirement coverage (every ID exactly once)

Legend: phase numbers are the phase that **completes** the requirement (earlier partial
work noted); `POST-MVP` = SRS §3.11/§1.3; `OUT OF REPO` = mobile app or owner-run
operational work. Phase 0–6 entries are complete and committed.

| ID | Phase / disposition | Note |
|---|---|---|
| FR-ING-01 | 9 | adapter inside backend host |
| FR-ING-02 | 2 | normalized record storage |
| FR-ING-03 | 2 | receipt vs event timestamp |
| FR-ING-04 | 2 | idempotent ingestion |
| FR-ING-05 | 2 | store before processing |
| FR-ING-06 | 2 | reply/reference preserved |
| FR-ING-07 | 2 | reactions evidence only |
| FR-ING-08 | 9 | replay importer, explicit timezone |
| FR-ING-09 | 2 | UNMAPPED groups |
| FR-ING-10 | 2 | UNKNOWN_SENDER |
| FR-ING-11 | POST-MVP | source edits/deletes |
| FR-ING-12 | POST-MVP | attachments |
| FR-BKT-01 | 9 | PB-1/PB-2 complete every-message placement |
| FR-BKT-02 | 9 | per-group independence (Phase 3 initial) |
| FR-BKT-03 | 9 | event-time ordering (Phase 3 initial) |
| FR-BKT-04 | 9 | join rule (Phase 3 initial) |
| FR-BKT-05 | 9 | closure via clock (PB-1) |
| FR-BKT-06 | 9 | arrival-order invariance (PB-1) |
| FR-BKT-07 | 9 | close/add serialization |
| FR-BKT-08 | 9 | late message, new linked bucket |
| FR-BKT-09 | 3 | bucket is a processing boundary |
| FR-BKT-10 | 3 | bucket identity separate |
| FR-BKT-11 | 9 | oversize buckets processed in parts |
| FR-BKT-12 | 3 | five bucket states |
| FR-REL-01 | 7 | classifier on every closed bucket; Phase 4 initial |
| FR-REL-02 | 4 | two decisions only |
| FR-REL-03 | 4 | doubt → RELEVANT |
| FR-REL-04 | 4 | per-message decisions |
| FR-REL-05 | 4 | decision provenance |
| FR-REL-06 | 4 | immutable decision |
| FR-REL-07 | 4 | failure never IRRELEVANT |
| FR-REL-08 | 4 | irrelevant messages retained |
| FR-PRP-01 | 5 | proposal model per bucket |
| FR-PRP-02 | 7 | eligibility default ON (Phase 5 policy) |
| FR-PRP-03 | 5 | one primary message |
| FR-PRP-04 | 5 | zero/one/many proposals |
| FR-PRP-05 | 5 | independent validation |
| FR-PRP-06 | 5 | TASK/ANNOUNCEMENT |
| FR-PRP-07 | 5 | proposal fields |
| FR-PRP-08 | 5 | assignment modes |
| FR-PRP-09 | 5 | task type suggestion |
| FR-PRP-10 | 5 | backend name resolution |
| FR-PRP-11 | 5 | placeholders, no invention |
| FR-PRP-12 | 5 | generation provenance |
| FR-PRP-13 | 5 | structural validation |
| FR-PRP-14 | 5 | proposals persist |
| FR-PRP-15 | 5 | no inferred acceptance |
| FR-PRP-16 | 6 | human review boundary |
| FR-PRP-17 | 6 | reviewer-only notification |
| FR-REV-01 | 6 | review states |
| FR-REV-02 | 6 | reviewer + Admin |
| FR-REV-03 | 6 | immutable revisions |
| FR-REV-04 | 6 | edit ≠ accept |
| FR-REV-05 | 6 | exactly one entity |
| FR-REV-06 | 6 | acceptance gates |
| FR-REV-07 | 6 | rejection retained |
| FR-REV-08 | 6 | labelled eval data |
| FR-REV-09 | 6 | changed fields |
| FR-REV-10 | 11 | record + structured audience in 6; "shown to its audience" is the Phase 11 fan-out |
| FR-TSK-01 | 7 | task fields |
| FR-TSK-02 | 7 | independent state dimensions |
| FR-TSK-03 | 7 | EXPLICIT assignees AWAITING |
| FR-TSK-04 | 7 | OPEN claims, one winner |
| FR-TSK-05 | 7 | agreement only by action |
| FR-TSK-06 | 7 | work status, COMPLETED final |
| FR-TSK-07 | 7 | per-volunteer progress + roll-up |
| FR-TSK-08 | 7 | creator cancellation |
| FR-TSK-09 | 7 | source vertical fixed |
| FR-TSK-10 | 7 | role-based authority |
| FR-TSK-11 | 7 | append-only history |
| FR-TSK-12 | 7 | stale edit rejected + history |
| FR-TSK-13 | 7 | transition enforcement |
| FR-TSK-14 | 7 | AI never changes tasks |
| FR-TSK-15 | 7 | no task merging |
| FR-TSK-16 | 7 | edit/reassign authority |
| FR-XV-01 | 6 | cross-vertical determination |
| FR-XV-02 | 6 | Head task goes live |
| FR-XV-03 | 6 | AH request staged, hidden |
| FR-XV-04 | 8 | first decision wins |
| FR-XV-05 | 8 | approve live / deny kept |
| FR-XV-06 | 8 | history with role/vertical |
| FR-NOT-01 | 11 | notification event matrix |
| FR-NOT-02 | 11 | one per proposal, no batching |
| FR-NOT-03 | 11 | wording matches state |
| FR-NOT-04 | 11 | failure never rolls back |
| FR-NOT-05 | 11 | retry without duplicates |
| FR-NOT-06 | 11 | Head/AH announcement push |
| FR-NOT-07 | 11 | push + in-app list |
| FR-ROS-01 | 12 | roster load (operational, no UI import) |
| FR-ROS-02 | 12 | fixed roster, Admin-only changes |
| FR-ROS-03 | 12 | roll number + OTP to roster phone |
| FR-AUT-01 | 12 | server-side authorization |
| FR-AUT-02 | 12 | protected operation list |
| FR-AUT-03 | 12 | volunteer restrictions |
| FR-AUT-04 | 12 | matrix approval + one test per cell |
| FR-AUT-05 | 12 | current role, history keeps role/vertical |
| FR-AUT-06 | 12 | reject and record (Phase 6 implemented review) |
| FR-AUT-07 | 12 | deactivation, never hard-delete |
| FR-AUT-08 | 12 | content never grants authority |
| FR-UI-01 | OUT OF REPO | mobile app; API data from 12 |
| FR-UI-02 | OUT OF REPO | volunteer dashboard UI; API data from 12 |
| FR-UI-03 | OUT OF REPO | AH dashboard UI; API data from 12 |
| FR-UI-04 | OUT OF REPO | Head dashboard UI; API data from 12 |
| FR-UI-05 | 12 | admin backlog/attempts/config data (UI out of repo) |
| FR-UI-06 | OUT OF REPO | review screen rendering; provenance from 6 |
| FR-UI-07 | OUT OF REPO | no authority logic in the app |
| FR-PM-01 | POST-MVP | reactions as acceptance |
| FR-PM-02 | POST-MVP | routing volunteer messages to AH |
| FR-PM-03 | POST-MVP | pre-bucket context |
| FR-PM-04 | POST-MVP | context processing |
| FR-PM-05 | POST-MVP | promote irrelevant messages |
| FR-PM-06 | POST-MVP | notification batching |
| FR-PM-07 | POST-MVP | multi-message proposals |
| FR-PM-08 | POST-MVP | source edits/attachments/reprocessing |
| FR-PM-09 | POST-MVP | FC role |
| FR-PM-10 | POST-MVP | voice, incident/emergency |
| DR-01 | 2 | enforced references |
| DR-02 | 2 | no delete/detach |
| DR-03 | 2 | derived never replaces source |
| DR-04 | 2 | missing/empty/negative distinct |
| DR-05 | 2 | completed-only processed/accepted |
| DR-06 | 2 | derived stores rebuildable |
| DR-07 | 2 | retention through archive |
| EIR-01 | 9 | Discord adapter contract |
| EIR-02 | 12 | app talks only to backend API |
| EIR-03 | 10 | backend↔AI contract (Phase 4/5 protocols) |
| EIR-04 | 11 | FCM push |
| EIR-05 | 12 | same authorization on every interface |
| NFR-REL-01 | 3 | recorded, retry-safe attempts |
| NFR-REL-02 | 3 | only execution failures retried |
| NFR-REL-03 | 4 | failed stage recoverable |
| NFR-REL-04 | 9 | worker crash recovery |
| NFR-REL-05 | 9 | task management survives AI outage |
| NFR-REL-06 | 12 | backlog visible in admin view (functions in 3) |
| NFR-REL-07 | 10 | fallback model recorded |
| NFR-AUD-01 | 6 | task traceability chain |
| NFR-AUD-02 | 5 | AI input/output provenance |
| NFR-AUD-03 | 7 | history not editable via interfaces |
| NFR-CON-01 | 2 | concurrent ingestion |
| NFR-CON-02 | 7 | exactly one winner (accept in 6, claims in 7) |
| NFR-PERF-01 | 10 | benchmark confirms targets |
| NFR-PERF-02 | 9 | latency instrumentation (table in 2) |
| NFR-PERF-03 | 10 | load-test matrix |
| NFR-SEC-01 | 12 | authentication + secret handling |
| NFR-SEC-02 | 12 | protect evidence/audit |
| NFR-SEC-03 | 12 | hierarchy visibility |
| NFR-SEC-04 | 7 | masking before every AI call (retrofit) |
| NFR-SEC-05 | 10 | self-hosted models, Colab masking |
| NFR-MNT-01 | 9 | parameters/models/prompts are config |
| NFR-MNT-02 | 9 | new source = new adapter |
| NFR-MNT-03 | 10 | model swap changes no semantics |
| NFR-MNT-04 | 7 | bucketing/auth/state/idempotency model-free tests |
| AI-01 | 10 | two logically separate AI functions |
| AI-02 | 10 | AI output advisory only |
| AI-03 | 6 | human edits stored separately |
| AI-04 | 10 | evaluate functions separately + end to end |
| AI-05 | 10 | labelled Hinglish evaluation set |
| AI-06 | 10 | smallest model meeting targets |
| AI-07 | 10 | reviewer correction rate per version |
| AI-08 | 10 | end-to-end recall + review burden |
| BR-01 | 2 | message is evidence |
| BR-02 | 7 | distinct identities/verticals/decision-maker |
| BR-03 | 7 | proposed ≠ agreeing assignee |
| BR-04 | 6 | acceptance does not verify fields |
| BR-05 | 3 | bucket is not a task/conversation |
| BR-06 | 2 | later message never erases evidence |
| BR-07 | 4 | execution failure ≠ negative result |
| BR-08 | 8 | conflicting evidence preserved until decision |
| BR-09 | 7 | reaction is evidence, not acceptance |
| BR-10 | 6 | only reviewer/Admin converts |
| AC-01 | 4 | independent per-message decisions |
| AC-02 | 5 | two tasks in one message |
| AC-03 | 5 | zero proposals is valid |
| AC-04 | 6 | reject retained |
| AC-05 | 6 | edit without accept |
| AC-06 | 6 | accept keeps AI values unconfirmed |
| AC-07 | 6 | explicit assignment flow |
| AC-08 | 7 | open task claims, one winner |
| AC-09 | 7 | ambiguity never infers agreement |
| AC-10 | 5 | volunteer message: no proposal |
| AC-11 | 5 | name resolution |
| AC-12 | 4 | failure ≠ irrelevant |
| AC-13 | 9 | classifier outage recovery |
| AC-14 | 8 | Head cross-vertical live |
| AC-15 | 8 | AH request/approval/denial |
| AC-16 | 7 | completion and creator visibility |
| AC-17 | 11 | notification failure retried |
| AC-18 | 6 | two accepts → one |
| AC-19 | 7 | stale edit rejected, both logged |
| AC-20 | 12 | direct assign API call rejected/logged |
| AC-21 | 4 | masking in AI inputs |
| AC-22 | 2 | reaction changes nothing |

Count check: 192 IDs listed (12+12+8+17+10+16+6+7+3+8+7+10+7+5+7+3+2+3+5+4+8+10+22).

---

## 7. Owner inputs — answers (single consolidated table)

"OWNER" means only the owner can supply/approve it; agents must not decide it. Items
already decided in `docs/DECISIONS.md` are not repeated. The `Oxx-n` IDs are the same IDs
used in each phase plan §9 and in each execution prompt's `OWNER DECISIONS` block. **Every
row below is answered in `docs/OWNER_ANSWERS.md` (29 Oct 2026); the Answer column quotes
it.** Items marked OWNER SUPPLIES remain stop gates and are collected in §7.1.

| ID | Phase | Owner input | Answer (`docs/OWNER_ANSWERS.md`) | Gates |
|---|---|---|---|---|
| O7-1 | 7 | Confirm the schema window (0016–0018) runs before any roster load | (a) Confirm: run 0016–0018 as a pre-roster window. If real roster rows exist, stop. | W3 |
| O7-2 | 7 | Authorize the mechanical Phase 6 pinned-test updates forced by PB-3/PB-6 | (a) Authorized: mechanical updates of Phase 6 pinned tests forced by PB-3 and PB-6, each with a one-line reason citing the requirement. | W3 |
| O7-3 | 7 | May an authorized user accept/deny on an assignee's behalf (FR-TSK-05)? | (a) Admin only may accept or deny on an assignee's behalf, recorded in history with an on-behalf flag and the Admin as actor. Nobody else. | W6 |
| O7-4 | 7 | Cancel authority: FR-TSK-08 creator vs FR-TSK-16/matrix Head/Admin | (a) Cancel authority: creator, Head of the responsible vertical, and Admin. | W6 |
| O7-5 | 7 | Admin work-status override (FR-TSK-06 says Admin; matrix cell "—") | (a) Admin may override work status, recorded in history. | W6 |
| O7-6 | 7 | Role CHECK vs the Phase 6 case-insensitive Admin test | (a) Strict role CHECK (`Admin`, `Head`, `AH`, `Volunteer`); the roster loader normalizes case; rewrite the Phase 6 case-insensitive Admin test to prove policy matching. | W3 |
| O7-7 | 7 | Optional test-typing cleanup (89 pre-existing errors) | (a) Defer the test-typing cleanup. | none |
| O8-1 | 8 | May Admin decide any pending request without being a recipient? | (a) Admin may decide any pending request without being a recipient. Add the matrix row. | W1 |
| O8-2 | 8 | Recipient whose current role/vertical changed: snapshot or current? | (a) Authority uses the person's current role and vertical (FR-AUT-05); the recipient snapshot only records who was asked. | W1 |
| O8-3 | 8 | Late decision attempt: silent no-op or recorded? | (a) A late decision attempt writes nothing and returns the existing decision (FR-XV-04: later attempts see it). | W2 |
| O9-1 | 9 | Discord intake implementation | (a) Stdlib REST polling with an injected transport, as planned. A gateway client is post-MVP if latency or rate limits hurt. | W5 |
| O9-2 | 9 | Supply a bot token + test guild/channel via secure config | **OWNER SUPPLIES** the bot token (environment variable only), a test server and channel, and enables the Message Content intent in the Discord developer portal. Reset the old token first. | W5 |
| O9-3 | 9 | Worker deployment, interval/backoff defaults | (a) 2 s interval, backoff 1–60 s. | W3 |
| O9-4 | 9 | Approve the replay export schema and indicative rules | Approve, **with an amendment**: the importer must take a sender mapping (export sender label → Discord user ID) and a group mapping (export group → channel), because WhatsApp exports have no Discord IDs. Unmapped senders become UNKNOWN_SENDER and are counted in the batch row. Message IDs for exports without IDs are derived deterministically (hash of group, time, sender label, content). | W4 |
| O9-5 | 9 | Clock row on a replay-first database | (a) No seed; the first caller sets the clock mode. | W2 |
| O9-6 | 9 | Default part sizes (40 relevance messages, 20 proposal candidates) | (a) Defaults 40 relevance messages and 20 proposal candidates per part; revisit after the Phase 10 benchmark. | W6 |
| O10-1 | 10 | Approve the adapter wire contract and endpoint paths | (a) Approve the wire contract as written. | W1 |
| O10-2 | 10 | Supply/approve the labelled evaluation set, location, labeller, minimum size | **OWNER SUPPLIES** the labelling work: at least 300 masked messages from the sample, including at least 80 RELEVANT and at least 60 hard negatives (jokes, status reports, banter), each with the sender's role and, for relevant ones, the expected proposal. The assistant helps label; the owner confirms. | W4–W7 |
| O10-3 | 10 | Accuracy/recall thresholds for AI-06 and the final model choice | Use the SRS section 7 targets: relevance recall at least 95% and precision at least 80%; proposal type and assignment mode at least 95% each; assignee and task type at least 90% each; proposal acceptance at least 80%. **The plan is wrong that none exist.** | W7 |
| O10-4 | 10 | Which self-hosted models/hardware; hosted models reference-only | Start with the owner's Windows PC (16 GB RAM, NVIDIA 4–8 GB VRAM) running one small model (about 4B) and one 7–8B model through Ollama, and a larger open model of about 14B in a Colab notebook for the quality ceiling. A hosted model (Gemini) only on masked synthetic text, as a baseline. The owner confirms the exact model tags in the Ollama library. | W7 |
| O10-5 | 10 | Confirm archive-only retention, no training on festival chat | (a) Archive only; no training on festival chat without a consent plan. | none |
| O10-6 | 10 | Accept/reject the 30 s P95 target given a 17–32 s bucket wait | (c) Keep the 30 s target in code and document that the bucket wait alone is 17–32 s. The owner later edits the SRS to measure from bucket close. | report |
| O11-1 | 11 | OTP/SMS provider + credentials; resolve FR-ROS-03 SMS vs EIR-04 FCM | (c) Build the OTP sender interface with a development sender now. **OWNER SUPPLIES** the SMS provider before go-live (in India, bulk SMS usually needs sender and template registration, so start early). FR-ROS-03 needs SMS to the roster phone because the app has no device token before sign-in. | W5 |
| O11-2 | 11 | FCM mode | (b) FCM HTTP v1 through an optional `push` extra (the signing needs a package the standard library lacks), isolated behind `PushSender`. I believe the legacy FCM HTTP API was shut down in 2024; confirm in the Firebase docs before building. | W4 |
| O11-3 | 11 | Retry policy and batch limit | (a) 5 attempts, 5 s base, 5 min cap, 50 per batch. | W2 |
| O11-4 | 11 | `VERTICAL` announcement audience = all active members or volunteers only? | (a) The `VERTICAL` audience is all active members of the vertical. | W3 |
| O11-5 | 11 | Deep-link scheme for the mobile app (out of repo) | **OWNER SUPPLIES** the app's deep-link scheme later; use an opaque path until then. | W1 |
| O12-1 | 12 | HTTP framework | (b) A mature framework (FastAPI-class) in an optional `api` extra, confined to `backend/api`, domain services framework-free. The Phase 12 plan needs a rewrite of its server section at phase start. | W2 |
| O12-2 | 12 | Approve the minimum route surface as the mobile contract | Approve the 12-endpoint surface, subject to a quick review at phase start. | W3 |
| O12-3 | 12 | Session lifetime, OTP expiry, attempts, rate limits | (a) 12 h sessions, 5 min OTP, 5 attempts, 3 requests per 15 min. | W1 |
| O12-4 | 12 | Confirm roster changes have no API in the MVP | (a) No roster API in the MVP; the roster is loaded by script before the festival. | W3 |
| O12-5 | 12 | Approve the FR-AUT-04 matrix as the production baseline | Approve the FR-AUT-04 matrix as the baseline **including the cells added by O7-3, O7-5 and O8-1**. | W5 |
| O12-6 | 12 | Mobile app repo/contract ownership, TLS/hosting | **OWNER SUPPLIES** the app repo, contract ownership and the TLS or hosting arrangement. | client connection |
| O12-7 | 12 | Retention for sessions/denials/attempts | (a) Keep sessions, denials and attempts as evidence; archive only. | none |

### 7.1 OWNER SUPPLIES items (stop gates, one table)

These cannot be delegated to an agent. The workstream that needs one stops at the gate and
reports; everything not dependent on it continues. O10-4 is owner-confirmed (model tags) but
is not marked OWNER SUPPLIES and is not a hard stop gate.

| ID | Phase that needs it | What the owner must supply | Gate |
|---|---|---|---|
| O9-2 | 9 | Bot token (environment variable only), a test server and channel, Message Content intent enabled in the Discord developer portal; reset the old token first | W5 live smoke only |
| O10-2 | 10 | Labelled set: ≥ 300 masked messages, ≥ 80 RELEVANT, ≥ 60 hard negatives, sender role, expected proposal for relevant ones; owner confirms the assistant's labelling | W4–W7 production report |
| O11-1 | 11 | SMS provider account and credentials before go-live (India: sender/template registration) | W5 live OTP only |
| O11-5 | 11 | The app's deep-link scheme | W1 wording tests may ship with an opaque path; real scheme later |
| O12-6 | 12 | Mobile app repo, contract ownership, TLS/hosting arrangement | before any real client connects |

---

## 8. Recommended order and reasons

1. **Phase 7 first.** It is the only phase that must change the schema window before roster
   data exists (PB-3/PB-5/PB-6), and every later phase consumes its visibility filter,
   authorization recorder and notification-staging helper. The relevance retrofit and
   eligibility default are small, self-contained slices that unblock live data.
2. **Phase 8 second.** Approval decisions act on tasks created by Phase 6 and made live by
   Phase 7's visibility/assignment logic. Doing it before liveness keeps the first
   end-to-end volunteer workflow complete.
3. **Phase 9 third.** The clock/scheduler, replay importer and Discord adapter produce the
   real data that Phases 10 and 12 need. PB-1/PB-2/PB-7 are correctness fixes that must
   land before any live tuning.
4. **Phase 10 fourth, with overlap.** The adapter and harness can be written against
   fixtures while Phase 9 finishes; the benchmark needs replay data. It must precede
   Phase 12's admin evaluation views but not Phase 11.
5. **Phase 11 fifth, in parallel with 10.** Delivery consumes notification rows already
   staged by 6/7/8; it has no dependency on the model. It must precede Phase 12 because
   the API exposes notification lists and the OTP sender.
6. **Phase 12 last.** It aggregates every service behind HTTP, applies the full
   authorization matrix and hierarchy visibility, and needs the framework decision. It is
   the only phase that can strand other work if the framework choice is late.

If only one stream is available, follow 7 → 8 → 9 → 10 → 11 → 12. If two streams are
available, run 9 beside 8 and 11 beside 10, never two schema-writing phases at once.

---

## 9. Document map and checkpoint convention

- `docs/ROADMAP.md` (this file) — authority for order, contracts, migrations, coverage.
- `docs/PHASE7_PLAN.md` … `docs/PHASE12_PLAN.md` — execution plans; each has a
  "checkpoint" table in §12 so a cut-off session can stop on a green tree.
- `docs/prompts/PHASE7_EXECUTE.md` … `PHASE12_EXECUTE.md` — paste-ready execution prompts.
- `docs/PLANNING_PROGRESS.md` — resume anchor; update after every document.

Every checkpoint must leave: working tree with only that phase's intended files, plain
pytest green, ruff/mypy green, migrations applying and re-applying cleanly, and no
staged changes. A checkpoint that cannot meet this is not offered; the plan reverts the
slice instead.

---

## 10. Planning cross-check results

- **Coverage (verified 2026-10-29).** `docs/SRS.md` contains exactly **192** distinct
  requirement IDs (regex over every `FR-/NFR-/DR-/EIR-/AI-/BR-/AC-` token). §6 lists
  exactly 192 rows, 192 distinct, **0 missing, 0 duplicates, 0 extras**. Distribution:
  phases 2–6 = 19/6/12/19/23, phase 7 = 29, phase 8 = 6, phase 9 = 18, phase 10 = 13,
  phase 11 = 10, phase 12 = 19, POST-MVP = 12, OUT OF REPO = 6 (sums to 192).
- **One deliberate correction to an earlier record.** `FR-REV-10` is credited to Phase 11
  (the "shown to its audience" half needs the fan-out), not Phase 6. The Phase 6 record
  still claims FR-REV-10 for the acceptance record and structured audience; nothing in
  committed code changes. This is a documentation-level correction, listed here rather
  than rewritten into the committed Phase 6 record.
- **Matrices use two labels.** A plan row marked "(reused from N)" or "(initial in N)"
  is a regression/contract check, not a second completion of that requirement; the
  completing phase is the one in §6.
- **Known cross-phase splits**, all stated in the plans: FR-UI-02/03/04 (data in 12, UI out
  of repo), NFR-REL-06 (functions in 3/9, admin view in 12), FR-AUT-06 (recorder in 6,
  every route in 12), EIR-03 (request shape in 7, full contract in 10), NFR-CON-02 (accept
  in 6, claims in 7, decision in 8).
- **Contradictions resolved or still the owner's.** Resolved 2026-10-29 by
  `docs/OWNER_ANSWERS.md`: the AI-06 quality targets **do exist** in SRS §7 (recall ≥ 95%,
  precision ≥ 80%, proposal type and assignment mode ≥ 95%, assignee and task type ≥ 90%,
  acceptance ≥ 80%) — an earlier plan statement that none existed was wrong and is
  corrected in `PHASE10_PLAN.md`; OTP delivery is SMS to the roster phone (FR-ROS-03, with
  FCM for push, EIR-04) — both channels, not a contradiction; FR-TSK-08 vs FR-TSK-16 is
  settled as creator + Head of the responsible vertical + Admin (O7-4); the Admin work-status
  right is settled as an Admin override (O7-5); roster edits have no API in the MVP (O12-4).
  Still outstanding for the owner: the 30 s P95 target vs a 17–32 s bucket wait (O10-6 keeps
  the target and documents the wait; the owner later edits the SRS to measure from bucket
  close); FR-UI mobile app out of repo; roll-number sign-in blocked when a roster phone is
  missing (PB-5 makes phone NOT NULL, which removes the fallback question but not the
  operational one).
- Owner inputs are consolidated in §7 (34 items, O7-1 … O12-7), each with its answer from
  `docs/OWNER_ANSWERS.md`; §7.1 lists the five OWNER SUPPLIES stop gates by phase. Each
  phase plan repeats only its own items, and each execution prompt carries them with the
  answer filled in.
- Migration numbering 0016–0024 is stable and disjoint; Phase 8 adds no migration.
