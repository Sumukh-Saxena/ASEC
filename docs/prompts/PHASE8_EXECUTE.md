# PHASE 8 EXECUTION PROMPT — Cross-Vertical Approval Decisions

Paste this whole file into a fresh session. It is self-contained. Do exactly this phase;
do not start Phase 9.

---

## 0. What you are doing

Execute **Phase 8** of ASEC as approved in `docs/PHASE8_PLAN.md`: the approval read model,
recipient-scoped authorization, the first-decision-wins decision transaction, requester and
live-task notification staging, decision/request history, and the Phase 6 request-history
follow-through. **No migration** — `0007` already has the storage. Tests first. Leave the
repository green at every checkpoint. **Do not commit.**

---

## 1. Context reads (in this order, before any edit)

1. `AGENTS.md`.
2. `docs/SRS.md` §3.7 (FR-XV-01..06), §3.9 FR-AUT matrix, §6, §8 AC-14/AC-15.
3. `docs/DECISIONS.md` §A (OD-REV-1/5/6/7) and §B — approved; do not re-open.
4. `docs/ROADMAP.md` §2 (shared contracts) and §6 (coverage).
5. `docs/PHASE8_PLAN.md` — the approved plan (§4 design, §6 traceability, §7 tests,
   §8 workstreams, §9 owner items, §11 battery, §13 verbatim requirements).
6. `docs/PHASE7_PLAN.md` §15 — what Phase 7 actually built (real names and counts).
7. Real code: `backend/db/migrations/0007_cross_vertical_approvals.sql`,
   `backend/review.py` (`_approval_recipient_ids`, `_materialize_task`,
   `_insert_notification`, `accept_proposal`), `backend/tasks.py` (`visible_tasks`,
   `_live_task_row`), `backend/authorization.py`, `backend/notification_events.py`.

Never open, print or reference `.env`, secrets or `session-*.md`.

---

## 2. Expected baseline (verify, do not assume)

```powershell
git log --oneline -n 3
git status --short
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
Get-ChildItem backend/db/migrations -Name | Sort-Object
```

Expected: Phase 7 committed on top of `fb5263d`; clean tree; plain `pytest` green with the
count recorded in `docs/PHASE7_PLAN.md` §15; ruff/mypy clean; migrations `0001`–`0018`
with `0018` last; `backend/tasks.py`, `backend/authorization.py`,
`backend/notification_events.py` and `backend/ai_context.py` present.

If Phase 7 is not committed or the tree is red, **stop and report**.

---

## 3. Standard rules (non-negotiable)

- No commits, no staging, no pushes, no destructive git.
- No `.env` or secrets anywhere; synthetic test data only.
- Do not edit `docs/SRS.md` or `docs/DECISIONS.md`.
- **No migration in this phase.** If you believe one is needed, stop: `0007` is the
  approved storage, and `0019` is reserved for Phase 9.
- No new dependency. `dependencies = []` stays empty.
- Tests first, with real RED output captured before implementation. Plain `pytest`.
- Do not weaken Phase 6/7 tests. If one must change, explain exactly why in the report.
- Documentation edits limited to `docs/PHASE8_PLAN.md` §15 and
  `docs/PLANNING_PROGRESS.md`.

---

## 4. Authorization statement

Authorized to:

- create `backend/approvals.py` (`ApprovalView`, `get_approval_request`,
  `list_pending_approvals`, `decide_request`, error types, `DecisionResult`);
- add `CROSS_VERTICAL_DECISION` and any needed key builder in
  `backend/notification_events.py` (additive only);
- make the **one** Phase 6 follow-through edit in `backend/review.py`: write the
  `APPROVAL_REQUEST` history row inside the existing accept transaction;
- add/update tests under `tests/`;
- append `docs/PHASE8_PLAN.md` §15 and update `docs/PLANNING_PROGRESS.md`.

Not authorized to: change task lifecycle behaviour, add a schema, add an approval round /
escalation / delegation / "request more information", touch delivery, HTTP, AI, or the
pipeline (Phases 9–12), or edit requirements.

---

## 5. Approved decisions this phase relies on

| ID | What it fixes for you |
|---|---|
| **OD-REV-7** | Accept creates `approval_request` + recipient snapshot for an AH cross-vertical task; an empty recipient set blocks the accept; the decision logic is Phase 8. |
| **OD-REV-5 / PB-6** | Audience/recipient semantics follow the sender's role, not the accepting Admin's. |
| **OD-REV-6** | `task.creator_id` = primary message sender; the Admin actor is recorded in history. |
| **PB-3** | `approval_state` and `version` are independent dimensions — deciding must not bump `version`. |
| **Phase 7 contract** | `visible_tasks` / `_live_task_row` is the only task visibility filter; Phase 8 must not duplicate it. |
| **Phase 7 contract** | `record_denial` / `require_active_actor` and `stage_notification` are the shared helpers; no new recorder or notification insert. |

---

## 6. OWNER DECISIONS FOR THIS PHASE

```
O8-1  May Admin decide any pending request without being a recipient? (Matrix says Yes.)
      Options: (a) Admin override; (b) recipients only.
      Plan recommendation: (a), per the FR-AUT-04 baseline. Needed before W1.
      OWNER ANSWER:

O8-2  A recipient whose current role/vertical changed: does authority follow the
      snapshot or the current role (FR-AUT-05 vs FR-XV-04)?
      Options: (a) current role/vertical wins; (b) snapshot only.
      Plan recommendation: (a) — the snapshot decides who was asked, the current role
      decides authority. Needed before W1.
      OWNER ANSWER:

O8-3  A later decision attempt: silent no-op or recorded attempt?
      Options: (a) no-op return, no denial row, no history; (b) record the attempt.
      Plan recommendation: (a), FR-XV-04 "change nothing". Needed before W2.
      OWNER ANSWER:
```

All three gate W1/W2. Unanswered = stop, do not guess.

---

## 7. Workstreams (plan §8) and checkpoints

| WS | Work | Ends at |
|---|---|---|
| W1 | `backend/approvals.py`: errors, `ApprovalView`, `get_approval_request`, `list_pending_approvals`, read-form authorization. | C1 |
| W2 | `decide_request`: `BEGIN IMMEDIATE`, authorization, first-decision-wins, state flip, no version bump, history row. | C2 |
| W3 | Notification staging: decision → requester (with message); approve → `TASK_ASSIGNED` (EXPLICIT) or `OPEN_TASK_AVAILABLE` (OPEN); deny → requester only. Dedupe keys from Phase 7. | C3 |
| W4 | Phase 6 follow-through: `APPROVAL_REQUEST` history row inside accept; Phase 6 suite green. | C3 |
| W5 | Mutation battery (eight, plan §7), audits, DoD battery, §15. | final |

Checkpoint green = full `pytest` green + ruff/mypy clean + tree runnable. Revert a
workstream that cannot be green; never leave a decision row without its state flip.

Cut-off safety: C1, C2, C3 and final must each leave the repo with a green full suite and
no half-written module.

---

## 8. Tests and mutations (plan §7)

New `tests/test_approval_service.py`, written before `approvals.py`:

- Admin non-recipient decides; recipient AH decides; recipient Head decides; non-recipient
  AH, Volunteer, inactive recipient, demoted recipient, relocated Head → rejected and each
  recorded in `authorization_denial`;
- first decision wins: repeat call returns `changed=False`, original message, no second
  history/notification row; two threads with a barrier → exactly one `changed=True`;
- approve: `approval_state='APPROVED'`, task now in `visible_tasks` for the responsible
  volunteer, assignee/volunteer rows staged once, requester row staged;
- deny: `DENIED`, still hidden, task + decision + message kept, requester row only;
- history: request row from accept, decision row with actor role/vertical, message in the
  reason;
- read model: scope per role, no source content/bucket fields;
- errors: unknown request, invalid token, blank message, `STATE_CONTRADICTION`;
- regression: Head cross-vertical task has no request and is live after accept.

Then the eight mutations in plan §7, one at a time, recording the exact red test and the
observed assertion; restore and verify each restore.

---

## 9. Final battery (in this order, real output)

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m backend --version
.venv\Scripts\python.exe -m pytest tests/test_approval_service.py tests/test_review_service.py
.venv\Scripts\python.exe -m pytest tests/test_task_service.py tests/test_authorization.py tests/test_notification_events.py
git diff --check
git status --short
```

Pass: full suite green, ruff/mypy clean, **no new migration file**, eight mutations red,
audits clean, nothing staged.

---

## 10. Read-only audit (no edits)

```powershell
Select-String -Path backend/approvals.py -Pattern 'content|bucket|raw_metadata'
Select-String -Path backend -Pattern 'UPDATE task SET approval_state'
Select-String -Path backend/approvals.py -Pattern 'approval_recipient'
Select-String -Path backend -Pattern 'INSERT INTO approval_decision'
git status --short
```

Expected: no source-content read in `approvals.py`; exactly one `UPDATE task SET
approval_state` site; `approval_recipient` used only for authorization/listing; the
decision insert only in `approvals.py`; nothing staged.

---

## 11. Plan update (one edit, at the very end)

Append `## 15. Implementation record` to `docs/PHASE8_PLAN.md` in the shape of
`docs/PHASE6_PLAN.md` §15: files changed, real test totals, RED evidence, mutation table,
battery output, audits, deviations (with requirement IDs), gaps/next step.

Update `docs/PLANNING_PROGRESS.md` with one status line (Phase 8 executed, count, next =
Phase 9 or 10).

---

## 12. Report and stop

Use the `AGENTS.md` completion-report format: summary; SRS IDs addressed (FR-XV-04/05/06,
FR-AUT-01/05/06, AC-14/15, NFR-CON-02, BR-08, FR-NOT-01/05); files changed; tests/checks
run with actual outcomes; known gaps/risks (including every OWNER answer used); suggested
next step.

Then **stop and report. Do not commit, do not stage, do not start Phase 9.**
