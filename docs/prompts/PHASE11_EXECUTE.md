# PHASE 11 EXECUTION PROMPT — Notification Delivery and Announcement Fan-out

Paste this whole file into a fresh session. It is self-contained. Do exactly this phase;
do not start Phase 12.

---

## SESSION BUDGET PROTOCOL

- **One workstream per session.** Run exactly one workstream (§7) unless the owner says
  otherwise; finish it at its checkpoint, then stop.
- **Progress log.** After each checkpoint, append one line to `docs/PHASE11_PROGRESS.md`
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

Execute **Phase 11** of ASEC as approved in `docs/PHASE11_PLAN.md`: state-accurate
rendering and the in-app list, the delivery worker with retries and attempt records, the
`PushSender`/FCM sender behind an interface, the `OtpSender` interface, and announcement
fan-out from the structured audience. Migration `0023` (`push_endpoint` +
`notification_read`). Tests first. Leave the repository green at every checkpoint.
**Do not commit.**

---

## 1. Context reads (in this order, before any edit)

1. `AGENTS.md`.
2. `docs/SRS.md` §3.8 FR-NOT-01..07, §3.6 in-app visibility, §3.9 FR-AUT matrix,
   §4/EIR-04, §6.5 security, AC-17, AC-22.
3. `docs/DECISIONS.md` §A (OD-REV-5) and §B (PB-5, PB-6, retention).
4. `docs/ROADMAP.md` §2 (contracts), §3 (migration numbers).
5. `docs/PHASE11_PLAN.md` — the approved plan (§4 design, §5 exact SQL, §6 traceability,
   §7 tests/mutations, §8 workstreams, §9 owner items, §11 battery, §13 verbatim
   requirements).
6. `docs/PHASE7_PLAN.md` §15, `docs/PHASE8_PLAN.md` §15, `docs/PHASE9_PLAN.md` §15,
   `docs/PHASE10_PLAN.md` §15 — real names and counts.
7. Real code: `backend/db/migrations/0008_notifications.sql`, `0018_announcement_audience`
   (in `0018`), `backend/notification_events.py`, `backend/review.py`,
   `backend/tasks.py`, `backend/approvals.py`, `backend/authorization.py`,
   `backend/config.py`.

Never open, print or reference `.env`, FCM keys, OTP codes, secrets or `session-*.md`.

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

Expected: Phases 7–9 committed, Phase 10 committed or in review; clean tree; plain
`pytest` green at the latest recorded count; ruff/mypy clean; migrations `0001`–`0022` if
Phase 10 landed (`0021` otherwise); `backend/notification_events.py` and
`backend/authorization.py` present.

If the tree is red, **stop and report**.

---

## 3. Standard rules (non-negotiable)

- No commits, no staging, no pushes, no destructive git.
- No credential, token, phone number or OTP code in the repo, tests, fixtures, logs,
  attempt errors, notification bodies or reports. Fake values in tests only.
- Do not edit `docs/SRS.md` or `docs/DECISIONS.md`.
- **Dependencies:** O11-2 approved FCM HTTP v1 only as an optional `push` extra behind
  `PushSender` (exact pin, one module, core tests green without it). No other dependency.
- Delivery is **always after** the domain transaction committed. A delivery failure never
  rolls back or blocks the change that caused it (FR-NOT-04).
- **No second notification row on retry** (FR-NOT-05). No batching (FR-NOT-02).
- Do not invent a provider, audience type or notification channel. If the SRS is silent
  (OTP channel, deep-link scheme), it stays an OWNER item.
- Tests first with real RED evidence; plain `pytest`; the suite makes no network call.
- Documentation edits limited to `docs/PHASE11_PLAN.md` §15 and
  `docs/PLANNING_PROGRESS.md`.

---

## 4. Authorization statement

Authorized to:

- create `backend/notification_delivery.py` (rendering, list, read, delivery, fan-out,
  sweep, `PushSender`, `FcmPushSender`, `OtpSender`, `RecordingOtpSender`);
- add `backend/db/migrations/0023_push_endpoints.sql` exactly as plan §5 (`push_endpoint`
  + `notification_read`, same version number, no renumbering);
- add endpoint helpers (`register_endpoint`, `deactivate_endpoint`);
- add/update tests under `tests/`;
- append `docs/PHASE11_PLAN.md` §15 and update `docs/PLANNING_PROGRESS.md`.

Not authorized to: change when notifications are staged, add batching/digests/quiet
hours, build the HTTP API or sign-in flow (12), add a provider, or edit requirements.

---

## 5. Approved decisions this phase relies on

| ID | What it fixes for you |
|---|---|
| **PB-6** | `announcement.audience_type` (`VERTICAL`/`AH_VOLUNTEERS`), `audience_vertical_id`, `audience_ah_person_id` — the fan-out source of truth. The old `audience_scope` is gone; do not read it. |
| **PB-5** | `person.phone` is NOT NULL, `role` is `Admin/Head/AH/Volunteer`, `reports_to` is a person FK. |
| **OD-REV-5** | The audience follows the sender's role, already stored in 0018. |
| **Phase 7 contract** | `stage_notification` + dedupe keys are the only insert path; `visible_tasks` is the only task filter. |
| **Retention** | Archive only; attempts and read markers are immutable evidence (NFR-AUD-03). |

---

## 6. OWNER DECISIONS FOR THIS PHASE

```
O11-1 OTP/SMS provider and credentials; resolve FR-ROS-03 (SMS to roster phone) vs
      EIR-04 (FCM).
      Options: (a) SMS vendor for OTP + FCM for push; (b) FCM-only; (c) defer, in-app
      code for testing only.
      Plan recommendation: (a), matching the FR-ROS-03 wording; owner supplies
      credentials. Needed before W5.
      OWNER ANSWER: (c) Build the OTP sender interface with a development sender now.
      **OWNER SUPPLIES** the SMS provider before go-live (in India, bulk SMS usually needs
      sender and template registration, so start early). FR-ROS-03 needs SMS to the roster
      phone because the app has no device token before sign-in. STOP GATE for live OTP;
      W5's interface and development sender proceed.

O11-2 FCM mode.
      Options: (a) legacy HTTP server key (no dependency, deprecated); (b) HTTP v1 with
      an approved google-auth/cryptography dependency; (c) owner-run push gateway.
      Plan recommendation: (a) only if the deprecation risk is accepted; otherwise (b).
      Needed before W4.
      OWNER ANSWER: (b) FCM HTTP v1 through an optional `push` extra (the signing needs a
      package the standard library lacks), isolated behind `PushSender`. I believe the
      legacy FCM HTTP API was shut down in 2024; confirm in the Firebase docs before
      building.

O11-3 Retry policy and batch limit.
      Options: (a) 5 attempts, 5 s base backoff, 5 min cap, 50 per batch; (b) owner
      values.
      Plan recommendation: (a), configuration-only. Needed before W2.
      OWNER ANSWER: (a) 5 attempts, 5 s base, 5 min cap, 50 per batch.

O11-4 Confirm a VERTICAL announcement goes to all active members of the vertical.
      Options: (a) all active members; (b) volunteers only.
      Plan recommendation: (a), the literal FR-REV-10 reading. Needed before W3.
      OWNER ANSWER: (a) The `VERTICAL` audience is all active members of the vertical.

O11-5 Deep-link scheme for the mobile app (out of repo).
      Plan recommendation: owner supplies; until then Phase 11 emits an opaque path
      string such as /review/{proposal_id}. Needed before closing W1's wording tests.
      OWNER ANSWER: **OWNER SUPPLIES** the app's deep-link scheme later; use an opaque path
      until then. STOP GATE only for the real scheme; opaque paths ship now.
```

Unanswered items that a workstream needs are a stop, not a guess. W1–W3 work with (a)
defaults only if the owner says "use the recommendations".

---

## 7. Workstreams (plan §8) and checkpoints

| WS | Work | Ends at |
|---|---|---|
| W1 | `0023` + endpoint helpers + `render_notification`, `list_notifications`, `mark_read`. | C1 |
| W2 | `deliver_pending`: claim in one transaction, send outside it, attempt insert, state update, backoff, `EXHAUSTED`, no duplicate row. | C2 |
| W3 | Announcement fan-out (`VERTICAL`, `AH_VOLUNTEERS`) + `sweep_unfanned_announcements`. | C3 |
| W4 | `PushSender` + `FcmPushSender` (mode per O11-2), payload, 401/403 permanent, 429/5xx retry, collapse key. | C4 |
| W5 | `OtpSender` interface, `RecordingOtpSender`, `NotificationOtpSender`, unconfigured path. | C4 |
| W6 | `run_delivery_tick` wiring, mutation battery (ten, plan §7), audits, DoD battery, §15. | final |

Checkpoint green = full `pytest` green + ruff/mypy clean + tree runnable. W4 is gated by
O11-2; W5 by O11-1 (the interface can land before the provider). Never leave a
half-applied migration.

Cut-off safety: each checkpoint must leave a green full suite.

---

## 8. Tests and mutations (plan §7)

New: `tests/test_notification_delivery.py`, `tests/test_notification_rendering.py`,
`tests/test_notification_list.py`, `tests/test_announcement_fanout.py`,
`tests/test_push_endpoints.py`, `tests/test_fcm_sender.py`, `tests/test_otp_sender.py`.

Must cover: success/retry/permanent-failure/max-attempts/backoff; no duplicate notification
row; a failing provider does not roll back the source change; sanitized attempt errors;
state-accurate wording (assigned-awaiting vs accepted, cancelled, completion, decision
message); `PROPOSAL_READY` deep link to the review screen; self-only list and idempotent
`mark_read` (another person's notification id → denial, not silence); fan-out to all active
vertical members and to an AH's volunteers, inactive
excluded, no reports → zero rows, idempotent sweep; 0023 constraints (unique token, active
index, FK); FCM payload/auth/401/429; OTP code never logged, unconfigured error.

Then the ten mutations in plan §7 one at a time, recording the exact red test and observed
assertion; restore and verify each restore.

---

## 9. Final battery (in this order, real output)

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m backend --version
.venv\Scripts\python.exe -m pytest tests/test_notification_delivery.py tests/test_notification_rendering.py tests/test_notification_list.py
.venv\Scripts\python.exe -m pytest tests/test_announcement_fanout.py tests/test_push_endpoints.py tests/test_fcm_sender.py tests/test_otp_sender.py
.venv\Scripts\python.exe -m pytest tests/test_review_service.py tests/test_task_service.py tests/test_approval_service.py
git diff --check
git status --short
```

Migration check (as a test): `0023` applies/re-applies, `PRAGMA foreign_key_check` empty,
duplicate token rejected, inactive endpoint not selected.

Pass: full suite green, ruff/mypy clean, ten mutations red, audits clean, no network in the
suite, no secret in any attempt/row/report, nothing staged.

---

## 10. Read-only audit (no edits)

```powershell
Select-String -Path backend/notification_delivery.py -Pattern 'token|phone|code'
Select-String -Path backend -Pattern 'INSERT INTO notification'
Select-String -Path backend -Pattern 'audience_scope'
Select-String -Path backend -Pattern 'fcm|googleapis'
git status --short
```

Expected: secrets only in provider payload construction, never logged/repr'd; notification
inserts only in `notification_events.py`; no `audience_scope` code path; FCM URLs only in
the sender; nothing staged.

---

## 11. Plan update (one edit, at the very end)

Append `## 15. Implementation record` to `docs/PHASE11_PLAN.md` in the shape of
`docs/PHASE6_PLAN.md` §15: files changed, real test totals, RED evidence, mutation table,
battery output, audits, deviations, gaps/next step, and which O11 answers were used.

Update `docs/PLANNING_PROGRESS.md` with one status line.

---

## 12. Report and stop

Use the `AGENTS.md` completion-report format: summary; SRS IDs addressed (FR-NOT-01..07,
FR-REV-10 delivery half, AC-17, NFR-SEC-01/03, NFR-AUD-03, EIR-04); files changed;
tests/checks run with actual outcomes; known gaps/risks (no provider configured, FCM
deprecation, at-least-once caveat); suggested next step.

Then **stop and report. Do not commit, do not stage, do not start Phase 12.**
