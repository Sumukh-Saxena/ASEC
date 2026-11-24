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
  configuration (7 days; O12-3).
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

### 4.2 HTTP transport (O12-1) - REWRITTEN AT PHASE START

> **Rewrite executed 2026-11-13 (Phase 12 Session A, W2).** O12-1 chose option (b): a
> mature framework in an optional `api` extra, confined to `backend/api`, with every
> domain service framework-free. This block replaces the 2026-10-29 stdlib draft (kept
> only in git history) and is the transport contract for the rest of Phase 12. Later
> workstreams add routes against it; none of these mechanics changes again.

#### 4.2.1 Dependency policy (`docs/OWNER_ANSWERS.md`)

- The core backend stays dependency-free: `dependencies = []` is unchanged and every
  non-API test passes on a clean install without the extra.
- `pyproject.toml` declares exactly one optional extra: `api = ["fastapi==0.162.0",
  "uvicorn==0.54.0"]`. Both pins are owner-supplied, exact versions released at least two
  weeks before 13 Nov 2026. No other direct dependency is added.
- The framework and its server are imported only inside `backend/api`. No domain module
  imports either package, so a missing extra can never break ingestion, bucketing,
  review, tasks, approvals, notifications, evaluation or the worker.
- A missing extra becomes `backend.api.ApiConfigurationError` (a `RuntimeError`
  subclass) at API import or creation time, with the install command in the message. It
  is never a bare `ModuleNotFoundError` from a domain import.
- Transitive packages (starlette, pydantic, anyio and the rest) are not pinned by this
  phase; their resolved versions are recorded in the checkpoint report so the owner can
  check their release dates.

#### 4.2.2 Import direction

- `backend/api` may import any domain service. No module outside `backend/api` may
  import `fastapi`, `uvicorn` or `starlette`, and no module outside `backend/api` and
  `backend/__main__.py` (the CLI boundary) may import `backend.api`.
- `backend/__main__.py` imports `backend.api` lazily inside the `serve-api` branch, so
  `python -m backend --version` and every existing subcommand work without the extra.
- A test walks `backend/**/*.py` with `ast` and enforces both directions; a second
  textual assertion catches a literal framework import that AST parsing would miss.

#### 4.2.3 App factory and lifespan

- `create_app(*, connection_factory, now, config, otp_sender=None)` builds one app. There
  is no module-level app instance, no global connection and no import-time side effect.
- Startup does nothing: no migrations, no worker, no scheduler, no connection. The worker
  remains a separate entry point (PB-4, plan §2.2). Nothing in the API process imports
  `backend.worker`.
- `FastAPI(...)` is created with `docs_url=None`, `redoc_url=None` and `openapi_url=None`
  so the document endpoints cannot become unclassified routes.
- `create_app` validates configuration at creation time (`load_auth_settings`); an
  invalid value raises `ConfigError` before the socket is bound.
- The app carries its dependencies on `app.state`: `connection_factory`, `now`,
  `auth_settings`, `otp_sender` and `config`. No handler reaches for a global.

#### 4.2.4 Per-request dependency seam

- One dependency, `_open_request_state`, opens exactly one SQLite connection through the
  injected `connection_factory` (the CLI passes `backend.db.connection.connect`, so WAL,
  `PRAGMA foreign_keys = ON` and the configured busy timeout are the same as everywhere
  else), yields a `RequestState(connection, now)`, and closes the connection in a
  `finally` block.
- `now` is the injected callable evaluated once per request; service calls receive that
  value, so a request sees one consistent instant and no module reads the wall clock.
- Handlers stay thin: parse the body, call one domain function, serialize the result.
  No SQL in a handler except the authentication/session calls the plan explicitly allows.
- Handlers and dependencies are `async def` but execute the existing synchronous service
  calls on the event-loop thread. That keeps the per-request connection on one thread
  (`sqlite3` default `check_same_thread=True`) and is the simplest correct choice for the
  single-host MVP. The cost is that one slow call, such as an OTP DM send, blocks other
  requests for its duration; this is recorded as a known risk, and moving delivery to
  the worker is the upgrade path, not part of this phase.

#### 4.2.5 Validation and errors

- Request bodies are Pydantic models with `extra="forbid"`, so an unknown field is a 422
  rather than a silently ignored value. The OTP and code fields are bounded strings; a
  body over 64 KiB is rejected before parsing.
- Every error response is one shape:
  `{"error": {"code": "<CODE>", "message": "<fixed text>", "request_id": "<hex>"}}`.
  The message is a constant per code; no detail, field name, input value, SQL, file path,
  token or OTP code is echoed.
- Fixed code table:

  | Status | Code | Used for |
  |---|---|---|
  | 401 | `UNAUTHENTICATED` | missing/malformed bearer token, wrong/expired/exhausted/reused code, revoked or expired session, inactive person |
  | 403 | `FORBIDDEN` | authorization denial (the service has already recorded it) |
  | 404 | `NOT_FOUND` | unknown path or subject |
  | 409 | `CONFLICT` | stale `expected_version`, capacity or state conflict |
  | 422 | `INVALID_REQUEST` | validation failure, unknown field, oversized body, unsupported method |
  | 429 | `RATE_LIMITED` | verify rate limit exceeded |
  | 500 | `INTERNAL_ERROR` | unexpected failure; generic body, logged server-side by request id only |

  A framework `HTTPException` outside the table maps to `INTERNAL_ERROR` when it is a
  server error and `INVALID_REQUEST` when it is a client error.
- Request IDs are generated server-side per request (`secrets.token_hex(8)`), returned in
  the `X-Request-ID` header and inside every error body. The server logs the id, method,
  route, status, actor id when authenticated, and latency; never a token, a code, a phone
  number, message content or SQL.
- A 500 is logged as the exception type and the request id only; the response is the
  fixed `INTERNAL_ERROR` body.

#### 4.2.6 Auth dependency

- `Authorization: Bearer <token>` only. No cookie, no CSRF surface, no session in a query
  string. A missing or malformed header is a 401 before any service call.
- The dependency hashes the token, calls `backend.auth.authenticate` with the request's
  `now`, and turns `AuthError` into the uniform 401. The authenticated person (id, role,
  verticals from the current roster row) is attached to the request state.
- Route-level role checks remain defence in depth only; the service re-checks
  authorization against the database, and denial recording stays in the shared
  `record_denial` path (FR-AUT-01/06).
- `/healthz`, `/auth/otp` and `/auth/verify` are the only public routes. Everything else
  requires a session.

#### 4.2.7 Route surface unchanged

- The §4.3 paths, methods and semantics are the contract (O12-2). The transport rewrite
  changes mechanics only; no route is renamed and no route is added without a §4.4
  classification.
- W2 ships only the auth block (`POST /auth/otp`, `POST /auth/verify`,
  `POST /auth/logout`, `GET /me`) plus `GET /healthz`. The review, task, approval,
  notification, dashboard and admin routes arrive in W3-W6.
- There is no roster route (O12-4, FR-ROS-02).

#### 4.2.8 Tests

- API tests live in `tests/test_api_*.py` and start with `pytest.importorskip("fastapi")`.
  That is the single sanctioned skip: it exists only so a clean install without the extra
  still passes the core suite. With the extra installed the modules run and are reported
  by name.
- The app is driven by a small in-repo ASGI caller (`app(scope, receive, send)` with
  stdlib `asyncio` only). No `httpx` and no test client dependency is added.
- One smoke test starts `uvicorn` on an ephemeral loopback port in a thread and makes a
  real `urllib` request, proving the served app is the one built by `create_app`.
- Domain tests (`tests/test_auth.py`, `tests/test_migrations_auth.py`) remain
  framework-free and are unchanged by this section.

#### 4.2.9 Deployment and CLI

- `python -m backend serve-api [--host H] [--port P]` imports `backend.api` lazily, reads
  host/port from configuration (`API_HOST`, default `127.0.0.1`; `API_PORT`, default
  8000), refuses a non-loopback host unless `API_ALLOW_NON_LOOPBACK` is explicitly true,
  and prints the bind address and status only.
- It never runs migrations, never starts the worker and never prints a token or a
  configuration value.
- TLS termination and hosting remain O12-6 (owner-supplied, out of repo).
### 4.3 Minimum route surface (owner approves, O12-2)

Auth: `POST /auth/otp`, `POST /auth/verify`, `POST /auth/logout`, `GET /me`.

| Area | Routes | Service |
|---|---|---|
| Review | `GET /proposals/{id}`, `POST /proposals/{id}/edits`, `POST /proposals/{id}/accept`, `POST /proposals/{id}/reject` | `review.py` |
| Tasks | `GET /tasks`, `GET /tasks/{id}`, `POST /tasks/{id}/claims`, `POST /tasks/{id}/agreement`, `POST /tasks/{id}/status`, `PATCH /tasks/{id}`, `POST /tasks/{id}/reassign`, `POST /tasks/{id}/cancel` | `tasks.py` |
| Approvals | `GET /approvals`, `GET /approvals/{id}`, `POST /approvals/{id}/decision` | `approvals.py` |
| Notifications | `GET /notifications`, `POST /notifications/{id}/read`, `POST /push-endpoints`, `DELETE /push-endpoints/{id}` | `notification_delivery.py` |
| Dashboards | `GET /dashboard/volunteer`, `/ah`, `/head`, `/admin` | read models below |
| Admin | `GET /admin/backlog`, `GET /admin/attempts`, `GET /admin/denials`, `GET /admin/evaluation/export`, `GET /admin/config`, `GET /admin/system`, `POST /admin/system/pause`, `POST /admin/system/resume` | `orchestration.py`, `review.py`, `evaluation.py`, `system_control.py` |

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

#### 4.7 Idempotency keys (owner A2, Session B)

Marked block added in Phase 12 Session B; it is the implementation contract for
migration `0025_idempotency.sql` and `backend/api/idempotency.py`.

- Scope: `POST /tasks/{id}/claims`, `/agreement`, `/status`, `/reassign`,
  `/cancel`, `POST /proposals/{id}/accept` and `/reject`,
  `POST /approvals/{id}/decision`, `POST /push-endpoints`. Reads, sign-in and
  `PATCH /tasks/{id}` are not idempotent-key routes; `PATCH` already carries
  `expected_version`.
- Header: `Idempotency-Key`, optional. When present it must be 16-64 characters
  from `A-Z a-z 0-9 . _ : -`; a malformed value is a 422 that does no work. The
  value is never logged and never returned.
- Storage: one row per `(person_id, route_key, key)`. `request_hash` is a
  SHA-256 over the request method, path and raw body, so the same key with a
  different request is a 422 `IDEMPOTENCY_KEY_REUSED`; a different person or a
  different route is independent.
- Replay: only a 2xx response is stored, and a replay returns the exact stored
  status and body without calling the service. A 4xx is never stored, so a
  corrected retry can succeed.
- Expiry: `API_IDEMPOTENCY_KEY_HOURS` (default 24). An expired row behaves as
  absent. `cleanup_expired(connection, now, batch_limit)` deletes expired rows
  in bounded batches; it is a function, not a scheduler.
- Limitations recorded in `docs/KNOWN_GAPS.md`: a crash between the service
  commit and the idempotency insert can lose the stored response (the retry
  then re-runs the operation, which the services treat idempotently where they
  can), and a 4xx response is deliberately not replayed.

---

#### 4.8 Matrix cells and visibility as built (Session C)

Marked block added in Phase 12 Session C; it records what W5/W6 actually built
and where the SRS matrix still needs an owner reading.

- The 48-cell `FR_AUT_OPERATIONS` table in `backend/api/matrix.py` is the
  single source of truth: every domain route's role set is derived from its
  row(s) via `Operation.matrix_operations`, and `ROUTE_OPERATIONS` maps each
  registered route to one operation. `tests/test_api_route_coverage.py` fails
  when a route lacks a row; `tests/test_api_authorization_matrix.py` exercises
  all 48 cells through the real routes plus completeness and vocabulary tests.
- Rows 2/3/4/10 share proposal acceptance, so the route takes their union;
  rows 5 and 11 are shared by the approval reads and the Admin reads. Task
  edit/reassign/cancel derive from row 6, which excludes Volunteer at the
  route gate (a Volunteer denial is `NOT_ROLE`, before the service's finer
  ownership reason).
- Row 8 (view processing records) gives Admin OVERRIDE and the other roles
  "—"; row 5 (approve/deny cross-vertical) gives Admin YES. Row 3's "own
  vertical" is implemented as the Phase 6 reviewer-owns-the-proposal rule; the
  SRS wording stays ambiguous for an AH reviewing another vertical's proposal.
- `PUSH_ANNOUNCEMENT` has no route of its own (plan §4.4); its Admin cell was
  marked `needs_owner=True` in Session C because row 2's proposal-accept
  override makes Admin acceptance reachable although the SRS writes "—" there.
  Session D owner decision A resolves it strictly; see §4.9.
- W6 adds only `GET /dashboard/volunteer`, `/ah`, `/head`, `/admin` and
  `GET /admin/backlog`. The three role dashboards read row 9's SEE_PROGRESS
  half and are restricted to their own role plus Admin's override; the two
  Admin routes read row 11's VIEW_INTERNALS half.
- Hierarchy visibility lives in `backend/visibility.py`: Admin everyone, Head
  own vertical(s), AH direct volunteer reports, Volunteer self. A person the
  caller cannot contact contributes base fields without `phone` when they
  appear in an otherwise readable task payload; the phone is absent, never
  `null`. Exception (a) is the creator/assignee task check; (b) and (c) remain
  the review and approval payloads. `GET /me` returns the caller's own name
  and phone.
- Dashboards are read-only compositions of `visible_tasks`, `task_progress`,
  the review queue, pending approvals, notifications and `pipeline_backlog`;
  they are unpaginated, a limit recorded in `docs/KNOWN_GAPS.md`.

---

#### 4.9 Owner decisions for Session D (binding, 2026-11-17)

Marked block added in Phase 12 Session D. These are the owner's decisions for
the last workstreams (D1-D4); where a decision and the SRS wording differ, the
decision records the approved reading and the implementation follows it.

- **A — announcements are strict.** Admin's `PUSH_ANNOUNCEMENT` cell stays "—".
  The matrix remains one table in `backend/api/matrix.py`; all other Admin
  overrides are unchanged. Because accepting an `ANNOUNCEMENT` proposal is the
  only announcement surface, `authorize_proposal_acceptance` re-reads the
  current role and the proposal type, records a denial against
  `PUSH_ANNOUNCEMENT` with reason `NOT_ROLE`, and refuses before the service.
- **B — the Admin view is small.** It shows stop/resume, export to the
  operator's PC and festival-at-a-glance counts only. The attempt, denial,
  evaluation and config blocks leave `GET /dashboard/admin`; their separate
  Admin routes remain.
- **C — two independent switches.** `PROCESSING` and `DELIVERY` pause
  independently. Ingestion always continues; a pause stops the worker's next
  processing tick or its next delivery pass, never message capture.
- **D — exports cover everything.** Reviews, tasks, approvals, processing
  records and notifications export to local files, masked by default under
  NFR-SEC-04 with `backend.masking.mask_pii`.
- **E — role dashboards stay uncluttered.** Volunteer, AH and Head dashboards
  keep the minimum useful rows; counts appear as badges, not extra lists.

---

#### 4.10 Exports as built (Session D3)

Marked block added in Phase 12 Session D. It records what owner decision D
became in `backend/export.py` and the `export` CLI.

- One JSON Lines file per dataset plus `manifest.json` (`format`,
  `masked`, `exported_at`, per-dataset counts). The manifest holds counts and
  flags only.
- The exported datasets are the domain records: verticals, persons and their
  verticals, source groups/clocks/progress, source messages, mentions,
  reactions, buckets and their messages, processing attempts, relevance inputs
  and decisions, proposal inputs/proposals/assignees/attributes/affected
  verticals/context/revisions/decisions, tasks and their assignees/
  attributes/affected verticals/history, approval requests/recipients/
  decisions, announcements, notifications/attempts/reads, authorization
  denials, latency measurements, replay batches, evaluation runs and case
  results, and the two system controls.
- Never exported, masked or not: the auth stores (`auth_attempt`,
  `auth_session`, `otp_challenge`, `idempotency_key`), push endpoints (tokens),
  and `person.roll_number` (the login credential).
- Masked by default: every string value passes through
  `backend.masking.mask_pii`, so phone, email, roll-number and government-ID
  shapes become their placeholders. `--unmasked` keeps the text but still
  excludes the credential columns and tables above.
- The source database is opened read-only (`mode=ro`); the export writes no
  row. SQLite may create its `-wal`/`-shm` sidecars next to a WAL database when
  it opens it. The destination directory must be absent or empty, so an
  operator file is never overwritten. The CLI prints counts and the mask flag
  only, never record content.

---

#### 4.11 Role dashboards as built (Session D4)

Marked block added in Phase 12 Session D. It records owner decision E in the
Volunteer, AH and Head payloads; the Admin view is unchanged from §4.9.

- Each dashboard keeps its existing lists as lists and adds two count-only
  objects and one flag map: `counts` (the true size of every list, even when the
  list is capped), `attention` (the subset that needs the caller to act) and
  `has_more` (one boolean per list, in payload order).
- `counts` keys are exactly the list names. `attention` keys are
  `unread_notifications` / `open_tasks` / `active_tasks` for Volunteer,
  `pending_review` / `approvals` / `active_created_tasks` for AH and
  `pending_review` / `approvals` / `active_tasks` for Head. "Active" is the
  existing reading of live accepted assignments: a task with no accepted,
  non-cancelled assignee, or one whose accepted assignees are not all
  `COMPLETED`, still needs work.
- Every list is capped at `API_DASHBOARD_LIST_LIMIT` (default 50, valid 1–200,
  `ApiReadSettings.dashboard_list_limit`). A service called with a non-positive
  limit raises `ValueError`; the handlers always pass the configured value.
  Counts and flags are computed before capping, so a badge never reports the
  capped size.

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
flag. Rows are never deleted by the application, except by the DR-07 retention procedure;
expired sessions and consumed challenges stay as evidence (NFR-AUD-03).

---

## 6. Traceability matrix

| Requirement(s) | Where implemented | Test |
|---|---|---|
| FR-ROS-01/02 | roster already loaded; no API import/edit flow (operational) | absence/404 test |
| FR-ROS-03, FR-AUT-07 | OTP DM to roster Discord ID, sessions, inactive block | auth tests |
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
| NFR-SEC-04, EIR-03 (owner decision D exports) | `backend/export.py` masks every string by default through `mask_pii`; the auth stores, push tokens and `person.roll_number` are never exported | `tests/test_export.py` |
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
| W3b | owner-A2 idempotency keys (0025, header, replay, cleanup; §4.7) | C3a |
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
| O12-3 | Session lifetime, OTP expiry, attempts, rate limits | (a) 7-day sessions (owner), 5 min OTP, 5 attempts, 3 requests per 15 minutes; (b) owner values | (a), configuration-only |
| O12-4 | Roster changes have no API in the MVP; Admin changes are operational | (a) confirm; (b) add an Admin endpoint | (a), FR-ROS-02; adding a route needs a new requirement |
| O12-5 | Approve the FR-AUT-04 matrix as the production baseline | approve / amend cells | approve before production; tests already encode it |
| O12-6 | Mobile app repo/contract ownership, TLS/hosting arrangement | owner supplies | needed before any real client connects |
| O12-7 | Retention rules for sessions/denials/attempts | (a) keep until the retention date, then delete via the DR-07 retention procedure; this supersedes archive-only; (b) other | (a), retention per DR-07 |

---

## 10. SRS ambiguities carried by this phase

1. **Roster edits** — FR-ROS-02 forbids MVP import/edit flows, while the FR-AUT-04 matrix
   gives Admin "Yes" for roster changes. No route is added; O12-4 confirms.
2. **Admin task status** — FR-TSK-06 allows Admin; the matrix cell is "—". Phase 7
   implements the FR-TSK-06 override and the matrix test records it as an accepted
   exception (flagged).
3. **Session/OTP values** — the SRS gives none; O12-3 supplies them.
4. **OTP channel** — resolved by the owner's amendment: FR-ROS-03 and EIR-04 both say the OTP
   is a Discord DM from the ASEC bot to the roster Discord ID; no SMS provider exists. Phase 12
   uses whichever `OtpSender` is configured (development sender or Discord DM sender).
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
  person signs in with their institute roll number, and a one-time password (OTP) is sent as
  a direct message from the ASEC bot to the Discord ID the roster holds for that roll number,
  never to an account or number typed at sign-in. A roster person with no Discord ID cannot
  sign in until the Admin supplies one before the festival (FR-ROS-02).
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

---

## 15. Session E closeout: deviations, assertion changes and carry-forwards

Added at the end of Phase 12 Session E (2026-11-24). The section is numbered 15
as requested by the Session E instruction; the last design section in this plan
is 13.

### 15.1 What Session E changed

- **E0** — export guards (repository, existing manifest, non-empty destination,
  two-flag unmasked acknowledgement and one warning), pause semantics
  (PROCESSING skips only model calls; ingestion, clock advance and bucket
  closure continue; DELIVERY stages and resumes exactly once), the unreadable
  switch fallback, the same-value switch no-op, and the Admin task counts
  (hidden `PENDING`/`DENIED` and cancelled tasks excluded).
- **E1** — the 21-mutation battery, every mutation red, every touched file
  restored byte-for-byte with an equal SHA-256, plus one new static guard for
  `hmac.compare_digest`.
- **E2** — the additive `python -m backend openapi [--check]` developer command,
  the generated `docs/api/openapi.json`, its README and five tests.
- **E3** — the read-only audits, the definition-of-done battery, this section and
  one status line in `docs/PLANNING_PROGRESS.md`.

### 15.2 Session D assertion changes (old → new, one-line reason)

These are the Session D test changes not individually spelled out in the C6–C9
progress lines. No requirement was relaxed; each change follows an owner
decision or a new migration.

| File | Old assertion | New assertion | Reason |
|---|---|---|---|
| `tests/test_api_authorization_matrix.py` | Admin `PUSH_ANNOUNCEMENT` was the `NEEDS-OWNER` branch: `cell.kind == NO`, `cell.needs_owner`, then `response.status == 200` for an announcement acceptance. | `_announcement_refuse` expects `403`, a denial recorded against `PUSH_ANNOUNCEMENT` with `subject_type=PROPOSAL` and the proposal left `PENDING`; the flagged set is `set()`. | Owner decision A resolved the cell strictly: Admin may not accept an ANNOUNCEMENT proposal. |
| `tests/test_api_dashboards.py` | `test_admin_dashboard_has_backlog_attempts_denials_evaluation_and_config_names` asserted backlog, attempts, denials, evaluation and config payloads. | `test_admin_dashboard_is_a_festival_glance_without_internal_ledgers` asserts the festival counts and that those internal ledgers are absent; the `count`/`query` imports were removed. | Owner decision B/D1 slimmed the Admin payload to a festival glance. |
| `tests/test_api_routes.py` | The expected route table had no system-control routes. | The table adds `GET /admin/system`, `POST /admin/system/pause` and `POST /admin/system/resume`. | Owner decision C (D2) added the two pause switches and their routes. |
| `tests/test_config.py` | No dashboard list-limit settings. | New tests pin the default 50, a configured 5 and a rejected 0 for `API_DASHBOARD_LIST_LIMIT`. | Owner decision E (D4) added the list cap. |
| Ten migration test files (`test_db_migrations.py`, `test_migrations_*.py`, `test_push_endpoints.py`) | `list(range(1, 26))` and pre-window lists ending at 25. | `list(range(1, 27))` and pre-window lists ending `..., 25, 26`. | Migration 0026 adds the system-control table. |
| New files | — | `tests/test_export.py`, `tests/test_api_system.py`, `tests/test_system_control.py`, `tests/test_migrations_system_control.py`. | D2/D3 added exports, pause switches and their schema. |

### 15.3 Session E test and behaviour changes

- The old `test_a_paused_processing_switch_stops_the_pipeline_tick` asserted the
  worker returned before the clock and bucketing (`groups == ()`, `assigned == 0`,
  no bucket, no clock row). It was replaced by
  `test_a_processing_pause_skips_model_calls_but_still_closes_buckets` and
  `test_resuming_processing_processes_the_closed_backlog_in_order`, which assert
  that a PROCESSING pause skips only the relevance/proposal calls while
  ingestion, the clock and bucket closure continue, and that resume processes the
  closed backlog losslessly in order. The change follows the owner's pause
  semantics; it does not weaken the no-model-call guarantee.
- The old `test_the_export_cli_accepts_the_unmasked_switch` was replaced by
  `test_the_export_cli_refuses_unmasked_without_the_acknowledgement` and
  `test_the_export_cli_accepts_unmasked_with_one_acknowledgement_warning`, which
  pin the two-flag acknowledgement and exactly one stderr warning.
- Added E0 tests: repository destination, existing manifest, non-empty
  destination, Admin hidden/cancelled counts, per-group backlog detail, delivery
  pause and exactly-once resume, unreadable switch (one redacted warning, no
  raise), same-value no-op, already-resumed no-op.
- Added the static `test_the_otp_hash_comparison_uses_compare_digest`. It parses
  `backend/auth.py` and asserts an `hmac.compare_digest` call. It is a source/AST
  guard, **not** a timing measurement; a unit test cannot honestly measure
  constant-time behaviour.

### 15.4 Mutation battery (E1)

Twenty-one mutations were applied one at a time, each with a targeted test run.
Twenty were red on the first pass. M9 (AH/Head evaluation export) initially
survived because the first mutation also emptied `matrix_operations`, and
`test_every_domain_route_is_bounded_by_its_matrix_cells` skips an operation with
no matrix row. The mutation was corrected to keep `matrix_operations` and widen
`allowed_roles` to `REVIEW_ROLES`; the existing bounding test then failed naming
`AH` and `Head`. Every mutation was restored by writing the saved original bytes
back, and every touched file's SHA-256 equalled its pre-mutation value
(`restore_ok=True` for all 21). No destructive git command was used.

### 15.5 OpenAPI (E2)

`python -m backend openapi` builds the real app with `Config({})`, a fixed clock
and a connection factory that raises if called; it opens no database, makes no
network call, reads no secret and binds no port. The document has 35 paths and
36 operations, equal to `ROUTE_OPERATIONS`. It is rendered with sorted keys,
two-space indent, LF and one trailing newline. `--check` exits 1 on a missing or
drifted document without writing; bad arguments exit 2.

**Carry-forward (cosmetic):** FastAPI's default `HTTPValidationError` and
`ValidationError` 422 models remain in `components.schemas`. The app's actual
422 body is the fixed `ErrorResponse`; the default models describe the
framework's validation detail, which the handlers replace. Removing them would
mean editing the generated schema by hand, so they are documented rather than
silently deleted.

### 15.6 Deviations and carry-forwards

- No commit, staging or push was performed in this session; the owner commits at
  a green checkpoint.
- This Python's `Path.read_text` has no `newline` argument, so the `--check`
  reader and the tests use `open(..., newline="")`.
- The C10 full suite was launched with the doubled `-q` (the `addopts` `-q` plus
  the CLI `-q`), which suppressed the summary line. Its count was verified by
  `pytest --co` (2016 collected) and the 2016 progress dots; exit 0. C11 and C12
  used `-o addopts="" -q` and printed their summaries (2017 and 2022 passed).
- Test counts: baseline 2007; C10 2016; C11 2017; C12 2022.
- The Session D assertion changes are listed in 15.2; no test was weakened,
  skipped or deleted, apart from the documented `importorskip("fastapi")`.
- Phase 12 is **not** verified live: no real Discord connection, real OTP
  delivery or production database was used. The evidence is the offline suite,
  the mutation battery and the audits.
- `hmac.compare_digest` timing is not measured; the guard is static (15.3).
- Export remains a manual operator command; no scheduler, queue or new
  dependency was added.

### 15.7 Final definition-of-done battery (2026-11-24)

- `.venv\Scripts\python.exe -m pytest -o addopts="" -q` → **2022 passed in
  627.94s**.
- `.venv\Scripts\python.exe -m ruff check .` → **All checks passed!**
- `.venv\Scripts\python.exe -m mypy` → **Success: no issues found in 44 source
  files**.
- `.venv\Scripts\python.exe -m backend --version` → **0.0.1**.
- Targeted: `tests/test_auth.py tests/test_migrations_auth.py` → 44 passed;
  `tests/test_api_authorization_matrix.py tests/test_api_routes.py` → 127
  passed; `tests/test_api_visibility.py tests/test_api_dashboards.py
  tests/test_api_errors.py tests/test_api_server_smoke.py` → 51 passed;
  `tests/test_export.py tests/test_system_control.py tests/test_openapi.py` → 31
  passed.
- Fresh temp DB: applied `[1..26]`, re-apply `[]`, `PRAGMA foreign_key_check`
  `[]`, `PRAGMA integrity_check` `ok`; tables `['announcement',
  'auth_session', 'idempotency_key', 'system_control']`.
- `git diff --check` silent; `git status --short` shows only the intended
  modified files and the two intended untracked paths (`docs/api/`,
  `tests/test_openapi.py`); nothing staged.
