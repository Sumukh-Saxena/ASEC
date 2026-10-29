# ASEC Phase 7 Plan — Foundations and Task Lifecycle

Status: planning only, 2026-10-29. No code, migration or test is changed by this
document. Authority: `docs/SRS.md` (requirements), `AGENTS.md` (workflow and
architecture rules), `docs/DECISIONS.md` §A/§B (approved owner decisions),
`docs/ROADMAP.md` (order, contracts, migration numbers). Where this plan and the SRS
disagree, the SRS wins and the disagreement is listed in §10. Storage existence never
implies implemented behaviour; tests-first and the slice workflow apply to every
workstream.

---

## 1. Verified baseline (2026-10-29, read-only; re-verify at execution)

Real output observed at planning time (full command list in `docs/ROADMAP.md` §1):

| Check | Observed |
|---|---|
| HEAD | `fb5263d Phase 6 planning complete` |
| `git status --short` | empty |
| `.venv\Scripts\python.exe -m pytest` | `1016 passed in 165.69s (0:02:45)` |
| `python -m ruff check .` | `All checks passed!` |
| `python -m mypy` | `Success: no issues found in 14 source files` |
| `python -m backend --version` | `0.0.1` |
| migrations | `0001`–`0015`, runner applies each file in one transaction |

Phase 7 starts from these real, present facts (verified by reading the files):

- `0006_tasks.sql`: `task` forces `people_needed = 1` for `EXPLICIT`, keeps
  `responsible_vertical_id` nullable for `INTERNAL`; `task_assignee` is keyed only by
  `(task_id, person_id)`; `task_history`, `task_attribute`, `task_affected_vertical`.
- `0009_announcements.sql`: free-text `audience_scope`, nullable `audience_vertical_id`.
- `0001_core_roster.sql`: `role` free text, `reports_to` free text, `phone`/`roll_number`
  nullable.
- `backend/review.py` materializes exactly one task or announcement on accept, writes
  `task_history` creation rows, and stages notifications with private `_record_denial`,
  `_require_active_actor` and `_insert_notification`.
- `backend/proposals.py`: `require_source_vertical_membership = False` by default, pinned
  by `test_membership_flag_off_is_the_phase5_default`.
- `backend/relevance.py` still sends real Discord data (`group_discord_channel_id`,
  `sender_discord_user_id`, raw `reply_reference`, real mention IDs); `proposals.py`
  already rewrites markup with `@m1…` handles.
- No `backend/tasks.py`, `authorization.py` or `notification_events.py`; no task
  operation beyond accept materialization. `task_assignee` rows exist for `PENDING`.

---

## 2. Scope, non-goals, and boundary

### 2.1 In scope (this phase)

| # | Slice | Requirements |
|---|---|---|
| S1 | Relevance request retrofit: opaque handles, markup rewrite, internal reply IDs, no Discord IDs in the request | EIR-03, NFR-SEC-04, FR-REL-01, AC-21, DR-03 |
| S2 | Eligibility default ON (sender must belong to the group's vertical) | FR-PRP-02, FR-AUT-08, AC-10 |
| S3 | Schema window 0016 (PB-5), 0017 (PB-3), 0018 (PB-6), with `review.py` follow-through | FR-TSK-01/02/03/09, FR-REV-10, FR-ROS-01/02 |
| S4 | Shared `authorization.py` and `notification_events.py` extracted from `review.py` without behaviour change | FR-AUT-01/06, FR-NOT-05, NFR-REL-01 |
| S5 | Single visibility filter and task read model (snapshot, progress roll-up) | FR-TSK-01/02/07, NFR-SEC-03, NFR-CON-02 |
| S6 | Task lifecycle writes: claim, agreement, work status, edit, reassign, cancel, history, notifications | FR-TSK-03..16, AC-08/09/16/19, BR-02/03/09 |
| S7 | Mutation battery and DoD (§7, §11) | NFR-MNT-04, NFR-AUD-03 |

### 2.2 Explicit non-goals

- No cross-vertical decision logic, `approval_state` transitions or liveness — Phase 8.
- No clock/scheduler, replay importer, Discord adapter, PB-1/PB-2/PB-7 — Phase 9.
- No AI HTTP adapter, evaluation harness, benchmark — Phase 10.
- No delivery worker, push, OTP — Phase 11; Phase 7 only stages `notification` rows.
- No HTTP API, sessions, dashboards, roster import UI — Phase 12; no mobile app code.
- No new dependency, framework, queue, vector store or agent.
- No SRS edit and no re-opening of `DECISIONS.md` §A/§B.

### 2.3 Boundary rule

Phase 6 owns accept materialization and review; Phase 7 owns everything that happens to
an accepted task afterwards. Phase 8 changes `approval_state`; Phase 7 never does.
Phase 11 delivers rows; Phase 7 never sends anything.

---

## 3. Reuse of real names (no parallel vocabulary)

| Existing name | Where | Phase 7 use |
|---|---|---|
| `connect`, `transaction` | `backend/db/connection.py` | same entry points; `BEGIN IMMEDIATE` for every write |
| `apply_migrations`, `MIGRATIONS_DIR` | `backend/db/migrations.py` | 0016–0018 |
| `Config.get/require` | `backend/config.py` | no new config surface in this phase |
| `ReviewPolicy`, `load_review_policy` | `backend/review.py` | unchanged; extraction keeps call signature |
| `_record_denial`, `_require_active_actor` | `backend/review.py` | move to `backend/authorization.py`; private wrappers stay |
| `_insert_notification`, event constants | `backend/review.py` | move to `backend/notification_events.py`; private wrapper stays |
| `mask_pii` | `backend/masking.py` | unchanged in relevance requests |
| `ProposalPolicy`, `load_proposal_policy` | `backend/proposals.py` | flip one default, keep dataclass fields |
| `RelevanceRequest`, `ContextMessage`, `RelevanceClassifier` | `backend/relevance.py` | redefine request fields in place (S1) |
| `task`, `task_assignee`, `task_history`, `task_attribute`, `task_affected_vertical` | `0006_tasks.sql` | rebuilt by 0017, same table names |
| `notification`, `notification_attempt` | `0008_notifications.sql` | unchanged; `event_type` is free text |
| `authorization_denial` | `0004_processing_attempts.sql` | unchanged; shared recorder writes it |
| `ProposalEdit`, `accept_proposal` | `backend/review.py` | unchanged except PB-3/PB-6 follow-through |

New modules are additive and import only downward: `authorization.py` and
`notification_events.py` import `db.connection` only; `tasks.py` imports
`authorization`, `notification_events` and `db.connection`.

---

## 4. Design

### 4.1 S1 — Relevance request retrofit (EIR-03, NFR-SEC-04)

The backend already rewrites markup for proposals (`_bucket_entries`, C1/DC-18/DC-19).
Phase 7 makes one shared, tested helper and uses it in both stages. New
`backend/ai_context.py`:

```python
class MentionHandles:
    def handle(self, discord_user_id: str) -> str   # "@m1", first-appearance order
    @property
    def mapping(self) -> tuple[tuple[str, str], ...]  # (handle, discord_user_id)

def rewrite_markup(content: str, handles: MentionHandles) -> str
def reply_to_message_id(raw_reply: str | None, *, group_id: int,
                        bucket_message_ids: frozenset[int]) -> int | None
def masked_request_content(content: str | None, handles: MentionHandles) -> str | None
```

Rules, pinned by tests:

1. `mask_pii` runs on the request copy only; stored `content` is never written (DR-03,
   AC-21). `masked_request_content` masks first, rewrites second.
2. Real markup `<@ID>`, `<@!ID>`, `<@&ID>`, `<#ID>`, `<:name:ID>`, `<a:name:ID>` is
   replaced: user forms become `@mN`; role/channel/emoji forms become `@role`,
   `#channel`, `:emoji:` with no ID. No raw Discord ID survives.
3. Handles are assigned in first-appearance order scanning the bucket by ascending
   message id, stored mentions first (position, then row id), then content mentions —
   exactly Phase 5's algorithm, now shared.
4. `ContextMessage` becomes `message_id`, `sender_person_id`, `sender_name`,
   `event_timestamp`, `reply_to_message_id`, `masked_content`, `mention_handles`
   (opaque); `sender_discord_user_id` is removed. `RelevanceRequest` keeps `bucket_id`,
   `vertical_name`, `instructions`, `messages`, replaces `group_discord_channel_id` with
   `group_handle` (`f"g{group_id}"`), and adds `mention_handle_map`.
5. The adapter sees handles only; the backend keeps the map. Relevance decisions stay
   keyed by internal `message_id`.
6. Proposals switch to the shared helper with byte-identical output; the Phase 5 suite
   is the regression proof. A malformed/absent context still raises `UnknownBucketError`.

### 4.2 S2 — Eligibility default ON (FR-PRP-02)

`ProposalPolicy.require_source_vertical_membership` default becomes `True`.
`PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP=0` still disables it (config escape hatch
stays). `_eligible_person_subquery` already implements membership; no query change.
Pinned tests updated:

- `test_membership_flag_off_is_the_phase5_default` → renamed to express "default ON",
  asserting no proposal for an outside-vertical AH sender; a second case with `"0"`
  keeps the old expectation.
- `test_membership_flag_on_requires_source_vertical_membership` → unchanged.
- Any test that builds an eligible sender without `person_vertical` must add the
  membership row or pass `"0"` explicitly; the failure mode is the regression signal.

Unmapped groups (`vertical_id NULL`) stay ineligible (existing
`test_unmapped_group_is_never_eligible`), per DECISIONS "Heads-and-AHs-only groups stay
unmapped until a design for them exists".

### 4.3 S3 — Schema window (0016, 0017, 0018)

Window rule: this is the **pre-roster** window. If a pre-window row violates a new
constraint, the migration aborts and the owner decides; the migration never invents
values. SQL is in §5. `PRAGMA defer_foreign_keys = ON` is required because the runner
opens `foreign_keys = ON` and one transaction per file; it is set inside the file and
released at commit.

Follow-through in `backend/review.py` (the only code that writes these tables today):

| Current code | Change | Reason |
|---|---|---|
| `_people_needed_for_task` returns `(1, 0)` for `EXPLICIT` | returns `(None, 0)` | PB-3; helper exists exactly for this |
| task insert sets `responsible_vertical_id` only for cross-vertical | set `= source_vertical_id` for `INTERNAL` | PB-3 NOT NULL + FR-TSK-09 |
| announcement insert writes `audience_vertical_id, audience_scope` | writes `audience_type, audience_vertical_id, audience_ah_person_id` | PB-6 |
| `_validate_eligibility`, acceptance gates | unchanged | Phase 6 behaviour stays |
| assignee insert `(task_id, person_id)` | unchanged columns; surrogate id is automatic | 0017 |

Pinned Phase 6 tests to update (execution authorization is an OWNER item, §9):
`test_migrations_tasks.py`, `test_migrations_announcements.py`,
`test_review_service.py`, `test_migrations_proposals.py`, plus every helper that inserts
a `person` without `phone`/`roll_number` (real occurrences: at least
`test_ingestion_service.py`, `test_migrations_approvals.py`,
`test_migrations_latency.py`, `test_migrations_notifications.py`,
`test_migrations_processing_attempts.py`, `test_migrations_resolution.py`,
`test_migrations_review_revisions.py`, `test_migrations_source_messages.py`,
`test_migrations_tasks.py`, `test_proposal_service.py`, `test_relevance_service.py`,
`test_review_service.py`). Synthetic values only.

Role CHECK vs Phase 6's case-insensitive Admin token: 0016 stores canonical
`Admin/Head/AH/Volunteer`; the test that writes `role = 'aDmIn'` must prove
case-insensitive **policy** matching without bypassing the CHECK (OWNER item O7-6).

### 4.4 S4 — Shared authorization and notification staging

`backend/authorization.py` (exact move, no new semantics):

```python
class AuthorizationError(Exception): ...
def record_denial(connection, *, actor_person_id, operation,
                  subject_type, subject_id, reason) -> None
def require_active_actor(connection, actor_person_id, *, operation,
                         subject_type, subject_id) -> sqlite3.Row
```

`record_denial` opens its own `transaction`, resolves an unknown actor to `NULL`, and is
called **after** a rejected write transaction has rolled back and **before** the caller's
exception is re-raised. `review.py` keeps `_record_denial` and `_require_active_actor` as
thin aliases so Phase 6 tests and call sites do not move. FR-AUT-06 stays recorded for
every service, not duplicated per module.

`backend/notification_events.py`:

```python
def stage_notification(connection, *, event_type, recipient_person_id,
                       dedupe_key, created_at, proposal_id=None, task_id=None,
                       approval_request_id=None, announcement_id=None) -> bool
def proposal_ready_key(proposal_id) -> str
def task_assignee_key(event_type, task_id, person_id) -> str
def approval_request_key(request_id, person_id) -> str
```

`stage_notification` is the moved `_insert_notification` (`ON CONFLICT (dedupe_key) DO
NOTHING`, returns `rowcount == 1`). `review.py` keeps `_insert_notification` as an alias.
No event-type CHECK is added: `0008` deliberately stores free text, and FR-NOT-01 is
prose. New task event labels: `ASSIGNEE_ACCEPTED`, `ASSIGNEE_DENIED`,
`WORK_COMPLETED`, `TASK_EDITED`, `TASK_REASSIGNED`, `TASK_CANCELLED` — each with dedupe
key `<event>:<task_id>:<person_id>`.

### 4.5 S5 — Single visibility filter and read model

```python
LIVE_APPROVAL_STATES = ("NOT_REQUIRED", "APPROVED")

def visible_tasks(connection, *, viewer_person_id: int,
                  include_cancelled: bool = False,
                  include_hidden: bool = False) -> tuple[TaskSummary, ...]
```

Authorization is derived **inside** the function from `person` and `person_vertical`
(never passed in by a caller, so an API layer cannot spoof it):

- missing/inactive viewer → `record_denial` + `TaskAuthorizationError`.
- `include_hidden=True` requires current role `Admin` (case-insensitive compare); any
  other caller is denied and recorded. This is the only **task** query that may include
  `PENDING`/`DENIED` rows; the Phase 8 approval surface reads `approval_request` directly
  and exposes only the task description and requester note (NFR-SEC-03(c)), never the
  hidden task row.
- base predicate: `approval_state IN ('NOT_REQUIRED','APPROVED')`; `cancellation =
  'ACTIVE'` unless `include_cancelled`.
- scope: Admin → all live tasks; Head → tasks whose `source_vertical_id` or
  `responsible_vertical_id` is one of the Head's verticals, plus tasks they created;
  AH → tasks they created; Volunteer → tasks assigned to them plus live `OPEN` tasks in
  their verticals (FR-TSK-04/FR-UI-02).

`_live_task_row(connection, task_id)` is the single row loader used by **every** mutator
and read and applies the same predicate; a hidden ID passed directly behaves as not
found and is never acted on. This closes the Phase 6 leak of `task_assignee` rows for
`PENDING` tasks.

Read model: `TaskSummary` (id, type, source/responsible vertical, creator,
assignment_mode, people_needed, unlimited flag, cancellation, version, roll-up) and
`task_progress(connection, task_id, *, viewer_person_id)` returning per-assignee
`(person_id, name, agreement, work_status, cancelled)` plus
`accepted_count/completed_count/people_needed` ("3 of 5 completed", FR-TSK-07). Matrix
row 10 governs progress: Admin all; Head own vertical; AH tasks they created; Volunteer
own tasks only.

### 4.6 S6 — Task lifecycle writes

All writes: `require_active_actor` → `_live_task_row` → authority check → one
`transaction`. Any denial is recorded after rollback (helper
`_deny_then_raise(connection, ...)`); staleness history is recorded in its own committed
transaction before the stale error is raised.

**Constants** (SRS tokens verbatim): agreements `AWAITING/ACCEPTED/DENIED`; work
statuses `NOT_STARTED/IN_PROGRESS/COMPLETED`; cancellation `ACTIVE/CANCELLED`.

**Errors** (`TaskError` base): `TaskNotFoundError`, `TaskAuthorizationError`,
`TaskStateError(code)`, `StaleTaskVersionError(expected, current, snapshot)`,
`TaskCapacityError`, `InvalidAgreementError`.

| Operation | Authority (matrix row) | Transaction | History | Notification |
|---|---|---|---|---|
| `claim_open_task(task_id, actor)` | Volunteer, active, member of `responsible_vertical_id`; task `OPEN`, live, active | `BEGIN IMMEDIATE`; read count of `ACCEPTED AND cancelled=0`; full → `TaskCapacityError`; insert `ACCEPTED`, `agreed_at=now` | `field='agreement'`, old `NULL`, new `ACCEPTED`, reason `CLAIM` | `ASSIGNEE_ACCEPTED:<task>:<person>` → creator |
| `set_agreement(task_id, actor, assignee_person_id, agreement)` | the assignee themself; `AWAITING` only; target `ACCEPTED`/`DENIED` | one write; update row + `agreed_at` | `field='agreement'`, old `AWAITING`, new value, reason `SELF_ACTION` | `ASSIGNEE_ACCEPTED`/`ASSIGNEE_DENIED` → creator |
| `update_work_status(task_id, actor, status)` | assignee themself or Admin (FR-TSK-06 override); row must be `ACCEPTED`, `cancelled=0` | one write | `field='work_status'`, old → new, reason `STATUS_UPDATE` | `WORK_COMPLETED` → creator when new = `COMPLETED` |
| `edit_task(task_id, actor, expected_version, changes)` | creator, Head of responsible vertical, Admin (FR-TSK-16) | `BEGIN IMMEDIATE`; compare `version`; stale → own-transaction history row then raise | one row per changed field | `TASK_EDITED` → active assignees |
| `reassign_task(task_id, actor, expected_version, add, remove)` | same as edit | one transaction; remove sets `cancelled=1`; add inserts `AWAITING` | `field='assignee'` rows with person id | `TASK_REASSIGNED` → affected |
| `cancel_task(task_id, actor, reason)` | creator, Head of responsible vertical, Admin (see §10) | one transaction; `cancellation='CANCELLED'`, `cancelled=1` on every non-`COMPLETED` active row | `field='cancellation'`, old `ACTIVE`, new `CANCELLED`, reason | `TASK_CANCELLED` → each cancelled assignee |

Transition rules enforced in code (FR-TSK-13): agreement only `AWAITING → ACCEPTED` or
`AWAITING → DENIED`; work status only `NOT_STARTED → IN_PROGRESS → COMPLETED`, same-value
is `TaskStateError("NO_CHANGE")`, `COMPLETED` is final, `NOT_STARTED → COMPLETED` is
rejected. A denied row is terminal and frees the place; PB-3's partial unique index
allows the same person to be added again later. `EXPLICIT` tasks get one `AWAITING` row
per named person at accept (Phase 6); Phase 7 does not expand that.

`edit_task` fields: `description` (non-blank, trimmed), `people_needed` (OPEN only,
not unlimited, `>= accepted_count`), `attributes` (replace set, keys unique). Task type,
source/responsible vertical and assignment mode are immutable here; changing them is a
new proposal/approval, not an edit. `version` increments on edit/reassign/cancel only;
assignee dimensions stay independent (FR-TSK-02), and claims do not bump `version`.

`task_progress` and every mutator reject a task that is `PENDING`, `DENIED` or
`CANCELLED` (except cancel itself, which accepts `ACTIVE` only) through
`_live_task_row`; this is the single filter that Phases 8 and 12 must reuse.

**Concurrency.** `transaction()` starts `BEGIN IMMEDIATE`; capacity, version and claim
checks happen inside it. Two threads claiming the last place serialize; the second gets
`TaskCapacityError("task is full")` (AC-08). Two stale edits serialize on `version`; the
loser gets `StaleTaskVersionError` with the current snapshot and a history row
(FR-TSK-12, AC-19). No network call is inside the transaction; notification rows are
staged in the same transaction and delivered later.

**Security and audit.** The service never reads content to decide authority (FR-AUT-08);
`actor_person_id` is the only identity input. `task_history` stores actor role and
vertical at the time (FR-AUT-05, FR-TSK-11) and is append-only (NFR-AUD-03). Deactivated
persons are blocked from new claims/reassignment (FR-AUT-07) while attribution stays.

---

## 5. Exact migration SQL (planned, full files)

### 5.1 `0016_roster_integrity.sql` (PB-5)

```sql
PRAGMA defer_foreign_keys = ON;
CREATE TABLE person_new (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, discord_user_id TEXT NULL,
    phone TEXT NOT NULL, role TEXT NOT NULL,
    reports_to INTEGER NULL REFERENCES person_new (id) ON DELETE RESTRICT,
    active INTEGER NOT NULL DEFAULT 1, roll_number TEXT NOT NULL,
    CHECK (length(trim(name)) > 0),
    CHECK (role IN ('Admin', 'Head', 'AH', 'Volunteer')),
    CHECK (discord_user_id IS NULL OR length(trim(discord_user_id)) > 0),
    CHECK (length(trim(phone)) > 0),
    CHECK (length(trim(roll_number)) > 0),
    CHECK (active IN (0, 1)),
    UNIQUE (discord_user_id),
    UNIQUE (phone),
    UNIQUE (roll_number)
);
INSERT INTO person_new (id, name, discord_user_id, phone, role, reports_to, active, roll_number)
SELECT id, name, discord_user_id, phone, role,
       CASE WHEN reports_to IS NULL OR reports_to = '' THEN NULL
            WHEN EXISTS (SELECT 1 FROM person p2 WHERE CAST(p2.id AS TEXT) = person.reports_to)
            THEN CAST(reports_to AS INTEGER) ELSE NULL END,
       active, roll_number
FROM person;
DROP TABLE person;
ALTER TABLE person_new RENAME TO person;
```

The `CASE` only accepts a numeric reports-to that names an existing person; any other
non-empty value must abort instead of silently becoming `NULL` (the file will raise
`RAISE(ABORT, ...)` via a guard `SELECT` before the insert — implemented as a
`SELECT CASE WHEN ... THEN RAISE(ABORT, 'reports_to must be a person id') END`). The
same guard pattern aborts on any `NULL` phone/roll_number, unknown role or self-report.

### 5.2 `0017_task_schema.sql` (PB-3)

```sql
PRAGMA defer_foreign_keys = ON;
CREATE TABLE task_new (
    id INTEGER PRIMARY KEY, proposal_id INTEGER NOT NULL UNIQUE,
    task_type TEXT NOT NULL, source_vertical_id INTEGER NOT NULL,
    responsible_vertical_id INTEGER NOT NULL, creator_id INTEGER NOT NULL,
    description TEXT NOT NULL, assignment_mode TEXT NOT NULL,
    people_needed INTEGER, people_needed_unlimited INTEGER NOT NULL DEFAULT 0,
    approval_state TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
    cancellation TEXT NOT NULL DEFAULT 'ACTIVE',
    version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
    CHECK (task_type IN ('INTERNAL', 'CROSS_VERTICAL')),
    CHECK (assignment_mode IN ('EXPLICIT', 'OPEN')),
    CHECK (approval_state IN ('NOT_REQUIRED', 'PENDING', 'APPROVED', 'DENIED')),
    CHECK (cancellation IN ('ACTIVE', 'CANCELLED')),
    CHECK ((task_type = 'INTERNAL' AND responsible_vertical_id = source_vertical_id)
        OR (task_type = 'CROSS_VERTICAL' AND responsible_vertical_id != source_vertical_id)),
    CHECK (people_needed IS NULL OR people_needed > 0),
    CHECK (people_needed_unlimited IN (0, 1)),
    CHECK ((people_needed IS NULL) != (people_needed_unlimited = 1)),
    CHECK (assignment_mode != 'EXPLICIT' OR people_needed IS NULL),
    CHECK (version >= 1),
    CHECK (length(trim(description)) > 0),
    CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (proposal_id) REFERENCES proposal (id) ON DELETE RESTRICT,
    FOREIGN KEY (source_vertical_id) REFERENCES vertical (id) ON DELETE RESTRICT,
    FOREIGN KEY (responsible_vertical_id) REFERENCES vertical (id) ON DELETE RESTRICT,
    FOREIGN KEY (creator_id) REFERENCES person (id) ON DELETE RESTRICT
);
INSERT INTO task_new (id, proposal_id, task_type, source_vertical_id,
                      responsible_vertical_id, creator_id, description,
                      assignment_mode, people_needed, people_needed_unlimited,
                      approval_state, cancellation, version, created_at)
SELECT id, proposal_id, task_type, source_vertical_id,
       CASE WHEN task_type = 'INTERNAL' THEN source_vertical_id
            ELSE responsible_vertical_id END,
       creator_id, description, assignment_mode,
       CASE WHEN assignment_mode = 'EXPLICIT' THEN NULL ELSE people_needed END,
       people_needed_unlimited, approval_state, cancellation, version, created_at
FROM task;
CREATE TABLE task_assignee_new (
    id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, person_id INTEGER NOT NULL,
    agreement TEXT NOT NULL DEFAULT 'AWAITING', agreed_at TEXT,
    work_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
    cancelled INTEGER NOT NULL DEFAULT 0, added_at TEXT NOT NULL,
    CHECK (agreement IN ('AWAITING', 'ACCEPTED', 'DENIED')),
    CHECK (work_status IN ('NOT_STARTED', 'IN_PROGRESS', 'COMPLETED')),
    CHECK (cancelled IN (0, 1)),
    CHECK (agreement = 'AWAITING' OR (agreed_at IS NOT NULL AND length(trim(agreed_at)) > 0)),
    CHECK (agreement != 'AWAITING' OR agreed_at IS NULL),
    CHECK (length(trim(added_at)) > 0),
    FOREIGN KEY (task_id) REFERENCES task_new (id) ON DELETE RESTRICT,
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT
);
INSERT INTO task_assignee_new (task_id, person_id, agreement, agreed_at,
                               work_status, cancelled, added_at)
SELECT task_id, person_id, agreement, agreed_at, work_status, cancelled, added_at
FROM task_assignee;
CREATE UNIQUE INDEX ux_task_assignee_active
    ON task_assignee_new (task_id, person_id)
    WHERE agreement != 'DENIED' AND cancelled = 0;
DROP TABLE task_assignee;
DROP TABLE task;
ALTER TABLE task_new RENAME TO task;
ALTER TABLE task_assignee_new RENAME TO task_assignee;
```

`task_history`, `task_attribute`, `task_affected_vertical` keep their shape and their
FKs to `task`; after the rename they resolve to the new table. Copy guards abort on any
`CROSS_VERTICAL` row with `NULL`/equal responsible vertical, any duplicate active
assignee pair, and any `EXPLICIT` row whose `people_needed` is not 1.

### 5.3 `0018_announcement_audience.sql` (PB-6)

```sql
PRAGMA defer_foreign_keys = ON;
CREATE TABLE announcement_new (
    id INTEGER PRIMARY KEY, proposal_id INTEGER NOT NULL,
    accepted_by INTEGER NOT NULL, accepted_at TEXT NOT NULL,
    audience_type TEXT NOT NULL, audience_vertical_id INTEGER NOT NULL,
    audience_ah_person_id INTEGER NULL,
    CHECK (audience_type IN ('VERTICAL', 'AH_VOLUNTEERS')),
    CHECK (audience_type != 'AH_VOLUNTEERS' OR audience_ah_person_id IS NOT NULL),
    CHECK (length(trim(accepted_at)) > 0),
    UNIQUE (proposal_id),
    FOREIGN KEY (proposal_id) REFERENCES proposal (id) ON DELETE RESTRICT,
    FOREIGN KEY (accepted_by) REFERENCES person (id) ON DELETE RESTRICT,
    FOREIGN KEY (audience_vertical_id) REFERENCES vertical (id) ON DELETE RESTRICT,
    FOREIGN KEY (audience_ah_person_id) REFERENCES person (id) ON DELETE RESTRICT
);
INSERT INTO announcement_new (id, proposal_id, accepted_by, accepted_at,
                              audience_type, audience_vertical_id,
                              audience_ah_person_id)
SELECT a.id, a.proposal_id, a.accepted_by, a.accepted_at, a.audience_scope,
       a.audience_vertical_id,
       CASE WHEN a.audience_scope = 'AH_VOLUNTEERS'
            THEN m.sender_person_id ELSE NULL END
FROM announcement AS a
JOIN proposal AS p ON p.id = a.proposal_id
JOIN source_message AS m ON m.id = p.primary_message_id;
DROP TABLE announcement;
ALTER TABLE announcement_new RENAME TO announcement;
```

Guards abort on any scope other than the two Phase 6 tokens, any `NULL`
`audience_vertical_id`, and any `AH_VOLUNTEERS` row whose primary sender is `NULL`
(delivery would have no audience). Phase 6 always writes one of the two tokens.

---

## 6. Traceability matrix

Completed IDs are exactly those the roadmap assigns to Phase 7 (roadmap §6, with
`FR-PRP-02` moved to 7 because the approved default changes behaviour here).

| Requirement(s) | Where implemented | Test |
|---|---|---|
| FR-TSK-01/02/09, BR-02 | 0017 columns, `TaskSummary`, INTERNAL responsible = source | migration + task read tests |
| FR-TSK-03/05/13, BR-03/09, AC-09 | `set_agreement`, `AWAITING` only, terminal denial frees the place | agreement/transition tests |
| FR-TSK-04, AC-08, NFR-CON-02 | `claim_open_task` capacity + one winner | capacity/two-thread tests |
| FR-TSK-06/07, AC-16 | `update_work_status`, `task_progress` roll-up, completion notice | status/progress tests |
| FR-TSK-08 | `cancel_task` + cancelled assignee notices | cancel tests |
| FR-TSK-10/16 | role authority, edit/reassign, never authorship | authorization tests |
| FR-TSK-11/12, AC-19, NFR-AUD-03 | history rows, version compare, stale history | history/stale tests |
| FR-TSK-14/15 | no AI entry point, no merge/bucket link | import/audit test |
| FR-REL-01 | S1 request retrofit; classifier input unchanged in meaning | relevance suite + handle tests |
| EIR-03 (request-shape half; full contract in 10) | handles-only relevance request | request-shape test |
| NFR-SEC-04 | masked request copies, no Discord ids | masking/handle tests |
| AC-21, DR-03 (regression; completed in 4/2) | stored content unchanged, masks applied | copy test |
| FR-PRP-02 | S2 default ON, unmapped groups stay ineligible | proposal eligibility tests |
| NFR-MNT-04 | every task test runs without a model | suite composition |

Not completed here (owned elsewhere): FR-AUT-01/02/03/04/06/08 (Phase 12 consolidates;
Phase 7 uses the extracted recorder), FR-XV-04..06 (8), FR-NOT-01..07 delivery (11),
FR-ROS-01/02 import (12), FR-UI-* (out of repo).

---

## 7. Tests-first and mutation battery

Write or extend these tests **before** the matching implementation. Test helpers use
synthetic names and synthetic phone/roll values only.

| File | Covers |
|---|---|
| `tests/test_migrations_roster_integrity.py` (new) | 0016 happy path, NULL phone/roll abort, bad role abort, reports_to non-id abort, FK integrity after rebuild |
| `tests/test_migrations_task_schema.py` (new) | 0017 copy rules, INTERNAL responsible = source, EXPLICIT `people_needed NULL`, partial unique index allows re-add after denial and blocks duplicate active |
| `tests/test_migrations_announcement_audience.py` (new) | 0018 token mapping, AH sender snapshot, abort cases |
| `tests/test_authorization.py` (new) | denial survives caller rollback; inactive actor recorded; review alias unchanged |
| `tests/test_notification_events.py` (new) | dedupe idempotency; key formats; review alias unchanged |
| `tests/test_task_service.py` (new) | visibility (hidden PENDING/DENIED, cancelled), scope per role, claim/capacity/race, agreement, status transitions, edit/stale/reassign/cancel, history rows, progress roll-up, notifications staged |
| `tests/test_ai_context.py` (new) | handle order, markup forms, reply mapping, no raw ID |
| `tests/test_relevance_service.py` (extend) | request has handles/group handle, no Discord ID, decisions unchanged |
| `tests/test_proposal_service.py` (extend) | default ON; `"0"` escape; unmapped group |
| `tests/test_review_service.py` (update) | PB-3 `None` encoding; PB-6 audience columns; role canonicalization |

Mutation list (each must turn at least one test red):

1. Delete the `approval_state IN (...)` clause in `_live_task_row` → hidden-task test red.
2. Delete the `cancellation = 'ACTIVE'` clause in `visible_tasks` → cancelled-task test red.
3. Change claim capacity from `>= people_needed` to `> people_needed` → sixth-claim test red.
4. Move the claim count read outside `BEGIN IMMEDIATE` → two-thread race test red.
5. Allow `COMPLETED → IN_PROGRESS` → final-status test red.
6. Remove the `version` comparison in `edit_task` → stale-edit test red.
7. Remove `record_denial` before raising → denial-persistence test red.
8. Revert eligibility default to `False` → default-ON test red.
9. Let `rewrite_markup` pass an unmapped `<@ID>` through → no-raw-ID test red.
10. Map `AH_VOLUNTEERS` to the accepting Admin in 0018 → audience test red.

---

## 8. Workstreams (one implementation batch, internal order)

| WS | Content | Stop point |
|---|---|---|
| W1 | S1 shared context helper + relevance retrofit; proposals switched, Phase 5 suite green | C1 |
| W2 | S2 eligibility default + pinned test updates | C2 |
| W3 | 0016, 0017, 0018 + `review.py` follow-through + all person/task/announcement fixtures | C3 |
| W4 | `authorization.py`, `notification_events.py` extraction, aliases, regression suite | C4 |
| W5 | `tasks.py` read model: `visible_tasks`, `_live_task_row`, `task_progress` | C5 |
| W6 | `tasks.py` writes: claim, agreement, status, edit, reassign, cancel | C5 |
| W7 | mutation battery, audit greps, DoD battery, §15 record | final |

No WS may start before the previous stop point is green. W3 is the only schema write;
no other branch may write migrations while it runs. W4 must be behaviour-neutral:
the Phase 6 suite is run before and after with the same count.

---

## 9. OWNER decisions for this phase

| ID | Question | Options | Recommendation |
|---|---|---|---|
| O7-1 | Confirm the schema window runs before any roster load and that no real roster rows must survive | (a) confirm pre-roster window; (b) allow a data-mapping script | (a). If real rows exist, stop and map deliberately |
| O7-2 | Authorize the pinned Phase 6 test updates that PB-3/PB-6 force | (a) authorize mechanical updates; (b) keep old expectations and delay PB-3/PB-6 | (a). The decisions already exist; this is execution authorization |
| O7-3 | May an authorized user accept/deny **on behalf of** an assignee (FR-TSK-05)? The matrix grants no cell | (a) Admin only, recorded in history; (b) creator; (c) self-action only in MVP | (c) for implementation now, recorded as a gap; choose (a) if the owner wants the FR-TSK-05 sentence fully exercised |
| O7-4 | Who may cancel: FR-TSK-08 says creator; FR-TSK-16/matrix add Head/Admin | (a) creator + Head of responsible vertical + Admin; (b) creator only | (a), consistent with the FR-AUT-04 baseline matrix; confirm |
| O7-5 | Admin work-status override (FR-TSK-06 says Admin; matrix cell is "—") | (a) Admin override; (b) assignee only | (a), per FR-TSK-06; confirm |
| O7-6 | Role CHECK vs the Phase 6 case-insensitive Admin test | (a) strict CHECK + update the test to prove policy matching; (b) case-insensitive CHECK | (a), canonical rows are what FR-ROS-01 loads |
| O7-7 | Optional test-typing cleanup (89 pre-existing errors) | (a) defer; (b) separate slice | (a) defer; mypy gate stays backend-only |

Nothing above re-opens `DECISIONS.md` §A/§B.

---

## 10. SRS ambiguities carried by this phase

1. **Cancel authority** — FR-TSK-08 names the creator; FR-TSK-16 and the matrix give
   Head/Admin edit/cancel rights. Plan follows the matrix; owner confirms (O7-4).
2. **Admin work status** — FR-TSK-06 grants Admin; the matrix cell is "—" (O7-5).
3. **On-behalf agreement** — FR-TSK-05 allows it; no matrix cell authorizes it (O7-3).
4. **Creator identity** — FR-TSK-01 says "the reviewer who accepted"; OD-REV-6 pins the
   primary sender. Phase 7 reads `task.creator_id` as stored; no reinterpretation.
5. **FR-TSK-01 "people needed (open tasks)"** vs PB-3's `NULL` encoding for EXPLICIT:
   the SRS field list does not forbid `NULL`; PB-3 is the approved encoding.
6. **Claim notification** — FR-NOT-01 lists "assignee accepted or denied → creator"; a
   claim is an acceptance, so the plan notifies the creator. Not explicitly stated.
7. **Cancelled assignee rows** — DECISIONS §D flags the SRS's missing "cancelled" value;
   PB-3 keeps the separate flag, which is what this plan uses.
8. **Announcement audience** — PB-6 fixes it; FR-REV-10 prose remains vague. No new
   audience type is invented.

---

## 11. Definition-of-done battery (exact commands, run in this order)

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m backend --version
```

Plus targeted runs during the phase:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_migrations_roster_integrity.py tests/test_migrations_task_schema.py tests/test_migrations_announcement_audience.py
.venv\Scripts\python.exe -m pytest tests/test_authorization.py tests/test_notification_events.py
.venv\Scripts\python.exe -m pytest tests/test_task_service.py tests/test_ai_context.py
.venv\Scripts\python.exe -m pytest tests/test_relevance_service.py tests/test_proposal_service.py tests/test_review_service.py
```

Migration re-apply check (must be a test, not a manual claim): a test applies
`apply_migrations` twice on a temp file DB and asserts the second call returns `[]` and
`PRAGMA foreign_key_check` is empty after a populated rebuild.

Read-only audits (no edit) before reporting, all via `Select-String -Path backend`:

- `'sender_discord_user_id|discord_channel_id'` → only `ingest.py`, `proposals.py` and
  `relevance.py` DB reads, never a request dataclass.
- `'FROM task'` outside `tasks.py`/`review.py` → no ad-hoc task query.
- `'mask_pii|content'` in `tasks.py` → no content read.
- `git status --short` → only intended files; nothing staged.

Pass means: plain `pytest` green with the new count, ruff and mypy clean, migrations
apply and re-apply, all ten mutations observed red, audits clean.

---

## 12. Risks and checkpoints

| Risk | Mitigation |
|---|---|
| Person rebuild breaks dozens of fixtures | W3 isolated; one helper per test file; full suite before proceeding |
| Deferred FKs hide a broken rebuild | `PRAGMA foreign_key_check` after a populated round-trip |
| Hidden-task leakage returns | one `_live_task_row` + mutation 1; repeated in 8/12 prompts |
| Extraction changes Phase 6 behaviour | aliases kept; suite before/after, no SQL edits |
| Stale-edit history lost on rollback | history in its own transaction before raising |
| Race test flakiness | threads + barrier + `busy_timeout` 5000; exactly one success |
| Announcement AH snapshot missing | 0018 guard aborts instead of a NULL audience |

| Checkpoint | Green state |
|---|---|
| C1 | relevance retrofit + shared helper; full suite green; no Discord ID in request dataclasses |
| C2 | eligibility ON; full suite green |
| C3 | 0016–0018 apply and re-apply; `review.py` follow-through; full suite green |
| C4 | extraction behaviour-neutral; identical test count before/after |
| C5 | task service read+write complete; full suite, ruff, mypy green |
| Final | mutation battery + audits + DoD battery observed; §15 record written |

If any checkpoint cannot be made green, revert that checkpoint's slice to the previous
green state; do not carry a half-applied schema window.

---

## 13. Appendix — verbatim requirements quoted

- **FR-TSK-01** A task shall have: persistent ID, description, task type, source vertical,
responsible vertical, affected verticals, creator, assignment mode, people needed (open
tasks), assignee records, and attributes. The creator is the reviewer who accepted the
proposal. These are separate fields and are not assumed to be the same person or
vertical.
- **FR-TSK-02** Task state shall be held in independent dimensions. No single field shall
encode more than one of them.
- **FR-TSK-03** Creating an EXPLICIT task shall create one assignee record per named
person, each AWAITING. It shall not be ACCEPTED.
- **FR-TSK-04** An OPEN task shall be visible to the active volunteers of its responsible
vertical, who can claim a place. A successful claim creates an assignee record that is
ACCEPTED. Claims stop when the number accepted equals people needed. UNLIMITED never
fills. If two volunteers claim the last place at once, exactly one succeeds and the
other is told the task is full.
- **FR-TSK-05** Agreement is set only by an explicit in-app action by the assignee, or by
an authorized user on the assignee's behalf with that fact recorded. It shall never be
inferred from messages, reactions or silence. A denial notifies the creator and frees the
place; the creator may reassign.
- **FR-TSK-06** Only the assignee (or Admin) changes their work status: NOT_STARTED →
IN_PROGRESS → COMPLETED. COMPLETED is final. The system has no verification step; the
creator verifies outside the system.
- **FR-TSK-07** The creator shall see each volunteer's status on each task, plus a roll-up
(for example "3 of 5 completed").
- **FR-TSK-08** The creator may cancel any task that is not fully completed. Cancelling
marks all non-completed assignee records cancelled and notifies the assignees.
- **FR-TSK-09** The source vertical is the vertical of the message's group. It shall not be
changed to match the responsible vertical.
- **FR-TSK-10** Authority comes from role, not authorship. Being the sender of a message
does not make anyone the owner of a task unless they hold the creator role by accepting
its proposal.
- **FR-TSK-11** Every change to a task shall create an append-only history record: field,
old value, new value, actor (user, or model with version), time, reason or evidence, and
the actor's role and vertical at that time. A changed value shall not overwrite history.
- **FR-TSK-12** Concurrent task edits shall not silently overwrite each other. An edit made
on a stale version shall be rejected with the current values shown, and both attempts
shall appear in the history.
- **FR-TSK-13** The system shall enforce the work-status transitions in FR-TSK-06 and the
agreement transitions AWAITING → ACCEPTED or DENIED.
- **FR-TSK-14** An AI component shall never create, assign, reassign, change the status of,
complete, correct or merge a task.
- **FR-TSK-15** Tasks shall never be merged because of similar wording, a shared bucket or
a shared episode.
- **FR-TSK-16** The creator, the Head of the responsible vertical, and Admin may edit or
reassign a task. Assignees are notified of changes.
- **AC-08** An AH posts "need 5 volunteers at the gate" → OPEN proposal with people
needed 5; after acceptance the vertical's volunteers see it; five claims succeed and a
sixth is told the task is full; two simultaneous claims for the last place give exactly
one winner.
- **AC-09** A name, then "Rahul?", a reaction, then "I'll do it" from another person → All
evidence kept; no acceptance inferred; Rahul's agreement stays AWAITING until Rahul acts
in the app.
- **AC-16** A volunteer completes a task → The creator is notified and sees which volunteer
completed which task; completion is final.
- **AC-19** Stale edit → Rejected with current values shown; both attempts logged.
- **NFR-CON-02** Simultaneous accepts, claims and assignments shall each have exactly one
winner.
- **NFR-SEC-04** Messages contain personal data such as names, phone numbers and ID
details. Before any AI call, phone numbers, email addresses and government-ID-like
numbers shall be masked. The originals stay in the backend only.
- **EIR-03 (excerpt)** Mentions are passed as opaque handles, and the backend maps them
back to people.
