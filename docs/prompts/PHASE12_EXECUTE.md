# PHASE 12 EXECUTION PROMPT — API, Authentication and Dashboard Backends

Paste this whole file into a fresh session. It is self-contained. This is the last phase.

---

## 0. What you are doing

Execute **Phase 12** of ASEC as approved in `docs/PHASE12_PLAN.md`: the OTP/session
authentication, the HTTP transport and route surface, server-side authorization on every
operation with recorded denials, the FR-AUT-04 12x4 matrix tests, NFR-SEC-03 hierarchy
visibility, and the dashboard/admin payloads. Migration `0024`. Tests first. Leave the
repository green at every checkpoint. **Do not commit.**

---

## 1. Context reads (in this order, before any edit)

1. `AGENTS.md`.
2. `docs/SRS.md` §3.9 (FR-AUT-01..08 and the matrix table), §3.10 (FR-UI-01..05),
   §3.11/roster FR-ROS-01..03, §4/EIR-02/EIR-05, §6.5, AC-20.
3. `docs/DECISIONS.md` §A/§B (PB-3, PB-4, PB-5, OD-REV-6, retention) — approved; do not
   re-open.
4. `docs/ROADMAP.md` §2 (contracts), §7 (owner inputs).
5. `docs/PHASE12_PLAN.md` — the approved plan (§4 design, §5 exact SQL, §6 traceability,
   §7 tests/mutations, §8 workstreams, §9 owner items, §11 battery, §13 verbatim
   requirements).
6. The §15 record of every earlier phase, for the real service names and counts.
7. Real code: `backend/authorization.py`, `backend/review.py`, `backend/tasks.py`,
   `backend/approvals.py`, `backend/notification_delivery.py`, `backend/evaluation.py`,
   `backend/orchestration.py`, `backend/config.py`, `backend/db/connection.py`, migration
   `0015` (`authorization_denial`), `0016` (`person`).

Never open, print or reference `.env`, secrets, OTP codes or `session-*.md`.

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

Expected: Phases 7–11 committed; clean tree; plain `pytest` green at the latest recorded
count; ruff/mypy clean; migrations `0001`–`0023`; `backend/notification_delivery.py` with
`OtpSender`; `dependencies = []` unless an owner decision added one.

If the tree is red, **stop and report**.

---

## 3. Standard rules (non-negotiable)

- No commits, no staging, no pushes, no destructive git.
- No secret, token, OTP code, phone number or message content in a response body, a log, an
  error, a test fixture or a report.
- Do not edit `docs/SRS.md` or `docs/DECISIONS.md`.
- **No new dependency** without O12-1. The recommended transport is stdlib `http.server`.
- The API is a thin adapter: parse, authenticate, call one existing service, map errors,
  serialize. **No SQL in handlers** except authentication/session and admin reads.
- Authorization is enforced **server-side inside the service**; a route check is defence in
  depth only. UI visibility is never authorization.
- Never trust a client-supplied person id, role or vertical.
- Every denied protected operation writes `authorization_denial` before the 403.
- Do not add a route without a matrix classification; a test enforces coverage.
- No roster edit/import endpoint (FR-ROS-02).
- Do not implement the mobile app, HTML, CSS, TLS hosting or FCM client (out of repo).
- Tests first with real RED evidence; plain `pytest`; no network in the suite except one
  localhost smoke test.

---

## 4. Authorization statement

Authorized to:

- create `backend/auth.py` and `backend/api/` (`__init__.py`, `server.py`, `handlers.py`,
  `errors.py`, `serializers.py`);
- add `backend/db/migrations/0024_auth_sessions.sql` exactly as plan §5
  (`otp_challenge`, `auth_session`, `auth_attempt`);
- add any **additive** helper the routes need in existing services (no behaviour change);
- add/update tests under `tests/`;
- append `docs/PHASE12_PLAN.md` §15 and update `docs/PLANNING_PROGRESS.md`.

Not authorized to: change domain rules, add domain tables, add an approval round, change
notification timing, edit requirements, or implement the client app.

---

## 5. Approved decisions this phase relies on

| ID | What it fixes for you |
|---|---|
| **PB-5** | `person.role IN ('Admin','Head','AH','Volunteer')`, `phone`/`roll_number` NOT NULL, `reports_to` FK — sign-in resolves one person by roll number and reads the roster phone. |
| **PB-4** | SQLite + WAL, `BEGIN IMMEDIATE`, one backend host → one connection per request. |
| **OD-REV-6** | `task.creator_id` is the primary sender; the actor is recorded in history — do not derive authority from the creator. |
| **Phase 7 contract** | `visible_tasks`/`_live_task_row` is the only task filter; hidden `PENDING`/`DENIED` and cancelled tasks never reach a dashboard. |
| **Phase 6 contract** | `list_reviewed_proposals_for_eval` stays Admin-only and takes the authenticated actor. |
| **Phase 7–11 contract** | `record_denial`/`require_active_actor` and `stage_notification` are reused; no parallel recorder. |
| **Retention** | Sessions, denials and attempts stay as evidence; no hard delete. |

---

## 6. OWNER DECISIONS FOR THIS PHASE

```
O12-1 HTTP framework.
      Options: (a) stdlib http.server (no dependency); (b) FastAPI-class dependency.
      Plan recommendation: (a): twelve endpoints, one host, zero install weight.
      Needed before W2 (W1 can proceed with handler-level tests).
      OWNER ANSWER:

O12-2 Approve the minimum route surface (plan §4.3) as the mobile contract.
      Options: approve / amend.
      Plan recommendation: approve; it mirrors existing services. Needed before W3.
      OWNER ANSWER:

O12-3 Session lifetime, OTP expiry, attempts, rate limits.
      Options: (a) 12 h session, 5 min OTP, 5 verify attempts, 3 requests / 15 min;
      (b) owner values.
      Plan recommendation: (a), configuration-only. Needed before W1.
      OWNER ANSWER:

O12-4 Confirm roster changes have no API in the MVP; Admin changes are operational.
      Options: (a) confirm; (b) add an Admin endpoint.
      Plan recommendation: (a), FR-ROS-02. Needed before closing W3.
      OWNER ANSWER:

O12-5 Approve the FR-AUT-04 matrix as the production baseline (cells unchanged).
      Options: approve / amend cells.
      Plan recommendation: approve before production; the tests already encode it.
      Needed before W5.
      OWNER ANSWER:

O12-6 Mobile app repo/contract ownership and TLS/hosting arrangement.
      Plan recommendation: owner supplies; needed before any real client connects.
      OWNER ANSWER:

O12-7 Retention for sessions/denials/attempts.
      Options: (a) keep as evidence, archive per DR-07; (b) other.
      Plan recommendation: (a).
      OWNER ANSWER:
```

Unanswered items that a workstream needs are a stop, not a guess.

---

## 7. Workstreams (plan §8) and checkpoints

| WS | Work | Ends at |
|---|---|---|
| W1 | `0024` + `backend/auth.py`: request/verify OTP, hashing, expiry, attempts, sessions, revoke, `authenticate`, rate limits, uniform responses. | C1 |
| W2 | Transport: routing table, JSON bodies, size cap, error mapping, request ids, one connection per request (framework per O12-1). | C2 |
| W3 | Review + task routes wired to services (`review.py`, `tasks.py`), including `PATCH` with `expected_version` and the claim route (AC-20 target). | C3 |
| W4 | Approval + notification + admin routes (`approvals.py`, `notification_delivery.py`, `orchestration.py`, Admin eval export). | C3 |
| W5 | One shared operation→role/scope table + the 48-cell matrix tests + denial rows. | C4 |
| W6 | `can_view_person` / `visible_fields`, dashboard payloads (volunteer/AH/Head/Admin), backlog age/count. | C5 |
| W7 | Mutation battery (ten, plan §7), audits, DoD battery, §15. | final |

Checkpoint green = full `pytest` green + ruff/mypy clean + tree runnable. W2 is gated by
O12-1; W3–W5 by O12-2/O12-5. Never leave a half-applied migration.

Cut-off safety: each checkpoint must leave a green full suite.

---

## 8. Tests and mutations (plan §7)

New: `tests/test_auth.py`, `tests/test_api_routes.py`,
`tests/test_api_authorization_matrix.py`, `tests/test_api_visibility.py`,
`tests/test_api_dashboards.py`, `tests/test_api_errors.py`,
`tests/test_migrations_auth.py`, `tests/test_api_server_smoke.py`.

Must cover: OTP request/verify incl. unknown vs known roll number (same response, no
enumeration), expiry, attempt cap, reuse, inactive person, revoke; token stored hashed;
rate limit; routing and JSON/body-limit/status mapping; **every one of the 48 matrix cells**
(parametrized, with the operation name in the test id); AC-20 (Volunteer calling the claim
route for another vertical → 403 and a denial row); scope cases (non-owner, non-vertical,
non-recipient); hierarchy field filtering for phone/content incl. the three SRS exceptions;
dashboards never include hidden or cancelled tasks; Admin-only eval export; uniform error
shape with no stack trace/secret; 0024 constraints; one real localhost round trip.

Then the ten mutations in plan §7 one at a time, recording the exact red test and observed
assertion; restore and verify each restore.

---

## 9. Final battery (in this order, real output)

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m backend --version
.venv\Scripts\python.exe -m pytest tests/test_auth.py tests/test_migrations_auth.py
.venv\Scripts\python.exe -m pytest tests/test_api_authorization_matrix.py tests/test_api_routes.py
.venv\Scripts\python.exe -m pytest tests/test_api_visibility.py tests/test_api_dashboards.py tests/test_api_errors.py tests/test_api_server_smoke.py
git diff --check
git status --short
```

Migration check (as a test): `0024` applies/re-applies, `PRAGMA foreign_key_check` empty,
duplicate token hash rejected, no plaintext code column.

Pass: full suite green, ruff/mypy clean, 48 matrix cells green, ten mutations red, audits
clean, no secret in any response/log, nothing staged.

---

## 10. Read-only audit (no edits)

```powershell
Select-String -Path backend/api -Pattern 'print\(|traceback|token|code|phone'
Select-String -Path backend/api -Pattern 'SELECT|INSERT|UPDATE|DELETE'
Select-String -Path backend/api -Pattern 'role\s*=\s*request|X-Person|X-Role'
Select-String -Path backend -Pattern 'INSERT INTO authorization_denial'
git status --short
```

Expected: no secret/content logging; SQL only for auth/session and admin reads; no
client-supplied identity; one denial-insert site (the shared recorder); nothing staged.

---

## 11. Plan update (one edit, at the very end)

Append `## 15. Implementation record` to `docs/PHASE12_PLAN.md` in the shape of
`docs/PHASE6_PLAN.md` §15: files changed, real test totals, RED evidence, mutation table,
battery output, audits, the matrix coverage list, deviations, gaps/next step, and which
O12 answers were used.

Update `docs/PLANNING_PROGRESS.md` with one status line marking planning/execution status.

---

## 12. Report and stop

Use the `AGENTS.md` completion-report format: summary; SRS IDs addressed (FR-ROS-01..03,
FR-AUT-01..08, FR-UI-02..05 data halves, NFR-SEC-01/02/03, NFR-REL-06, EIR-02/05, AC-20);
files changed; tests/checks run with actual outcomes; known gaps/risks (out-of-repo app,
TLS hosting, roster-edit gap, OTP provider); suggested next step.

Then **stop and report. Do not commit, do not stage.** This is the last phase in the
roadmap; the next step is the owner's release/acceptance decision, not another phase.
