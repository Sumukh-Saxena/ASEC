# ASEC Phase 12 Plan — API, Authentication and Dashboard Backends

Status: planning only, 2026-10-29. No code, migration or test is changed by this
document. Authority: `docs/SRS.md`, `AGENTS.md`, `docs/DECISIONS.md` §A/§B,
`docs/ROADMAP.md`. Phase 12 is last: it needs the services and authorization built in
Phases 6–11. Tests-first applies to every workstream.

---

## 1. Verified baseline (pre-Phase-12 facts, 2026-10-29; re-verify at execution)

| Check | Planned expectation |
|---|---|
| HEAD | Phase 11 committed (Phase 10 may be in review) |
| `pytest` / `ruff` / `mypy` | green / clean / clean |
| migrations | `0001`–`0023` |
| services | `review.py`, `tasks.py`, `approvals.py`, `notification_delivery.py`, `evaluation.py`, `discord_adapter.py` present |
| authorization | `backend/authorization.py` (`record_denial`, `require_active_actor`) used by every service |
| notifications | `OtpSender` interface from Phase 11; provider may still be undecided |

Real facts verified today:

- `authorization_denial(id, actor_person_id, operation, subject_type, subject_id,
  reason, attempted_at)` exists in `0015`; `backend/review.py` records denials in its own
  transaction.
- `person` (0016) has `role IN ('Admin','Head','AH','Volunteer')`, `phone` and
  `roll_number` NOT NULL, `reports_to` FK, `active`; no auth/session/OTP table exists.
- `backend/config.py` redacts key names containing `TOKEN/SECRET/PASSWORD/KEY/PHONE/OTP`;
  `Config.require` is the only value access.
- `FR-AUT-04` matrix is a 12-operation × 4-role table in `docs/SRS.md` §3.9; each cell
  needs an automated test.
- `pyproject.toml` has `dependencies = []`; O12-1 is answered: a FastAPI-class framework
  in an optional `api` extra confined to `backend/api` (see §4.2, rewrite at phase start).
- Phase 11 ships `OtpSender` and a test sender; FR-ROS-03 is otherwise unimplemented.

---

## 2. Scope, non-goals, and boundary

### 2.1 In scope

| # | Slice | Requirements |
|---|---|---|
| S1 | 0024 OTP/session/attempt schema and `backend/auth.py` | FR-ROS-03, FR-AUT-01/07, NFR-SEC-01 |
| S2 | HTTP transport and routing (framework per O12-1), JSON errors, request IDs | EIR-02, NFR-SEC-01 |
| S3 | Route surface for review, tasks, approvals, notifications, admin | FR-AUT-02, FR-UI-05 |
| S4 | Authorization enforcement per operation + denial recording | FR-AUT-01..08, AC-20 |
| S5 | FR-AUT-04 matrix tests: every cell | FR-AUT-04 |
| S6 | Hierarchy visibility and field filtering | NFR-SEC-03 |
| S7 | Dashboard payloads (volunteer/AH/Head/Admin) without UI | FR-UI-02/03/04/05, NFR-REL-06 |
| S8 | Tests, mutations, DoD | all above |

### 2.2 Non-goals

- No mobile app, web UI, HTML templates, CSS or push notification client (FR-UI-01/06/07
  and the UI halves of 02–04 are out of repo).
- No new domain behaviour: routes call existing services; no SQL in handlers beyond
  authentication/session and admin reads.
- No background worker start from the API process; it stays a separate entry point.
- No new dependency outside the O12-1 optional `api` extra (FastAPI-class framework
  confined to `backend/api`, exact pin, core tests green without it).
- No roster import/edit endpoint (FR-ROS-02 says the MVP has none); no hard delete.
- No OTP provider invention (Phase 11 O11-1); tests use a recording sender.
- No SRS/DECISIONS edit.

### 2.3 Boundary rule

The API is a thin, authenticated adapter: parse, authenticate, call one service, map
errors, serialize. Authorization is re-checked inside the service; a route-level check is
defence in depth, never the only check. UI visibility is never authorization.

---

## 3. Reuse of real names

| Existing name | Where | Phase 12 use |
|---|---|---|
| `record_denial`, `require_active_actor` | `backend/authorization.py` | every protected route/service |
| `get_proposal_review`, `save_proposal_edit`, `accept_proposal`, `reject_proposal` | `backend/review.py` | review routes |
| `visible_tasks`, `claim_open_task`, `set_agreement`, `update_work_status`, `edit_task`, `reassign_task`, `cancel_task`, `task_progress` | `backend/tasks.py` | task routes |
| `decide_request`, `list_pending_approvals`, `get_approval_request` | `backend/approvals.py` | approval routes |
| `list_notifications`, `mark_read`, `register_endpoint`, `deactivate_endpoint` | `backend/notification_delivery.py` | notification routes |
| `pipeline_backlog`, `processing_queue` | `backend/orchestration.py` | admin routes |
| `list_reviewed_proposals_for_eval` | `backend/review.py` | Admin-only export |
| `OtpSender` | `backend/notification_delivery.py` | OTP delivery |
| `run_evaluation`, evaluation tables | `backend/evaluation.py`, 0022 | Admin eval view |
| `connect`, `transaction` | `backend/db/connection.py` | one connection per request |
| `Config.get/require` | `backend/config.py` | host/port/session/OTP/rate values |

New modules: `backend/api/__init__.py`, `backend/api/server.py` (transport + routing),
`backend/api/handlers.py` (request → service), `backend/api/errors.py`,
`backend/api/serializers.py`; `backend/auth.py` (domain auth). `api` imports services;
services never import `api`.

---

## 4. Design

### 4.1 Authentication (FR-ROS-03, FR-AUT-01/07)

```python
@dataclass(frozen=True)
class AuthenticatedPerson:
    person_id: int
    role: str
    vertical_ids: tuple[int, ...]

def request_otp(connection, *, roll_number: str, sender: OtpSender,
                now: str | None = None) -> None
def verify_otp(connection, *, roll_number: str, code: str,
               now: str | None = None) -> Session
def authenticate(connection, token: str, *, now: str | None = None) -> AuthenticatedPerson
def revoke_session(connection, token: str, *, now: str | None = None) -> bool
```

- `request_otp` resolves the roll number to exactly one active roster person. The phone
  is read from the roster, never from the request. Unknown, inactive and valid roll
  numbers get the **same** response and comparable timing; the attempt is recorded with a
  reason, and only a valid active person triggers `sender.send(phone, code)`.
- The code is 6 digits from `secrets`, stored only as `PBKDF2-HMAC-SHA256(code, salt)`
  with a random 16-byte salt; expiry 5 minutes; max 5 verification attempts; one active
  challenge per person (a new request marks the previous unconsumed challenge consumed).
- `verify_otp` uses `hmac.compare_digest`, rejects expired/consumed/exhausted codes,
  increments `attempts` even on failure, and on success creates a session:
  `secrets.token_urlsafe(32)` returned once, `SHA-256` hash stored. Session lifetime is
  configuration (proposal: 12 h; O12-3).
- `authenticate` hashes the bearer token, requires an unexpired, unrevoked session and an
  active person; it returns the current role and verticals, never a role supplied by the
  client (FR-AUT-05).
- Deactivation blocks sign-in and new assignment (FR-AUT-07); existing sessions fail on
  the next request because the person is inactive. Attribution stays.
- Rate limits are DB-backed via `auth_attempt`: per roll-number hash, e.g. 3 OTP requests
  and 10 verify failures per 15 minutes; exceeding returns the same public response (or
  429 for verify) and records the attempt. Values are configuration (O12-3).
- No password, no refresh token, no cookie; `Authorization: Bearer <token>` only, so no
  CSRF surface. TLS termination is deployment (O12-6).

### 4.2 HTTP transport (O12-1) — REWRITE AT PHASE START

> **Do not execute the bullets below as written.** O12-1 (`docs/OWNER_ANSWERS.md`) chose
> **(b) a mature framework (FastAPI-class) in an optional `api` extra**, confined to
> `backend/api`, with domain services framework-free. The stdlib sketch that follows is the
> pre-decision draft, kept only as a record of what the section covered.
>
> **Rewrite at phase start.** The rewritten section must cover, at minimum:
> 1. **Dependency policy (`docs/OWNER_ANSWERS.md`).** The core backend stays
>    dependency-free; the framework and its server are allowed only as an optional `api`
>    extra in `pyproject.toml`, pinned to an exact version at least two weeks old, imported
>    only under `backend/api`, with all core tests passing without the extra installed. A
>    missing extra must fail with a clear configuration error, not an import error in the
>    domain packages.
> 2. **Import direction.** `backend/api` may import domain services; no domain module may
>    import the framework. Enforce it with an import test (AST/regex over `backend/`).
> 3. **App factory and lifespan.** One `create_app(...)` taking `connection_factory`,
>    `now`, `config`; no module-level app, no global connection; the worker is never started
>    by the API process.
> 4. **Dependency-injection seam** replacing `handler(request, context)`: a per-request
>    dependency that opens one SQLite connection, sets WAL/foreign keys as today, closes it,
>    and attaches the authenticated person; handlers stay thin service calls.
> 5. **Validation and errors.** Framework request models for bodies/params (still rejecting
>    unknown fields), an exception handler mapping `ApiError` and domain exceptions to the
>    same status/code table (404/403/409/422/401/429/500), request IDs, and no stack traces,
>    SQL, paths or secrets in responses.
> 6. **Auth dependency.** Bearer token → `authenticate` → 401/403 with denial recording;
>    route-level checks remain defence in depth, never the only check.
> 7. **Route surface unchanged.** The §4.3 paths, methods and status codes stay the contract
>    (O12-2); the rewrite changes transport mechanics only.
> 8. **Tests.** Handler-level tests stay framework-free where possible; the localhost smoke
>    test uses the framework's test client or a real ephemeral-port server; the suite must
>    still pass on a clean install with the `api` extra absent for every non-API test.
> 9. **Deployment.** `python -m backend.api` (or the framework's runner) reads host/port
>    from config; TLS/hosting stays O12-6; no auto-migrate on start.

The pre-decision draft, for reference only:

- Recommended default: stdlib `http.server.ThreadingHTTPServer` +
  `BaseHTTPRequestHandler`, a small regex routing table, `json` bodies, one SQLite
  connection per request (`connect(path)`), `Content-Type: application/json; charset=utf-8`.
  No dependency; fits one backend host (PB-4).
- ~~Alternative: a FastAPI-class dependency. It adds install/deploy weight and is not
  needed for twelve endpoints; the owner decides (O12-1).~~ Superseded: O12-1 chose the
  FastAPI-class framework in the optional `api` extra.
- Handlers are pure functions `handler(request, context) -> (status, payload)` where
  `context` carries the connection, `now` and the authenticated person. This makes the
  whole surface testable without sockets; one localhost smoke test exercises the real
  server on an ephemeral port.
- Request limits: body size cap (e.g. 64 KiB), JSON object required, unknown fields
  rejected per route, UTF-8 only. Responses never include stack traces, SQL, file paths or
  secrets.
- Errors: `ApiError(status, code)`; domain mapping: not-found → 404, authorization → 403
  (denial already recorded by the service), stale version → 409 with the current snapshot,
  capacity → 409, validation → 422, bad/expired token → 401, rate limit → 429, unexpected →
  500 with a request id only. A 500 is logged server-side with the request id and no
  payload content.
- The server is started by `python -m backend.api` (host/port from config) and never
  auto-starts a worker.

### 4.3 Minimum route surface (owner approves, O12-2)

Auth: `POST /auth/otp`, `POST /auth/verify`, `POST /auth/logout`, `GET /me`.

| Area | Routes | Service |
|---|---|---|
| Review | `GET /proposals/{id}`, `POST /proposals/{id}/edits`, `POST /proposals/{id}/accept`, `POST /proposals/{id}/reject` | `review.py` |
| Tasks | `GET /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/claims`, `POST /tasks/{id}/agreement`, `POST /tasks/{id}/status`, `PATCH /tasks/{id}`, `POST /tasks/{id}/reassign`, `POST /tasks/{id}/cancel` | `tasks.py` |
| Approvals | `GET /approvals`, `GET /approvals/{id}`, `POST /approvals/{id}/decision` | `approvals.py` |
| Notifications | `GET /notifications`, `POST /notifications/{id}/read`, `POST /push-endpoints`, `DELETE /push-endpoints/{id}` | `notification_delivery.py` |
| Dashboards | `GET /dashboard/volunteer`, `/ah`, `/head`, `/admin` | read models below |
| Admin | `GET /admin/backlog`, `GET /admin/attempts`, `GET /admin/denials`, `GET /admin/evaluation/export`, `GET /admin/config` | `orchestration.py`, `review.py`, `evaluation.py` |

- `PATCH /tasks/{id}` carries `expected_version`; a stale value returns 409 with the
  current task snapshot (FR-TSK-12/AC-19).
- `POST /tasks/{id}/claims` is the AC-20 target: a Volunteer may call it only for their
  own vertical; any other caller is rejected and recorded. There is no "assign to
  someone" route for volunteers — reassignment is `POST /tasks/{id}/reassign` and is
  restricted to creator/Head/Admin. AC-20's "assign API" maps to this surface.
- `GET /admin/evaluation/export` calls the Admin-only Phase 6 export; no other role can
  reach it, and the denial is recorded.
- No roster route: FR-ROS-02 says the MVP has no import/sync/edit flow. The matrix's
  "Change roster data — Admin Yes" is satisfied by an owner-run operational process, not
  an API (O12-4).

### 4.4 Authorization matrix (FR-AUT-01..08)

Every operation is classified in one table shared by the routes and the tests, with rows
matching the SRS matrix exactly: receive/review own proposals, edit/accept/reject,
create task by accepting, raise cross-vertical, approve/deny cross-vertical, edit/
reassign/cancel task, claim open task, accept/deny/status own assignment, see per-volunteer
progress, push announcements, view processing records, change roster data.

- Admin = override where the matrix says Yes/Override; Head = own proposals/tasks they
  created/responsible vertical; AH = own proposals/tasks they created/volunteers under
  them; Volunteer = own tasks/assignments/claims only. Every scope check reads the
  database, not the request.
- A rejected call writes `authorization_denial` with `operation`, `subject_type`,
  `subject_id`, `reason` before the 403 (FR-AUT-06). The reason is a stable code
  (`NOT_ROLE`, `NOT_OWNER`, `NOT_VERTICAL`, `INACTIVE`, `NOT_RECIPIENT`), never a free-text
  message from the client.
- Announcements ("push announcements") are accepted through review; there is no separate
  broadcast route. The matrix cell maps to accepting an announcement proposal, which
  Phase 6/8 already authorize by sender role.
- FR-AUT-08: no route reads message content to decide authority; content fields are
  outputs only.

### 4.5 Hierarchy visibility (NFR-SEC-03)

```python
def can_view_person(connection, actor: AuthenticatedPerson, target_person_id: int) -> bool
def visible_fields(connection, actor, target) -> frozenset[str]
```

- Admin: everyone; Head: everyone in their vertical(s); AH: volunteers whose
  `reports_to` is the AH; Volunteer: self.
- Phone and message content are returned only when `can_view_person` is true. Exceptions:
  (a) a task creator sees the contact details of that task's assignees; (b) the reviewer
  sees the other messages in the proposal's bucket (Phase 6 view); (c) an approver sees
  the task description and requester note (Phase 8 view).
- Serializers take an allow-list of fields; a missing permission removes the field, it
  never returns `null` that implies absence. Hidden tasks (`PENDING`/`DENIED`) and
  cancelled tasks never appear because every task query goes through `visible_tasks`.
- `GET /dashboard/volunteer` returns own tasks + open tasks in own verticals +
  notifications; `/ah` adds review queue, created tasks with progress, volunteers under
  the AH, pending approvals as recipient; `/head` adds the vertical view and affected
  cross-vertical tasks; `/admin` returns backlog, attempt counts, denials, evaluation
  export availability and config **key names only**.
- NFR-REL-06: the admin backlog payload includes count and age of the oldest bucket from
  `pipeline_backlog`; overload is visible as delay, never silent.

### 4.6 Errors, logging and security

- Authentication failures and denials are recorded; error responses are uniform
  (`{"error": {"code": "...", "message": "..."}}`) and never reveal whether a roll number
  exists.
- Logs: request id, method, route, status, actor id (if authenticated), latency; never
  token, OTP, phone, message content or SQL. `Config.as_public_dict` is the only way to
  expose configuration (names only).
- One connection per request; `BEGIN IMMEDIATE` inside services; no shared connection
  between threads. The server is single-host and relies on WAL.
- CORS is not opened by default; the mobile app is a native client (EIR-02). No wildcard.
- All routes require authentication except `/auth/otp` and `/auth/verify` (NFR-SEC-01).
- The API never calls Discord, AI or FCM directly; it calls backend services (EIR-02,
  architecture rule).

---

## 5. Exact migration SQL (planned, full file)

### `0024_auth_sessions.sql` (FR-ROS-03, FR-AUT-01)

```sql
-- OTP challenges store only a salted hash; the code is never stored.
CREATE TABLE otp_challenge (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL,
    code_salt   TEXT NOT NULL,
    code_hash   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    consumed_at TEXT,
    CHECK (length(trim(code_salt)) > 0),
    CHECK (length(trim(code_hash)) > 0),
    CHECK (length(trim(created_at)) > 0),
    CHECK (length(trim(expires_at)) > 0),
    CHECK (attempts >= 0),
    CHECK (consumed_at IS NULL OR length(trim(consumed_at)) > 0),
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT
);

CREATE INDEX ix_otp_challenge_person ON otp_challenge (person_id, created_at);

-- Session tokens are stored as SHA-256 hashes; the plaintext exists only in the
-- response to a successful verify.
CREATE TABLE auth_session (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    issued_at  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    CHECK (length(trim(token_hash)) > 0),
    CHECK (length(trim(issued_at)) > 0),
    CHECK (length(trim(expires_at)) > 0),
    CHECK (revoked_at IS NULL OR length(trim(revoked_at)) > 0),
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT
);

CREATE INDEX ix_auth_session_person ON auth_session (person_id, expires_at);

-- Rate-limit and audit evidence. The roll number itself is not stored; its hash
-- is enough to count attempts and cannot be used to sign in.
CREATE TABLE auth_attempt (
    id               INTEGER PRIMARY KEY,
    roll_number_hash TEXT NOT NULL,
    kind             TEXT NOT NULL,
    attempted_at     TEXT NOT NULL,
    succeeded        INTEGER NOT NULL,
    reason           TEXT,
    CHECK (length(trim(roll_number_hash)) > 0),
    CHECK (length(trim(kind)) > 0),
    CHECK (length(trim(attempted_at)) > 0),
    CHECK (succeeded IN (0, 1)),
    CHECK (reason IS NULL OR length(trim(reason)) > 0)
);

CREATE INDEX ix_auth_attempt_roll ON auth_attempt (roll_number_hash, attempted_at);
```

No table is altered; `person` already carries the roll number, phone, role and active
flag. Rows are never deleted by the application; expired sessions and consumed challenges
stay as evidence (NFR-AUD-03).

---

## 6. Traceability matrix

| Requirement(s) | Where implemented | Test |
|---|---|---|
| FR-ROS-01/02 | roster already loaded; no API import/edit flow (operational) | absence/404 test |
| FR-ROS-03, FR-AUT-07 | OTP to roster phone, sessions, inactive block | auth tests |
| FR-AUT-01/02 | every protected route authenticates and authorizes | route coverage test |
| FR-AUT-03 | Volunteer cannot assign/review/create | matrix cells |
| FR-AUT-04 | 12 × 4 matrix, one test per cell | parametrized matrix suite |
| FR-AUT-05 | current role from DB; history keeps old role | authz/history tests |
| FR-AUT-06 (recorder from 6), AC-20 | denial row before 403 | denial tests |
| FR-AUT-08 | no content-based authority | audit test |
| NFR-SEC-01/02 | bearer auth, secret redaction, no stack traces | error/security tests |
| NFR-SEC-03 | hierarchy field filtering + exceptions | visibility tests |
| FR-UI-05 | admin backlog/attempts/config payloads | dashboard tests |
| FR-UI-02/03/04 (data half only; requirement is OUT OF REPO) | dashboard payloads consumed by the app | dashboard tests |
| NFR-REL-06 | admin backlog count/age | backlog test |
| EIR-02/05 | app↔backend only; same rules every interface | architecture test |

Out of repo: FR-UI-01/06/07 and the rendering halves of FR-UI-02/03/04; mobile app and TLS
hosting. The roadmap therefore credits FR-UI-02/03/04 to OUT OF REPO, not to Phase 12.

---

## 7. Tests-first and mutations

| File | Covers |
|---|---|
| `tests/test_auth.py` (new) | request/verify, enumeration, expiry, attempts, reuse, inactive, revoke, token hashing, rate limit |
| `tests/test_api_routes.py` (new) | routing, JSON, auth header, body limits, status mapping, request ids |
| `tests/test_api_authorization_matrix.py` (new) | 48 cells, scope cases, denial rows, AC-20 |
| `tests/test_api_visibility.py` (new) | hierarchy fields, exceptions, hidden/cancelled tasks |
| `tests/test_api_dashboards.py` (new) | each role payload, admin-only export/backlog, no PENDING tasks |
| `tests/test_api_errors.py` (new) | no secrets/stack traces, uniform error shape, 401 vs 403 |
| `tests/test_migrations_auth.py` (new) | 0024 constraints, unique token hash, FK, no plaintext code |
| `tests/test_api_server_smoke.py` (new) | real server on localhost, one authenticated round trip |

Mutation list:

1. Trust a client-supplied `person_id`/role header → spoof test red.
2. Drop the server-side role check on assign/review → matrix red.
3. Skip `record_denial` on 403 → AC-20 red.
4. Return phone/content for a non-subordinate → visibility red.
5. Allow OTP reuse or expired code → auth red.
6. Store a token/code in plaintext → migration test red.
7. Different response for unknown vs known roll number → enumeration red.
8. Include `PENDING`/`DENIED` tasks in a dashboard → visibility red.
9. Let AH/Head reach the evaluation export → matrix red.
10. Return a stack trace/secret in a 500 → error test red.

---

## 8. Workstreams

| WS | Content | Stop |
|---|---|---|
| W1 | 0024 + `auth.py` (OTP, sessions, rate limit) | C1 |
| W2 | transport/routing/errors/serializers (O12-1) | C2 |
| W3 | review + task routes wired to services | C3 |
| W4 | approvals + notifications + admin routes | C3 |
| W5 | matrix table + 48-cell tests | C4 |
| W6 | visibility + dashboard payloads | C5 |
| W7 | mutations, audits, DoD, §15 record | final |

W2 is gated on O12-1; W1 can proceed with handler-level tests. No route may be added
without a matrix classification (test enforces route coverage).

---

## 9. OWNER decisions for this phase

| ID | Question | Options | Recommendation |
|---|---|---|---|
| O12-1 | HTTP framework | **ANSWERED (`docs/OWNER_ANSWERS.md`): (b) a mature framework (FastAPI-class) in an optional `api` extra, confined to `backend/api`, domain services framework-free** | decided; §4.2 is marked "rewrite at phase start" and must be rewritten before W2 |
| O12-2 | Approve the minimum route surface (§4.3) as the mobile contract | approve / amend | approve; it mirrors existing services |
| O12-3 | Session lifetime, OTP expiry, attempts, rate limits | (a) 12 h, 5 min, 5, 3/15 min; (b) owner values | (a), configuration-only |
| O12-4 | Roster changes have no API in the MVP; Admin changes are operational | (a) confirm; (b) add an Admin endpoint | (a), FR-ROS-02; adding a route needs a new requirement |
| O12-5 | Approve the FR-AUT-04 matrix as the production baseline | approve / amend cells | approve before production; tests already encode it |
| O12-6 | Mobile app repo/contract ownership, TLS/hosting arrangement | owner supplies | needed before any real client connects |
| O12-7 | Retention rules for sessions/denials/attempts | (a) keep as evidence, archive per DR-07; (b) other | (a), consistent with the archive-only default |

---

## 10. SRS ambiguities carried by this phase

1. **Roster edits** — FR-ROS-02 forbids MVP import/edit flows, while the FR-AUT-04 matrix
   gives Admin "Yes" for roster changes. No route is added; O12-4 confirms.
2. **Admin task status** — FR-TSK-06 allows Admin; the matrix cell is "—". Phase 7
   implements the FR-TSK-06 override and the matrix test records it as an accepted
   exception (flagged).
3. **Session/OTP values** — the SRS gives none; O12-3 supplies them.
4. **OTP channel** — FR-ROS-03 says SMS; EIR-04 says FCM; O11-1 decides. Phase 12 uses
   whichever `OtpSender` is configured.
5. **"Push announcements"** — no separate broadcast operation exists; the matrix cell maps
   to accepting an announcement proposal (Phase 6/11).
6. **FR-UI-05 "configuration"** — the API exposes key names and non-secret values only;
   secrets are never returned (NFR-SEC-01).

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
.venv\Scripts\python.exe -m pytest tests/test_auth.py tests/test_migrations_auth.py
.venv\Scripts\python.exe -m pytest tests/test_api_authorization_matrix.py tests/test_api_routes.py
.venv\Scripts\python.exe -m pytest tests/test_api_visibility.py tests/test_api_dashboards.py tests/test_api_errors.py tests/test_api_server_smoke.py
```

Migration check: 0024 applies/re-applies, `PRAGMA foreign_key_check` empty, duplicate
token hash rejected, no column stores a plaintext code.

Read-only audits:

- `Select-String -Path backend/api -Pattern 'print\(|traceback|token|code|phone'` → no
  secret/content logging.
- `Select-String -Path backend/api -Pattern 'SELECT|INSERT|UPDATE|DELETE'` → only auth
  session reads and admin reads; domain writes go through services.
- `Select-String -Path backend/api -Pattern 'role\s*=\s*request|X-Person|X-Role'` → none.
- Route-to-matrix coverage test: every registered route has a matrix row.
- `git status --short` → intended files only; nothing staged.

Pass: full suite green, ruff/mypy clean, 48 matrix cells pass, ten mutations red, audits
clean, no secret in any response/log.

---

## 12. Risks and checkpoints

| Risk | Mitigation |
|---|---|
| Authorization drift between route and service | services remain authoritative; matrix tests |
| Hidden/cancelled task leak | `visible_tasks` only; dashboard tests |
| Token/OTP leak | hashes at rest, redaction, audits |
| Enumeration | uniform responses + timing note |
| Framework choice late blocks all routes | O12-1 first; W1 proceeds independently |
| Roster edit gap | O12-4; no invented endpoint |
| Threaded server + SQLite | one connection per request; WAL; no shared connection |

| Checkpoint | Green state |
|---|---|
| C1 | 0024 + auth; suite green |
| C2 | transport/routing/errors; handler tests green |
| C3 | service routes wired; suite green |
| C4 | 48-cell matrix green, denials recorded |
| C5 | visibility/dashboards green; smoke test green |
| Final | mutations/audits/DoD observed; §15 record written |

---

## 13. Appendix — verbatim requirements quoted

- **FR-ROS-01** The roster (name as used in chat, institute roll number, Discord ID,
  phone number, role, vertical(s), reports-to line) shall be loaded before the festival.
- **FR-ROS-02** The roster is fixed during the festival. The MVP has no import, sync or
  edit flows, and only Admin can change roster data.
- **FR-ROS-03** Every sign-in to the app shall resolve to exactly one roster person. The
  person signs in with their institute roll number, and a one-time password (OTP) is sent
  to the phone number the roster holds for that roll number, never to a number typed at
  sign-in.
- **FR-AUT-01** The backend shall authorize every protected operation using server-side
  identity, role and scope. Hiding a button is not authorization. A direct API call shall
  get the same result as the app.
- **FR-AUT-02** Protected operations include: proposal review, task creation and edit,
  assignment, reassignment, cancellation, status change, cross-vertical approval,
  announcements, viewing system internals, and roster changes.
- **FR-AUT-03** A volunteer shall not assign tasks to others, review proposals or create
  tasks.
- **FR-AUT-04** The matrix below is the baseline. It shall be approved by the project
  owner before production, and each cell shall have an automated test.
- **FR-AUT-05** Authorization uses the person's current role. History keeps the role and
  vertical in force at the time of each action.
- **FR-AUT-06** Unauthorized attempts shall be rejected and recorded.
- **FR-AUT-07** A person referenced by any record shall not be hard-deleted. Deactivation
  shall block sign-in and new assignment while keeping attribution.
- **FR-AUT-08** A message's content shall never grant authority. Authority comes only
  from the authorization model.
- **EIR-02** App and backend. The app talks only to the backend API. Every rule in this
  SRS is enforced there.
- **EIR-05** Authorization. Every interface enforces the same rules.
- **NFR-SEC-01** All access requires authentication. Discord, model and push credentials
  are held in secure configuration and never exposed to users or logged in errors.
- **NFR-SEC-02** Source evidence, processing records and audit history shall be protected
  from unauthorized access or change.
- **NFR-SEC-03** Phone numbers and message content are visible to a person only for people
  below them in the roster hierarchy. Admin sees everyone, a Head sees everyone in their
  vertical, and an AH sees the volunteers who report to them. A volunteer sees only their
  own messages and tasks. Exceptions: (a) a task's creator sees the contact details of its
  assignees, (b) the review screen shows the reviewer the other messages in the proposal's
  bucket, and (c) an approval request (FR-XV-03) shows the approver the task description
  and the requester's note, not the source chat.
- **FR-AUT-04 matrix** (rows): Receive and review own proposals; Edit, accept or reject a
  proposal; Create a task in own vertical (by accepting); Raise a cross-vertical task;
  Approve or deny a cross-vertical request; Edit, reassign or cancel a task; Claim an open
  task; Accept or deny an assigned task; update own work status; See per-volunteer
  progress; Push announcements; View processing records, model results, configuration;
  Change roster data.
- **AC-20** A volunteer calls the assign API directly → Rejected and logged.
