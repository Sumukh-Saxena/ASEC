# ASEC Phase 8 Plan — Cross-Vertical Approval Decisions

Status: planning only, 2026-10-29. No code, migration or test is changed by this
document. Authority: `docs/SRS.md`, `AGENTS.md`, `docs/DECISIONS.md` §A/§B,
`docs/ROADMAP.md`. Phase 7 must be complete and green before this phase starts (§1).
Storage existence never implies behaviour; tests-first applies to every workstream.

---

## 1. Verified baseline (pre-Phase-7 facts, 2026-10-29; re-verify at execution)

| Check | Planned expectation |
|---|---|
| HEAD | a Phase 7 commit on top of `fb5263d` |
| `.venv\Scripts\python.exe -m pytest` | green, count from the Phase 7 record (started at `1016 passed`) |
| `python -m ruff check .` / `python -m mypy` | `All checks passed!` / no issues in `backend` |
| migrations | `0001`–`0018` applied; `0018` last |
| `backend/tasks.py` | present: `visible_tasks`, `_live_task_row`, lifecycle functions |
| `backend/authorization.py`, `backend/notification_events.py` | present and used by `review.py` and `tasks.py` |

Real facts already verified in the repository today:

- `0007_cross_vertical_approvals.sql` exists: `approval_request(task_id UNIQUE)`,
  `approval_recipient(request_id, person_id UNIQUE pair)`,
  `approval_decision(request_id UNIQUE, decision APPROVED|DENIED, decision_message)`.
- `0006_tasks.sql` / Phase 7 `0017` hold `task.approval_state` in
  `NOT_REQUIRED|PENDING|APPROVED|DENIED`; `PENDING` and `DENIED` tasks are hidden by
  Phase 7's filter.
- `backend/review.py` on accept: for an AH cross-vertical task it sets
  `approval_state = 'PENDING'`, inserts `approval_request` + recipient snapshot, blocks
  when the recipient set is empty (`NO_APPROVAL_RECIPIENTS`), and stages
  `CROSS_VERTICAL_REQUEST` rows; for a Head cross-vertical task it sets
  `NOT_REQUIRED` and stages `HEAD_CROSS_VERTICAL_TASK`. It writes only a
  `TASK_CREATED` history row — **no request history row today**.
- `_approval_recipient_ids` selects active `AH`/`Head` rows whose `person_vertical`
  is the **responsible** vertical.
- No `backend/approvals.py` exists; no decision service exists. `approval_decision` is
  written only by tests.

---

## 2. Scope, non-goals, and boundary

### 2.1 In scope

| # | Slice | Requirements |
|---|---|---|
| S1 | `backend/approvals.py` read model: `get_approval_request`, `list_pending_approvals` with recipient-safe fields | FR-XV-03/04, NFR-SEC-03(c), FR-UI-03 |
| S2 | Authorization for decisions: recipient snapshot + current role/vertical, Admin override, denial recording | FR-AUT-01/05/06, FR-XV-04 |
| S3 | `decide_request`: first decision wins, approve flips task live, deny keeps it closed and kept | FR-XV-04/05, AC-15, NFR-CON-02, BR-08 |
| S4 | Requester notification with decision message; live-task notifications on approval | FR-XV-05, FR-NOT-01/05 |
| S5 | Task history for request and decision with actor role/vertical | FR-XV-06, FR-TSK-11 |
| S6 | Phase 6 follow-through: request history row at accept | FR-XV-06 |
| S7 | Tests, mutations, DoD | all above |

### 2.2 Non-goals

- No schema change. `0007` is sufficient (§5).
- No task lifecycle change; Phase 7 owns claims, agreements, statuses, edits, cancel.
  Phase 8 only changes `task.approval_state` and stages notifications.
- No delivery (Phase 11), no HTTP (Phase 12), no AI (Phase 10), no pipeline (Phase 9).
- No new role, audience or approval vocabulary; no second approval round, escalation,
  delegation or "request more information".
- No reopening of `DECISIONS.md` §A/§B. No edit of `docs/SRS.md`.

### 2.3 Boundary rule

Phase 6 creates the request and the hidden task. Phase 8 resolves the request. Phase 7's
visibility filter decides who can then see the task; Phase 8 must not duplicate it.

---

## 3. Reuse of real names

| Existing name | Where | Phase 8 use |
|---|---|---|
| `approval_request`, `approval_recipient`, `approval_decision` | `0007` | read/write as-is |
| `task.approval_state`, `task.version`, `task.creator_id` | `0006`/`0017` | flip state; version untouched (approval is not a task edit) |
| `task_history` | `0006` | request + decision rows |
| `visible_tasks`, `_live_task_row` | `backend/tasks.py` (Phase 7) | no new task query; approval listing uses the request tables |
| `require_active_actor`, `record_denial` | `backend/authorization.py` (Phase 7) | every decision authenticated and denials recorded |
| `stage_notification`, key builders | `backend/notification_events.py` (Phase 7) | all notifications |
| `EVENT_CROSS_VERTICAL_REQUEST`, `EVENT_HEAD_CROSS_VERTICAL_TASK` | `backend/review.py` | unchanged; Phase 8 adds `CROSS_VERTICAL_DECISION` |
| `AcceptanceBlockedError("NO_APPROVAL_RECIPIENTS")` | `backend/review.py` | unchanged |
| `_approval_recipient_ids` | `backend/review.py` | reused for the approval-time volunteer scan target vertical |

New module `backend/approvals.py` imports `authorization`, `notification_events`,
`db.connection`; it must not import `review.py` or `proposals.py`.

---

## 4. Design

### 4.1 States and invariants

| Dimension | Values | Phase 8 transition |
|---|---|---|
| Approval (`task.approval_state`) | `NOT_REQUIRED`, `PENDING`, `APPROVED`, `DENIED` | only `PENDING → APPROVED` or `PENDING → DENIED` |
| Decision (`approval_decision.decision`) | `APPROVED`, `DENIED` | exactly one row per request (`UNIQUE(request_id)`) |
| Cancellation | `ACTIVE`, `CANCELLED` | untouched |
| Assignee agreement / work status | Phase 7 values | untouched |

Invariants: a `PENDING`/`DENIED` task is never visible through `visible_tasks`; a
`DENIED` task is never deleted, cancelled or re-opened by this phase; the decision row,
its message and both history rows survive any later failure (they are committed in the
same transaction as the state flip).

### 4.2 Authorization

`decide_request` authenticates first with `require_active_actor`. It then resolves the
request and applies this rule, server-side and inside the write transaction:

1. Admin (current role, case-insensitive) may decide any request in `PENDING` state —
   the FR-AUT-04 matrix row "Approve or deny a cross-vertical request: Admin Yes". Admin
   does not need a recipient row.
2. Otherwise the actor must have an `approval_recipient` row for **this** request, be
   currently active, hold current role `AH` or `Head`, and (for a Head) still belong to
   the responsible vertical. This applies FR-AUT-05 (current role) to the FR-XV-04
   "any one of those people" snapshot.
3. A Volunteer, an AH/Head who was not a recipient, a recipient whose current role or
   vertical changed, or an inactive person is rejected with
   `ApprovalAuthorizationError` and recorded through `record_denial`
   (`operation="approval.decide"`, `subject_type="approval_request"`,
   `subject_id=request_id`). The denial is written after the rejected transaction rolls
   back, as in Phase 6/7.

`list_pending_approvals` and `get_approval_request` apply the same rule in read form:
Admin sees all pending requests; everyone else sees only requests where they are a
recipient with current role `AH`/`Head` in the responsible vertical. A non-recipient gets
an empty list (or `ApprovalNotFoundError` for a direct id), not another team's queue — and
a direct id attempt is denied and recorded. This is FR-AUT-01/06 and NFR-SEC-03.

### 4.3 Decision transaction (FR-XV-04/05, AC-15, NFR-CON-02)

```python
class ApprovalError(Exception): ...
class ApprovalNotFoundError(ApprovalError): ...
class ApprovalAuthorizationError(ApprovalError): ...
class InvalidDecisionError(ApprovalError): ...

@dataclass(frozen=True)
class DecisionResult:
    request_id: int
    task_id: int
    decision: str
    message: str | None
    decided_by: int
    decided_at: str
    changed: bool          # False for a later attempt
    notified: int          # staged rows in this call

def decide_request(connection, request_id, *, actor_person_id,
                   decision: str, message: str | None = None) -> DecisionResult
```

Steps, one `BEGIN IMMEDIATE` transaction:

1. Load the request with its task (`task_id`, `requested_by`, `request_message`,
   `responsible_vertical_id`, `assignment_mode`, `people_needed`, `creator_id`,
   `approval_state`). Missing → `ApprovalNotFoundError`.
2. Authorize (§4.2). A task not in `PENDING` is not an error by itself: if a decision
   already exists, return it unchanged (step 4); if the state is `APPROVED`/`DENIED`
   without a decision row, stop with `ApprovalError("STATE_CONTRADICTION")` — never
   invent a decision.
3. Validate `decision in ("APPROVED", "DENIED")`; `message` is optional, trimmed and
   non-blank when supplied (0007 CHECK).
4. If `approval_decision` already exists for the request, return it with
   `changed=False` and stage nothing. This is the FR-XV-04 "later attempts see the
   decision and change nothing" path; it is not an authorization failure, and it writes
   no history row and no notification.
5. Insert the decision (`decided_by`, `decision`, optional message, `decided_at`).
   `UNIQUE(request_id)` plus `BEGIN IMMEDIATE` makes a concurrent second decision see
   the first row and return it (AC-15, NFR-CON-02).
6. Update `task.approval_state` to the decision. Do **not** bump `task.version`: approval
   is a separate dimension (FR-TSK-02), and bumping it would invalidate a concurrent
   Phase 7 edit that is not in conflict.
7. Write `task_history`: `field_name='approval_state'`, old `PENDING`, new decision,
   `reason` = the decision message or `CROSS_VERTICAL_DECISION`, `actor_person_id`,
   `actor_role` = current role, `actor_vertical_id` = actor's membership in the
   responsible vertical (NULL for Admin outside it), `changed_at` = now (FR-XV-06).
8. Stage notifications (§4.4).
9. Return `DecisionResult(changed=True, ...)`.

Any exception rolls back decision, state, history and notifications together: a
notified approval can never exist without its decision row. Delivery is Phase 11's job;
staging is idempotent.

### 4.4 Notifications (FR-XV-05, FR-NOT-01/05)

All through `stage_notification`, dedupe keys include the request/task and person:

| When | Event label | Recipient | Points at |
|---|---|---|---|
| Decision (approve or deny) | `CROSS_VERTICAL_DECISION` | the requester (`requested_by`) | `approval_request_id` |
| Approve, `EXPLICIT` | `TASK_ASSIGNED` | every active, non-cancelled `AWAITING` assignee | `task_id` |
| Approve, `OPEN` | `OPEN_TASK_AVAILABLE` | every active volunteer of the responsible vertical | `task_id` |

Dedupe keys are the Phase 6/7 formats (`CROSS_VERTICAL_DECISION:<request>:<person>`,
`TASK_ASSIGNED:<task>:<person>`, `OPEN_TASK_AVAILABLE:<task>:<person>`), so a retry or a
repeated approve stages nothing twice. On denial only the requester row is staged; no
assignee or volunteer is told, and the task stays hidden. The requester notification body
must carry the decision message when present (wording is Phase 11's concern; the row
stores the message in the decision record, and the delivery layer reads it from there).

### 4.5 Read model

`ApprovalView` fields: request id, task id, requester name/id, request message,
responsible vertical name/id, source vertical name, task description, task type,
assignment mode, people needed, created_at, current decision (if any) and its message.
It deliberately contains **no source message content and no bucket data** — NFR-SEC-03(c)
allows the approver the description and the requester's note only.

`list_pending_approvals(connection, *, actor_person_id) -> tuple[ApprovalView, ...]`
returns requests with no `approval_decision` and `task.approval_state='PENDING'`, ordered
by `created_at, id`. `get_approval_request(connection, request_id, *, actor_person_id)`
returns one or raises. Both authorize with the read form of §4.2; direct access by a
non-recipient is denied and recorded.

### 4.6 Phase 6 follow-through (FR-XV-06)

At accept, `backend/review.py` writes the request row but no history row. Phase 8 adds,
inside the same accept transaction, after the `approval_request` insert:

```
task_history(task_id, 'APPROVAL_REQUEST', NULL, 'PENDING', now,
             reviewer_person_id, actor_role, actor_vertical_id,
             request_message or 'CROSS_VERTICAL_REQUEST')
```

This is the only change to Phase 6 code. It does not change any state, gate or existing
row; the Phase 6 suite plus one new assertion is the proof. A request without a history
row is an FR-XV-06 failure even though the decision is correct.

### 4.7 Errors and security

- Unknown request → `ApprovalNotFoundError`; invalid decision token or blank message →
  `InvalidDecisionError`; unauthorized → denial + `ApprovalAuthorizationError`.
- No message content is read to decide authority (FR-AUT-08); the requester is identified
  from `approval_request.requested_by`, never from text.
- `approval_request.request_message` and `approval_decision.decision_message` are stored
  verbatim (0007) and may contain personal data: they are returned only to recipients,
  the requester and Admin, and are masked before any future AI use (not used by AI here).
- History and decision rows have no update/delete path (NFR-AUD-03).
- A `DENIED` task remains in storage with its assignee rows and decision (FR-XV-05
  "the task and decision are kept"); deactivation never deletes it (FR-AUT-07).

---

## 5. Migrations

**None.** `0007` already provides exactly the storage this phase needs:

- one request per task (`UNIQUE(task_id)`),
- the recipient snapshot (`approval_recipient`),
- one decision per request (`UNIQUE(request_id)`) with the `APPROVED|DENIED` CHECK,
- optional request and decision messages, non-blank when present,
- RESTRICT foreign keys on every reference.

Phase 8 changes no column, index or constraint. If implementation discovers a real gap
(for example a missing decision timestamp index), stop and report; do not add migration
`0019` (reserved for PB-2 in Phase 9) and do not renumber. The roadmap's number sequence
must stay stable.

---

## 6. Traceability matrix

| Requirement(s) | Where implemented | Test |
|---|---|---|
| FR-XV-04, AC-15 | `decide_request`: recipient/Admin authority, optional message, first wins | decision + first-wins tests |
| FR-XV-05, AC-15 | approve → live + requester notice; deny → hidden, kept, requester notice with message | approve/deny tests |
| FR-XV-06, FR-TSK-11 | request history (4.6) + decision history with role/vertical | history tests |
| FR-XV-02 (implemented in 6), AC-14 | Head cross-vertical task is live after accept with no request; Phase 8 asserts no decision path touches it | regression test |
| FR-AUT-01/05/06 | server-side recipient check, current role, denial recorded | authorization tests |
| NFR-SEC-03(c) | `ApprovalView` contains description + note only | content-leak test |
| NFR-CON-02 (accept in 6, claims in 7; decision here) | `BEGIN IMMEDIATE` + `UNIQUE(request_id)` | two-thread decision test |
| BR-08 | decision resolves the request, evidence and both sides kept | kept-row test |
| FR-NOT-01/05 | decision/assigned/open rows staged with dedupe | notification tests |
| FR-UI-03 (data half) | `list_pending_approvals` | read-model test |

Already complete elsewhere: FR-XV-01/02/03 (6), task lifecycle (7), delivery (11),
HTTP/UI (12 / out of repo).

---

## 7. Tests-first and mutations

New `tests/test_approval_service.py` (write before implementation):

- authorization: Admin not a recipient; recipient AH; recipient Head; non-recipient AH;
  Volunteer; inactive recipient; recipient demoted to Volunteer; Head moved to another
  vertical; each rejection recorded in `authorization_denial`.
- first decision wins: repeat call returns `changed=False` with the original message and
  writes no second history/notification; two connections/threads with a barrier produce
  exactly one `changed=True`; `UNIQUE(request_id)` is never violated.
- approve: `approval_state='APPROVED'`; `visible_tasks` now includes the task for the
  responsible volunteer; `TASK_ASSIGNED` staged once per AWAITING assignee (EXPLICIT) or
  `OPEN_TASK_AVAILABLE` once per active volunteer (OPEN); requester row staged.
- deny: `approval_state='DENIED'`; `visible_tasks` still excludes it; no assignee/volunteer
  rows; requester row exists; task, decision and message still present.
- history: request row from accept, decision row with actor role and vertical, decision
  message in `reason`.
- read model: pending list scope per role; no message content, bucket or source fields.
- errors: unknown request, invalid token, blank message, `STATE_CONTRADICTION`.
- regression: Head cross-vertical task has no request and is live after accept with its
  Head notification.

Mutation list:

1. Drop the `approval_recipient` check → non-recipient AH test red.
2. Drop the current-role/vertical re-check → demoted/moved tests red.
3. Allow overwriting an existing decision → first-wins test red.
4. Flip `approval_state` to the opposite value → approve/deny tests red.
5. Skip `OPEN_TASK_AVAILABLE`/`TASK_ASSIGNED` staging on approval → notification test red.
6. Skip the request history insert in `review.py` → FR-XV-06 test red.
7. Add `source_message.content` to `ApprovalView` → leak test red.
8. Remove the decision message from the requester notification payload/decision reason →
   message test red.

---

## 8. Workstreams

| WS | Content | Stop |
|---|---|---|
| W1 | `approvals.py` errors, `ApprovalView`, `get_approval_request`, `list_pending_approvals` + authorization | C1 |
| W2 | `decide_request` transaction, first-wins, state flip, history | C2 |
| W3 | Notification staging on approve/deny + dedupe | C3 |
| W4 | Phase 6 request-history follow-through + Phase 6 regression suite | C3 |
| W5 | Mutation battery, audits, DoD battery, §15 record | final |

No schema write. W1 must not import `tasks.py` internals; it reads the request tables and
uses `authorization.py`.

---

## 9. OWNER decisions for this phase

| ID | Question | Options | Recommendation |
|---|---|---|---|
| O8-1 | Confirm Admin may decide any pending request without being a recipient (matrix row says Yes) | (a) Admin override; (b) recipients only | (a), per FR-AUT-04 baseline; confirm |
| O8-2 | Confirm that a recipient whose current role/vertical changed loses the right (FR-AUT-05 vs FR-XV-04 snapshot) | (a) current role/vertical wins; (b) snapshot only | (a); snapshot decides who was asked, current role decides authority |
| O8-3 | Confirm a late decision attempt is a silent no-op (no denial record, no history) | (a) no-op return; (b) record the attempt | (a), FR-XV-04 "change nothing" |

No decision re-opens `DECISIONS.md` §A/§B.

---

## 10. SRS ambiguities carried by this phase

1. **"Every AH"** in FR-XV-03 — Phase 6 reads it as every AH of the responsible
   vertical. Phase 8 keeps that snapshot; an all-AHs reading would need a new recipient
   rule and is not implemented.
2. **FR-XV-04 "any one of those people"** vs FR-AUT-05 current role — resolved as
   recipient snapshot + current role (O8-2).
3. **Head cross-vertical notification** — FR-XV-02 is already satisfied in Phase 6; no
   decision row exists for it. Phase 8 must not create one.
4. **Decision message retention** — stored verbatim in `approval_decision`; the requester
   is told "with the message" (FR-XV-05), but wording/format is Phase 11.
5. **Admin vertical for history** — an Admin outside the responsible vertical has no
   membership there; `actor_vertical_id` is NULL while `actor_role='Admin'`, which is the
   truthful record (FR-XV-06 asks for the actor's role and vertical at that time).

---

## 11. Definition-of-done battery

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m backend --version
```

Targeted:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_approval_service.py tests/test_review_service.py
.venv\Scripts\python.exe -m pytest tests/test_task_service.py tests/test_authorization.py tests/test_notification_events.py
```

Read-only audits:

- `Select-String -Path backend/approvals.py -Pattern 'content|bucket|raw_metadata'` → no
  source-content read.
- `Select-String -Path backend -Pattern 'UPDATE task SET approval_state'` → exactly one
  site, in `approvals.py`.
- `Select-String -Path backend/approvals.py -Pattern 'approval_recipient'` → authorization
  and listing only.
- `git status --short` → only intended files; nothing staged.

Pass: full suite green, ruff/mypy clean, all eight mutations observed red, audits clean,
no migration added.

---

## 12. Risks and checkpoints

| Risk | Mitigation |
|---|---|
| Second decision silently overwrites the first | `UNIQUE(request_id)` + in-transaction existing-check + two-thread test |
| Approval leaks a hidden task | no new visibility query; assert `visible_tasks` before/after |
| Requester not told the message | decision row and history reason carry it; payload test |
| Duplicate notifications on retry | Phase 7 dedupe keys; `changed=False` stages nothing |
| Approver sees source chat | `ApprovalView` field allow-list + leak test |
| Phase 6 regression from the history row | one added insert; full Phase 6 suite |

| Checkpoint | Green state |
|---|---|
| C1 | read model + authorization; suite green |
| C2 | decision transaction + first-wins + history; suite green |
| C3 | notifications + Phase 6 follow-through; suite green |
| Final | mutations, audits, DoD observed; §15 record written |

Revert the current workstream if a checkpoint cannot be green; never leave a decision row
without its state flip.

---

## 13. Appendix — verbatim requirements quoted

- **FR-XV-01** A task is CROSS_VERTICAL when its responsible vertical differs from its
  source vertical. The reviewer sets this during review (FR-REV-03).
- **FR-XV-02** A Head's cross-vertical task goes live as soon as it is accepted. The Head
  of the responsible vertical shall be notified.
- **FR-XV-03** An AH's cross-vertical task, when accepted, shall be created with
  Approval = PENDING and shall not be visible to volunteers. A request, with an optional
  message from the requester, shall be sent to every AH and the Head of the responsible
  vertical.
- **FR-XV-04** Any one of those people may approve or deny the request, with an optional
  message. The first decision wins. Later attempts see the decision and change nothing.
- **FR-XV-05** On approval the task goes live to the responsible vertical's volunteers
  according to its assignment mode, and the requester is notified. On denial the task
  never goes live, the requester is notified with the message, and the task and decision
  are kept.
- **FR-XV-06** Requests, decisions and messages shall be recorded in the task history
  with the actor's role and vertical.
- **AC-14** A Head raises a cross-vertical task → Source vertical unchanged; responsible
  vertical set; task goes live; the responsible Head is notified.
- **AC-15** An AH requests a cross-vertical task → Task PENDING and hidden; AHs and Head
  of the target vertical notified; first decision wins, with its message; approval makes
  it live, denial keeps it closed and tells the requester.
- **BR-08** Evidence that conflicts is preserved until an authorized decision resolves it.
- **NFR-CON-02** Simultaneous accepts, claims and assignments shall each have exactly one
  winner.
- **NFR-SEC-03 (excerpt)** An approval request (FR-XV-03) shows the approver the task
  description and the requester's note, not the source chat.
