# PHASE 7 EXECUTION PROMPT — Foundations and Task Lifecycle

Paste this whole file into a fresh session. It is self-contained: it does not assume you
remember the planning session. Do exactly this phase; do not start Phase 8.

---

## SESSION BUDGET PROTOCOL

- **One workstream per session.** Run exactly one workstream (§7) unless the owner says
  otherwise; finish it at its checkpoint, then stop.
- **Progress log.** After each checkpoint, append one line to `docs/PHASE7_PROGRESS.md`
  (create the file if missing) with the checkpoint, the real commands run and their actual
  outcomes. Never write a result that was not observed.
- **No automatic continuation.** Never start the next workstream without being told.
- **Green or stop.** If the tree is not green at a checkpoint (plain `pytest`, ruff, mypy,
  migrations apply and re-apply, no staged changes), stop and report the failing command
  and its output; do not paper over it and do not continue.
- **Owner commits.** The owner commits at each green checkpoint; the agent never stages,
  commits or pushes.

---

## 0. What you are doing

Execute **Phase 7** of ASEC as approved in `docs/PHASE7_PLAN.md`: the relevance-request
retrofit, the eligibility default change, the single schema window (0016–0018), the
shared authorization/notification extraction, and the task lifecycle FR-TSK-01..16 with
its single visibility filter. Tests first. Leave the repository green at every
checkpoint. **Do not commit.**

---

## 1. Context reads (in this order, before any edit)

1. `AGENTS.md` — workflow rules, architecture invariants, security rules.
2. `docs/SRS.md` — §3.6 Tasks, §3.7 review/approval, §3.1 ingestion, §4/EIR-03,
   §6 non-functionals, §8 acceptance criteria. Requirements are authoritative.
3. `docs/DECISIONS.md` — §A and §B are approved owner decisions; read them, do not
   re-open them.
4. `docs/ROADMAP.md` — phase order, shared contracts, migration numbers, coverage table.
5. `docs/PHASE7_PLAN.md` — **the approved plan you are implementing**. §4 is the design,
   §5 the exact SQL, §7 tests/mutations, §8 workstreams/checkpoints, §9 owner items,
   §11 the battery, §13 the verbatim requirements.
6. `docs/PHASE6_PLAN.md` §15 — the Phase 6 implementation record and the format you must
   reproduce for Phase 7.
7. Real code you will touch or reuse: `backend/db/connection.py`, `backend/config.py`,
   `backend/masking.py`, `backend/relevance.py`, `backend/proposals.py`,
   `backend/bucketing.py`, `backend/review.py`, `backend/db/migrations.py`, and the
   migrations `0001`–`0015`.
8. Test helpers you will have to update: `tests/test_ingestion_service.py`,
   `tests/test_proposal_service.py`, `tests/test_relevance_service.py`,
   `tests/test_review_service.py`, `tests/test_migrations_*.py`.

Never open, print, copy or reference `.env`, any secret file, or `session-*.md`.

---

## 2. Expected baseline (verify, do not assume)

```powershell
git log --oneline -n 3
git status --short
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m backend --version
Get-ChildItem backend/db/migrations -Name | Sort-Object
```

Expected at the time this prompt was written:

- `HEAD` = `fb5263d Phase 6 planning complete`, working tree clean.
- plain `pytest` → `1016 passed` (record the exact line you observe).
- `ruff` → `All checks passed!`; `mypy` → `Success: no issues found in 14 source files`.
- `python -m backend --version` → `0.0.1`.
- migrations `0001`–`0015` only.

If the baseline differs (dirty tree, failing tests, a different HEAD), **stop and report**
before changing anything. A phase may not start on a red tree.

---

## 3. Standard rules (non-negotiable)

- **No commits, no staging, no pushes.** Never `git add`, `git commit`, `git reset`,
  `git checkout -- .`, `git clean`, or any destructive git command.
- **No `.env`, no secrets** in code, tests, fixtures, logs, errors, or documentation. Test
  phones/roll numbers are obviously synthetic.
- **Do not edit `docs/SRS.md`.** Do not edit `docs/DECISIONS.md`. Do not re-open §A/§B.
- **No new dependency, framework, queue, vector store or agent.** `dependencies = []`
  stays empty in this phase.
- **Tests first.** For each workstream: write the failing test(s), run them, capture the
  real RED output, then implement, then capture the GREEN output. Never report a result
  you did not observe.
- **Plain `pytest`** for the summary line (`addopts` already sets `-q`; adding `-q` again
  hides the count).
- **Windows/PowerShell** commands as written. Use `.venv\Scripts\python.exe -m ...`.
- **Small slices.** Workstream by workstream, in order. Do not carry a half-applied
  migration window, and do not proceed past a red checkpoint.
- **Do not weaken an existing test to make it pass.** If a Phase 6 expectation must change
  because of a PB decision, update it mechanically, state the exact old/new assertion in
  the report, and keep the behaviour it was protecting covered by a new test.
- **Documentation edits are limited to** `docs/PHASE7_PLAN.md` §15 (the implementation
  record you append at the end) and `docs/PLANNING_PROGRESS.md` (status lines). Nothing
  else under `docs/` may change.

---

## 4. Authorization statement

This session is authorized to:

- create `backend/ai_context.py`, `backend/authorization.py`,
  `backend/notification_events.py`, `backend/tasks.py`;
- add migrations `backend/db/migrations/0016_roster_integrity.sql`,
  `0017_task_schema.sql`, `0018_announcement_audience.sql`;
- modify `backend/relevance.py`, `backend/proposals.py`, `backend/review.py`,
  `backend/bucketing.py` only as the plan requires (S1, S2, S3 follow-through);
- add and update tests under `tests/`;
- append `docs/PHASE7_PLAN.md` §15 and update `docs/PLANNING_PROGRESS.md`.

This session is **not** authorized to: touch Phase 8+ features (approval decisions,
scheduler/worker, replay, Discord adapter, AI HTTP adapter, delivery, HTTP API), add
dependencies, edit the SRS or DECISIONS, or change any requirement.

---

## 5. Approved decisions this phase relies on

| ID | What it fixes for you |
|---|---|
| **PB-3** | Task table rebuild: `EXPLICIT` stores `people_needed` NULL; `task_assignee` gets a surrogate id and a partial unique index on active rows (not denied, not cancelled); keep the separate `cancelled` flag; `INTERNAL` stores `responsible_vertical_id = source_vertical_id` NOT NULL. |
| **PB-5** | Role vocabulary `Admin/Head/AH/Volunteer` with a CHECK; `reports_to` becomes a FK; `phone` and `roll_number` NOT NULL; one role per person. Same rebuild window as PB-3, before any roster load. |
| **PB-6** | Structured announcement audience: `audience_type IN ('VERTICAL','AH_VOLUNTEERS')` + `audience_ah_person_id`. Phase 6 tokens map one-to-one. |
| **Relevance retrofit** | Relevance requests get opaque handles and Phase 5's markup rewriting; no real Discord ID leaves the backend. |
| **Eligibility across verticals** | Default **ON**: an AH/Head message produces a proposal only if the sender belongs to the group's vertical; heads-and-AHs-only groups stay unmapped. |
| **OD-REV-1** | Accept materializes the entity in the accept transaction; Phase 7 adds only the volunteer lifecycle. |
| **OD-REV-5 / PB-6** | Audience token mapping (sender role, not accepting Admin). |
| **OD-REV-6** | `task.creator_id` = primary message sender; the accepting Admin is the history actor. |
| **Retention and training** | Archive only; no training corpus. |

Nothing here may be reinterpreted. If the code and a decision disagree, the decision wins
and the discrepancy is reported.

---

## 6. OWNER DECISIONS FOR THIS PHASE

Fill each answer before the workstream that needs it. An unanswered item that a workstream
needs is a **stop**, not a guess. If the owner replies "use the recommendations", record
that sentence verbatim in the report and take the plan's recommendation.

```
O7-1  Run 0016-0018 as a pre-roster window (no real roster rows must survive)?
      Options: (a) confirm pre-roster window; (b) allow a data-mapping script.
      Plan recommendation: (a); if real rows exist, stop and map deliberately.
      OWNER ANSWER: (a) Confirm: run 0016–0018 as a pre-roster window. If real roster rows
      exist, stop.

O7-2  Authorize the mechanical updates of pinned Phase 6 tests forced by PB-3/PB-6?
      Options: (a) authorize; (b) keep old expectations and delay PB-3/PB-6.
      Plan recommendation: (a). Needed before W3.
      OWNER ANSWER: (a) Authorized: mechanical updates of Phase 6 pinned tests forced by
      PB-3 and PB-6, each with a one-line reason citing the requirement.

O7-3  May an authorized user accept/deny on an assignee's behalf (FR-TSK-05)?
      Options: (a) Admin only, recorded in history; (b) creator; (c) self-action only.
      Plan recommendation: (c) for now, recorded as a gap; (a) if the owner wants the
      FR-TSK-05 sentence fully exercised.
      OWNER ANSWER: (a) Admin only may accept or deny on an assignee's behalf, recorded in
      history with an on-behalf flag and the Admin as actor. Nobody else.

O7-4  Cancel authority: FR-TSK-08 says creator, FR-TSK-16/matrix add Head/Admin.
      Options: (a) creator + Head of responsible vertical + Admin; (b) creator only.
      Plan recommendation: (a), consistent with the FR-AUT-04 baseline matrix.
      OWNER ANSWER: (a) Cancel authority: creator, Head of the responsible vertical, and
      Admin.

O7-5  Admin work-status override (FR-TSK-06 says Admin; matrix cell is "-").
      Options: (a) Admin override; (b) assignee only.
      Plan recommendation: (a), per FR-TSK-06.
      OWNER ANSWER: (a) Admin may override work status, recorded in history.

O7-6  Role CHECK vs the Phase 6 case-insensitive Admin test (`role='aDmIn'`).
      Options: (a) strict CHECK + rewrite the test to prove policy matching; (b) a
      case-insensitive CHECK.
      Plan recommendation: (a).
      OWNER ANSWER: (a) Strict role CHECK (`Admin`, `Head`, `AH`, `Volunteer`); the roster
      loader normalizes case; rewrite the Phase 6 case-insensitive Admin test to prove
      policy matching.

O7-7  Optional test-typing cleanup (89 pre-existing mypy errors in tests).
      Options: (a) defer; (b) separate slice now.
      Plan recommendation: (a) defer; the mypy gate stays backend-only.
      OWNER ANSWER: (a) Defer the test-typing cleanup.
```

Gates: **W3 needs O7-1, O7-2, O7-6.** **W6 needs O7-3, O7-4, O7-5.** W1 and W2 need
nothing except §2 being green.

---

## 7. Workstreams (plan §8) and checkpoints

Do them in this order. Each ends at a checkpoint; run the full battery at each one.

| WS | Work | Ends at |
|---|---|---|
| W1 | `backend/ai_context.py` (`MentionHandles`, `rewrite_markup`, `reply_to_message_id`, `masked_request_content`); retrofit `RelevanceRequest`/`ContextMessage`; switch `proposals.py` to the shared helper. Phase 5 proposal suite must stay byte-identical. | C1 |
| W2 | Eligibility default ON in `ProposalPolicy`; update the pinned default test; keep the `"0"` escape hatch. | C2 |
| W3 | 0016, 0017, 0018 exactly as plan §5, with the guard `SELECT ... RAISE(ABORT, ...)` clauses; `review.py` follow-through (`_people_needed_for_task` → `(None, 0)` for EXPLICIT, INTERNAL responsible = source, PB-6 announcement columns); update person/task/announcement fixtures. | C3 |
| W4 | Extract `backend/authorization.py` and `backend/notification_events.py`; keep `review.py` private aliases; behaviour-neutral (same test count before/after). | C4 |
| W5 | `backend/tasks.py` read model: `visible_tasks` (authority derived inside), `_live_task_row`, `TaskSummary`, `task_progress`. | C5 |
| W6 | `backend/tasks.py` writes: `claim_open_task`, `set_agreement`, `update_work_status`, `edit_task`, `reassign_task`, `cancel_task`, history, notifications, concurrency. | C5 |
| W7 | Mutation battery (all ten, plan §7), read-only audits, full DoD battery, append §15. | final |

Checkpoint green means: full `pytest` green, `ruff` and `mypy` clean, the new/changed
tests present, and the tree runnable from that point (nothing half-migrated, no dead
imports, no skipped tests). If a checkpoint cannot be made green, revert that workstream's
changes to the previous green state and report — do not leave it half-done.

Cut-off safety: after C1, C2, C3, C4, C5 and final, the repository must be runnable and
the full suite green, because the session may end there. Do not begin W3 until W2's C2 is
green; migrations 0016–0018 must land together in one window.

---

## 8. Test and mutation requirements (plan §7)

New test files: `tests/test_migrations_roster_integrity.py`,
`tests/test_migrations_task_schema.py`, `tests/test_migrations_announcement_audience.py`,
`tests/test_authorization.py`, `tests/test_notification_events.py`,
`tests/test_task_service.py`, `tests/test_ai_context.py`.

Extended: `tests/test_relevance_service.py`, `tests/test_proposal_service.py`,
`tests/test_review_service.py`.

Cover at minimum:

- hidden `PENDING`/`DENIED` tasks and cancelled tasks never appear through
  `visible_tasks`, and a hidden id passed directly to any mutator behaves as not found;
- claim capacity, sixth claim rejected, two-thread last-place race = exactly one winner;
- agreement only from an explicit action; denial frees the place and allows re-add;
- work-status transitions incl. `COMPLETED` final and same-value rejection;
- edit `expected_version` staleness → current values returned + history row for both
  attempts; reassign/cancel notifications to affected assignees;
- one `task_history` row per changed field with actor role and vertical at the time;
- `EXPLICIT` `people_needed IS NULL`; `INTERNAL` responsible = source; partial unique
  index permits re-add after denial and blocks a duplicate active row;
- 0018 audience token mapping and AH snapshot; guard aborts on unmappable rows;
- no raw Discord id, phone or email in a relevance request; stored content unchanged;
- migration re-apply returns `[]` and `PRAGMA foreign_key_check` is empty after a
  populated rebuild.

Then run the ten mutations in plan §7 one at a time and record the exact failing test and
the observed assertion for each. Restore the file after each mutation and verify the
restore (hash or `git diff` empty). A mutation that does not turn a test red is a missing
test: add it.

---

## 9. Final battery (run in this order, capture real output)

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m backend --version
git diff --check
git status --short
```

Also run the targeted groups in plan §11 (migrations, extraction, task service,
relevance/proposal/review). Fresh-database migration check: a test (not a manual claim)
applies `apply_migrations` twice on a temp file DB, asserts `[]` on the second call and
`PRAGMA foreign_key_check` empty.

Pass means: plain `pytest` green with the new higher count, `ruff` clean, `mypy` clean,
version unchanged (`0.0.1`), `git diff --check` silent, `git status --short` showing only
intended files and **nothing staged**.

---

## 10. Read-only audit (no edits)

```powershell
Select-String -Path backend -Pattern 'sender_discord_user_id|discord_channel_id'
Select-String -Path backend -Pattern 'FROM task'
Select-String -Path backend/tasks.py -Pattern 'mask_pii|content'
Select-String -Path backend -Pattern 'INSERT INTO task_history|UPDATE task_history|DELETE FROM task_history'
git status --short
```

Expected: Discord ids only in ingestion/proposal/relevance DB reads, never in a request
dataclass; `FROM task` only in `tasks.py`, `review.py` and the Phase 8 place; `tasks.py`
never reads message content; history has no update/delete path; nothing staged.

---

## 11. Plan update (one edit, at the very end)

Append a `## 15. Implementation record` section to `docs/PHASE7_PLAN.md`, matching
`docs/PHASE6_PLAN.md` §15 in shape:

- 15.1 files actually changed, with status and one-line content;
- 15.2 real test totals (plain `pytest` line, targeted runs) and the RED evidence you
  actually captured;
- 15.3 mutation battery table: mutation, target test, observed RED, restore check;
- 15.4 final battery: exact commands and real output;
- 15.5 audits: commands and real output;
- 15.6 deviations from the plan and why (with requirement IDs);
- 15.7 known gaps/risks and the next step (Phase 8).

Do not rewrite earlier sections of the plan. Update `docs/PLANNING_PROGRESS.md` with one
status line (Phase 7 executed, count, date, next = Phase 8).

---

## 12. Report and stop

Use the `AGENTS.md` completion-report format:

- **Summary**
- **SRS requirement IDs addressed** (the plan §6 list; state any not met)
- **Files changed**
- **Tests/checks run** — exact commands and the actual observed outcomes (plain `pytest`
  line, ruff, mypy, version, mutation results, audits)
- **Known gaps or risks** (including every OWNER answer used and every unblocked item)
- **Suggested next step** (Phase 8, only after a green full suite)

Then **stop and report. Do not commit, do not stage, do not start Phase 8.**
