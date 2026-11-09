# ASEC Phase 11 Plan — Notification Delivery and Announcement Fan-out

Status: planning only, 2026-10-29. No code, migration or test is changed by this
document. Authority: `docs/SRS.md`, `AGENTS.md`, `docs/DECISIONS.md` §A/§B,
`docs/ROADMAP.md`. Phase 11 consumes notification rows already staged by Phases 6/7/8
and can run beside Phase 10. Tests-first applies to every workstream.

---

## 1. Verified baseline (pre-Phase-11 facts, 2026-10-29; re-verify at execution)

| Check | Planned expectation |
|---|---|
| HEAD | Phase 9 committed; Phase 10 may be in progress |
| `pytest` / `ruff` / `mypy` | green / clean / clean |
| migrations | `0001`–`0022` if Phase 10 landed; otherwise `0021` |
| staged rows | `notification`/`notification_attempt` from `0008`; tying via `notification_events.stage_notification` (Phase 7) |
| audience columns | `announcement.audience_type`, `audience_vertical_id`, `audience_ah_person_id` (0018) |
| visibility | `tasks.visible_tasks` is the only task filter (Phase 7) |

Real facts verified today:

- `0008_notifications.sql`: `notification(event_type, recipient_person_id, dedupe_key
  UNIQUE, exactly one of proposal/task/approval_request/announcement, delivery_state,
  created_at)`; `notification_attempt(notification_id, attempt_number, outcome, error,
  recorded_at)` append-only.
- `backend/review.py` stages `PROPOSAL_READY`, `TASK_ASSIGNED`, `OPEN_TASK_AVAILABLE`,
  `CROSS_VERTICAL_REQUEST`, `HEAD_CROSS_VERTICAL_TASK`; Phase 8 adds
  `CROSS_VERTICAL_DECISION`; Phase 7 adds task lifecycle labels.
- Nothing reads `delivery_state` or writes `notification_attempt` today; no push, OTP or
  FCM code exists; `dependencies = []`.
- `announcement` (0018) has structured audience; no fan-out code exists.
- `person.phone` is NOT NULL after 0016; `person.reports_to` is a person FK; `role` is
  `Admin|Head|AH|Volunteer`.
- `Config` redacts names containing `TOKEN/SECRET/PHONE/OTP/KEY`; no secret file is read.

---

## 2. Scope, non-goals, and boundary

### 2.1 In scope

| # | Slice | Requirements |
|---|---|---|
| S1 | Rendering and in-app list: state-accurate wording, deep links, read state | FR-NOT-03/07, FR-REV-10, NFR-SEC-03 |
| S2 | Delivery worker: retry, backoff, attempts, terminal failure, no rollback | FR-NOT-01/02/04/05, AC-17 |
| S3 | `PushSender` protocol + FCM sender behind it; 0023 endpoints | FR-NOT-07, EIR-04, NFR-SEC-01 |
| S4 | `OtpSender` interface for Phase 12 (no provider decision here) | FR-ROS-03 (interface only) |
| S5 | Announcement fan-out from PB-6 audience, idempotent sweep | FR-NOT-06, FR-REV-10 |
| S6 | Tests, mutations, DoD | all above |

### 2.2 Non-goals

- No HTTP API or auth/session flow (Phase 12); the OTP sender is only an interface.
- No change to when notifications are staged (Phases 6/7/8) and no batching (FR-NOT-02,
  FR-PM-06 is Post-MVP).
- No SMS provider (amendment: OTP is a Discord DM from the ASEC bot to the roster Discord ID;
  O11-1); the plan ships a development sender and a Discord DM sender behind one interface.
- No mobile app code, deep-link handler or UI.
- No second notification channel invented, no digest, no quiet hours.
- No edit of `DECISIONS.md` §A/§B or `docs/SRS.md`.

### 2.3 Boundary rule

Phase 6/7/8 own *what happened* and stage a row in the same transaction as the state
change. Phase 11 owns *how it reaches a person*. A delivery failure must never undo the
row's cause (FR-NOT-04); delivery is always after commit and outside the domain
transaction.

---

## 3. Reuse of real names

| Existing name | Where | Phase 11 use |
|---|---|---|
| `notification`, `notification_attempt` | `0008` | source of truth for delivery state and attempts |
| `delivery_state` | `0008` (free text) | values `PENDING`, `SENT`, `FAILED`, `EXHAUSTED` (documented encoding) |
| `stage_notification`, key builders | `backend/notification_events.py` | fan-out staging only; never direct INSERT |
| event constants | `review.py`, `tasks.py`, `approvals.py` | renderer keys; no new event vocabulary unless FR-NOT-01 names it |
| `announcement.audience_type/vertical/ah` | 0018 | fan-out audience |
| `person.reports_to`, `person.role`, `person.active` | 0016 | AH volunteers and vertical members |
| `visible_tasks` | `backend/tasks.py` | deep links point at task ids; no separate filter |
| `Config.require/get` | `backend/config.py` | FCM credentials, retry knobs |
| `transition_bucket_processing_state` | `bucket_lifecycle.py` | not used; no bucket coupling |

New module `backend/notification_delivery.py` with `render_notification`,
`list_notifications`, `mark_read`, `deliver_pending`, `fan_out_announcement`,
`sweep_unfanned_announcements`, `run_delivery_tick`, and protocols `PushSender`,
`OtpSender`. It imports `authorization`, `notification_events`, `db.connection`; it does
not import `review.py`, `tasks.py` or `approvals.py` (rendering reads rows by SQL).

---

## 4. Design

### 4.1 Rendering and in-app list (FR-NOT-03/07)

`notification` stores the event and its subject, not a frozen sentence. The body is
composed at read/send time from the current state, so wording cannot drift from reality:

```python
@dataclass(frozen=True)
class NotificationView:
    notification_id: int
    event_type: str
    title: str
    body: str
    deep_link: str | None
    created_at: str
    read_at: str | None

def render_notification(connection, notification_id) -> NotificationView
def list_notifications(connection, *, person_id, unread_only=False) -> tuple[NotificationView, ...]
def mark_read(connection, notification_id, *, person_id) -> bool
```

- `list_notifications` is self-only: a caller can list and mark only their own rows;
  Admin gets no cross-person list here (NFR-SEC-03; admin views are Phase 12 and show
  aggregates, not inboxes).
- Exact state-dependent wording is pinned by tests: `TASK_ASSIGNED` renders "assigned to
  you, awaiting your response", never "accepted"; an `ASSIGNEE_ACCEPTED` renders "X
  accepted", not "assigned". `PROPOSAL_READY` renders the proposal summary and a
  `deep_link` to the review screen (`/review/{proposal_id}`), satisfying FR-NOT-07's
  "open the review screen directly".
- `CROSS_VERTICAL_DECISION` includes the decision message when present; `TASK_CANCELLED`
  says cancelled; `OPEN_TASK_AVAILABLE` says a place is open, not that it is assigned.
- A missing subject row (should be impossible by FK) raises rather than rendering a
  half-truth.
- `mark_read` first verifies the notification's `recipient_person_id` equals the caller's
  `person_id`; a mismatch is an authorization denial (recorded through the Phase 7
  recorder), not a silent no-op, because `notification_read` has no person column and the
  ownership check must live in the service. It then inserts into `notification_read`
  (0023); a second call for the caller's own row is a no-op returning `False`. Read state
  never changes the delivery state.

### 4.2 Delivery worker (FR-NOT-04/05, AC-17)

```python
class PushSender(Protocol):
    def send(self, *, token: str, title: str, body: str,
             deep_link: str | None, collapse_key: str) -> str | None: ...

class OtpSender(Protocol):
    def send(self, *, discord_id: str, code: str) -> None: ...

@dataclass(frozen=True)
class DeliveryBatch:
    attempted: int
    sent: int
    retrying: int
    exhausted: int

def deliver_pending(connection, *, push: PushSender | None, limit: int = 50,
                    now: str | None = None) -> DeliveryBatch
```

- Claim: select `notification` rows in `PENDING` (and `FAILED` whose backoff has elapsed)
  with `delivery_state != 'SENT'`, ordered by `created_at, id`, limited by `limit`.
  Selection and the attempt-number computation happen in one `BEGIN IMMEDIATE`
  transaction, so two workers cannot take the same row (single worker is the deployment,
  but the guarantee is cheap).
- Send happens **outside** any transaction: render (read), close the read, call the
  provider, then open a short transaction to insert the `notification_attempt` and update
  `delivery_state`. A slow or failing provider never holds the database writer.
- Success → `SENT`, attempt `outcome='SENT'`. Failure → attempt `outcome='FAILED'` with a
  sanitized error (status code and provider code only, never a token, phone or body);
  state `FAILED` until `attempt_number` reaches `NOTIFICATION_MAX_ATTEMPTS`
  (configuration), then `EXHAUSTED`. Backoff is `min(base * 2**(n-1), cap)` from the last
  attempt time; values are configuration, not SRS numbers (O11-3).
- Retry never creates a second `notification` row: `dedupe_key` is unique and the worker
  only updates state/attempts. This is FR-NOT-05's "no duplicate notification for the
  same event and recipient"; at the provider level a crash after send but before the
  state update can resend the same body, so the provider `collapse_key` is the
  notification id and the attempt row makes the duplicate visible. The plan states this
  honestly rather than claiming exactly-once push.
- No provider call is made for a row whose recipient is inactive (FR-AUT-07) or has no
  active endpoint; it is marked `EXHAUSTED` with `outcome='NO_ENDPOINT'` — visible, not
  silently dropped. OTP rows target the roster Discord ID (FR-ROS-03); OTP delivery is
  Phase 12's caller.
- A source change is never rolled back (FR-NOT-04): delivery runs after the domain
  transaction committed; a failure only writes attempts.

### 4.3 FCM sender (EIR-04, FR-NOT-07)

- **Mode: FCM HTTP v1** (`https://fcm.googleapis.com/v1/projects/{id}/messages:send`,
  OAuth2 service-account JWT). `docs/OWNER_ANSWERS.md` O11-2 chose (b); the legacy HTTP
  key API is not a safe base (the owner believes Google shut it down in 2024 — confirm in
  the Firebase docs before building). There is no legacy-key option in this plan.
- `FcmPushSender` implements `PushSender` and lives in one module behind that protocol, so
  the delivery worker never imports the provider. The worker and every core test use a
  fake `PushSender`; no test requires the extra.
- **Dependency policy (`docs/OWNER_ANSWERS.md`, applies to every phase).** The core backend
  stays dependency-free. The HTTP v1 signing package is allowed only as an optional extra
  `push` in `pyproject.toml` (`google-auth`/`cryptography`-class, exact pin at least two
  weeks old), confined to the `FcmPushSender` module, **with all core tests passing without
  it installed**. The import is lazy inside the sender, so importing
  `backend.notification_delivery` and running plain `pytest` works on a clean install; a
  missing extra raises `PushProviderNotConfigured` at send time, never an import error at
  module load. Installing the extra is an owner/deployment step, not a core dependency.
- Payload: `message.token`, `message.notification.title/body`, `message.data.deep_link`,
  `message.android.collapse_key`. Response `401/403` is permanent (no retry, mark
  `EXHAUSTED`), `429/5xx` retry, malformed response fails the attempt. The service-account
  JSON is read with `Config.require`, never logged or stored in the database.
- Endpoint registration (0023) is written by the future API (Phase 12); Phase 11 provides
  `register_endpoint`/`deactivate_endpoint` helpers for tests and the API to call. Token
  uniqueness is enforced; a rotated token deactivates the old row.

### 4.4 OTP interface (FR-ROS-03 boundary)

`OtpSender.send(discord_id, code)` is the only dependency Phase 12 needs. The recipient is
the roster Discord ID, never an account or number typed at sign-in (FR-ROS-03). Phase 11 ships:

- the protocol and a `RecordingOtpSender` for tests (never used in production),
- a development sender for local runs that records the code instead of sending it,
- a `DiscordDmOtpSender` that opens a DM channel with the roster Discord ID and sends one
  message through the bot (reuses the Phase 9 Discord client; no new migration),
- an explicit `OtpSenderNotConfigured` error for production paths until a Discord DM sender
  is configured.

The amended FR-ROS-03 says the OTP is a Discord DM from the ASEC bot to the roster Discord ID;
EIR-04 names FCM push and the Discord OTP DM. O11-1 answered (d): Discord DM, no SMS provider.
No code invents an SMS vendor, an email fallback or an admin-code fallback. A failed DM is
recorded and must not be distinguishable from success in the sign-in response.

### 4.5 Announcement fan-out (FR-NOT-06, FR-REV-10)

```python
def fan_out_announcement(connection, announcement_id) -> int
def sweep_unfanned_announcements(connection) -> tuple[int, ...]
```

- Audience from 0018:
  - `VERTICAL`: every active person in `person_vertical` for `audience_vertical_id`
    (the Head's vertical). The SRS says "a Head's vertical"; all active members is the
    literal reading.
  - `AH_VOLUNTEERS`: every active `Volunteer` whose `reports_to = audience_ah_person_id`
    (the sender AH's volunteers). If the AH has no reports, the fan-out stages zero rows
    and reports it; it does not fall back to the whole vertical.
- One notification per recipient, `event_type='ANNOUNCEMENT'`, points at
  `announcement_id`, dedupe `ANNOUNCEMENT:<announcement_id>:<person>`. Idempotent.
- The body contains the announcement's accepted description, never source chat or bucket
  context (NFR-SEC-03).
- `sweep_unfanned_announcements` finds accepted announcements with no `ANNOUNCEMENT:`
  row and stages them; the delivery tick calls it before `deliver_pending`. This avoids
  editing the Phase 6 accept transaction and makes a crash between accept and fan-out
  recoverable.
- FR-NOT-06's "push announcements" is this fan-out plus the push channel; no new
  broadcast operation or audience vocabulary is added.

### 4.6 Delivery tick

```python
def run_delivery_tick(connection, *, push, otp_sender=None, limit=50) -> DeliveryBatch
```

Order: sweep announcements → deliver pending (push for notification rows; OTP rows only
when an `OtpSender` is supplied and the row is an OTP challenge). The worker from Phase 9
calls this after the pipeline tick, or a separate process calls it; either way it holds no
in-memory queue and is restart-safe.

### 4.7 Security

- Push tokens and OTP codes are secrets: never in `repr`, logs, attempt errors, app
  notification bodies (the code goes only to the OtpSender, i.e. the Discord DM, not the
  in-app list), or test fixtures beyond obviously fake values.
- `notification_attempt.error` is sanitized at the boundary: provider status/code only.
- In-app list is self-only; deep links are opaque ids, not secrets.
- Phone numbers are masked in logs and never rendered into a notification body sent to a
  different person.
- The FCM/OTP credentials are configuration only (NFR-SEC-01).

---

## 5. Exact migration SQL (planned, full file)

### `0023_push_endpoints.sql` (FR-NOT-07, EIR-04)

```sql
-- One row per active device token. Tokens are secrets: never logged, never in
-- an error string. Legacy/duplicate tokens are deactivated, not deleted, so a
-- delivery attempt can still explain itself (FR-AUD/NFR-AUD-03).
CREATE TABLE push_endpoint (
    id           INTEGER PRIMARY KEY,
    person_id    INTEGER NOT NULL,
    provider     TEXT NOT NULL,
    token        TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT,
    CHECK (provider IN ('FCM')),
    CHECK (length(trim(token)) > 0),
    CHECK (active IN (0, 1)),
    CHECK (length(trim(created_at)) > 0),
    CHECK (last_seen_at IS NULL OR length(trim(last_seen_at)) > 0),
    UNIQUE (token),
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT
);

CREATE INDEX ix_push_endpoint_active
    ON push_endpoint (person_id)
    WHERE active = 1;

-- In-app read marker. Absence means unread; the row is never updated, so a
-- read receipt is immutable evidence (NFR-AUD-03).
CREATE TABLE notification_read (
    notification_id INTEGER PRIMARY KEY,
    read_at         TEXT NOT NULL,
    CHECK (length(trim(read_at)) > 0),
    FOREIGN KEY (notification_id)
        REFERENCES notification (id) ON DELETE RESTRICT
);
```

The roadmap's 0023 line is extended with `notification_read`; it stays the same version
and file, so no renumbering. No `notification` column is altered and no delivery state is
schema-constrained (0008 deliberately kept it free text).

---

## 6. Traceability matrix

| Requirement(s) | Where implemented | Test |
|---|---|---|
| FR-NOT-01 | every staged event type has a renderer; delivery covers all rows | render/delivery matrix test |
| FR-NOT-02 | one row per event/recipient; no batching | dedupe test |
| FR-NOT-03 | state-accurate wording per event | wording tests |
| FR-NOT-04 | delivery after commit; failure only writes attempts | rollback test |
| FR-NOT-05, AC-17 | retry on the same row; attempts append-only | retry/duplicate test |
| FR-NOT-06 | fan-out to vertical / AH volunteers | fan-out tests |
| FR-NOT-07, EIR-04 | push + in-app list + review deep link | FCM/list tests |
| FR-REV-10 (delivery half; acceptance record in 6) | announcement audience fan-out | fan-out test |
| FR-ROS-03 (interface) | `OtpSender` (development sender + Discord DM sender), no provider invented | OTP interface test |
| NFR-SEC-01/03 | credentials config-only; self-only list | leak/scope tests |
| NFR-AUD-03 (reused from 7) | attempts/read rows immutable | audit test |

Already complete elsewhere: staging (6/7/8), audience columns (7), API/session (12).

---

## 7. Tests-first and mutations

| File | Covers |
|---|---|
| `tests/test_notification_delivery.py` (new) | success, 5xx retry, 4xx permanent, max attempts, backoff, no duplicate row, no rollback, error sanitization |
| `tests/test_notification_rendering.py` (new) | all event types, assigned vs accepted wording, decision message, deep links, missing subject raises |
| `tests/test_notification_list.py` (new) | self-only, unread filter, mark-read idempotent, no Admin cross-inbox |
| `tests/test_announcement_fanout.py` (new) | VERTICAL, AH_VOLUNTEERS, inactive excluded, no reports → zero, idempotent sweep, no source chat |
| `tests/test_push_endpoints.py` (new) | 0023 constraints, token unique, active index, deactivation, secret redaction |
| `tests/test_fcm_sender.py` (new) | HTTP v1 payload, bearer token from a fake signer, 401 permanent, 429/5xx retry, collapse key, module import works without the `push` extra |
| `tests/test_otp_sender.py` (new) | protocol, fake sender, code never logged, unconfigured error |

Mutation list:

1. Insert a second notification row on retry → dedupe test red.
2. Let a delivery failure roll back the source change → rollback test red.
3. Include inactive recipients in fan-out → fan-out test red.
4. Read the dropped `audience_scope` instead of 0018 columns → fan-out test red.
5. Log the FCM token/OTP code → leak test red.
6. Return another person's notifications → scope test red.
7. Render "accepted" for an awaiting assignee → wording test red.
8. Remove the max-attempt cap → exhaustion test red.
9. Put the OTP code in the in-app body → leak test red.
10. Skip the announcement sweep → recovery test red.

---

## 8. Workstreams

| WS | Content | Stop |
|---|---|---|
| W1 | 0023 + endpoint helpers + rendering/list/read | C1 |
| W2 | delivery worker + retry/backoff/attempts | C2 |
| W3 | announcement fan-out + sweep | C3 |
| W4 | `PushSender` + FCM sender (after O11-2) | C4 |
| W5 | `OtpSender` interface + unconfigured path | C4 |
| W6 | delivery tick integration + mutations/audits/DoD | final |

W4 is gated on the `push` extra being installed and FCM credentials; W1–W3 and W5 run without it, and the full core suite must stay green with the extra absent.

---

## 9. OWNER decisions for this phase

| ID | Question | Options | Recommendation |
|---|---|---|---|
| O11-1 | OTP delivery channel; resolve EIR-04 (FCM) vs FR-ROS-03 | **ANSWERED (`docs/OWNER_ANSWERS.md`)**: (d) OTP by Discord DM from the ASEC bot to the roster Discord ID; no SMS provider; build the `OtpSender` interface with a development sender and a Discord DM sender | adopted; no provider account or registration is needed; a roster person with no Discord ID cannot sign in (FR-ROS-02) |
| O11-2 | FCM mode | **ANSWERED (`docs/OWNER_ANSWERS.md`)**: (b) HTTP v1 through an optional `push` extra behind `PushSender`; the legacy HTTP key is not used | decided; signing package confined to one module, core tests run without it |
| O11-3 | Retry policy: max attempts, backoff base/cap, batch limit | (a) 5 attempts, 5 s base, 5 min cap, 50/batch; (b) owner values | (a), configuration-only |
| O11-4 | Confirm `VERTICAL` announcement audience = all active members of the vertical | (a) all active members; (b) volunteers only | (a), literal FR-REV-10; confirm |
| O11-5 | Deep-link scheme for the mobile app (out of repo) | `asec://review/{id}` etc. | owner supplies; Phase 11 uses an opaque path string |

---

## 10. SRS ambiguities carried by this phase

1. **OTP delivery channel** — resolved by the owner's amendment: FR-ROS-03 and EIR-04 both say
   the OTP is a Discord DM from the ASEC bot to the roster Discord ID, with FCM only for push.
   The `OtpSender` interface ships a development sender and a Discord DM sender; no SMS vendor,
   email fallback or admin-code fallback is invented.
2. **FR-NOT-06 "push announcements"** — the SRS does not define a separate broadcast
   operation; this plan treats it as the existing accept + fan-out path.
3. **"Assignee accepted or denied → creator"** — a claim is an acceptance; Phases 7/8
   stage it. Wording is pinned here.
4. **Announcement audience membership** — "a Head's vertical" is read as all active
   members; O11-4 confirms.
5. **At-least-once push** — FR-NOT-05 forbids duplicate *notifications*, not duplicate
   provider deliveries after a crash; the plan states the limit.
6. **Read state** — the SRS asks for an in-app list but not read/unread semantics; 0023
   adds an immutable read marker as the smallest support.

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
.venv\Scripts\python.exe -m pytest tests/test_notification_delivery.py tests/test_notification_rendering.py tests/test_notification_list.py
.venv\Scripts\python.exe -m pytest tests/test_announcement_fanout.py tests/test_push_endpoints.py tests/test_fcm_sender.py tests/test_otp_sender.py
.venv\Scripts\python.exe -m pytest tests/test_review_service.py tests/test_task_service.py tests/test_approval_service.py
```

Migration check: 0023 applies/re-applies, `PRAGMA foreign_key_check` empty, duplicate
token rejected, inactive endpoint not selected.

Read-only audits:

- `Select-String -Path backend/notification_delivery.py -Pattern 'token|phone|code'` →
  only provider payload construction, never a log/repr/error.
- `Select-String -Path backend -Pattern 'INSERT INTO notification'` → only
  `notification_events.py`.
- `Select-String -Path backend -Pattern 'audience_scope'` → no code path (0018 columns
  only).
- `git status --short` → intended files only; nothing staged; no credentials.

Pass: full suite green, ruff/mypy clean, ten mutations red, audits clean, no network in
the suite, no secret in any attempt/row/report.

---

## 12. Risks and checkpoints

| Risk | Mitigation |
|---|---|
| Duplicate notification rows | unique dedupe key; worker updates only |
| Delivery failure blocks a task change | delivery outside the domain transaction |
| Token/phone/code leak | boundary sanitization + audits + tests |
| FCM HTTP v1 | optional `push` extra behind `PushSender`; core tests run without it (O11-2) |
| Announcement audience drift | 0018 columns only; no scope fallback |
| Crash between send and state update | collapse key + attempt row; documented at-least-once |
| Unlimited retries | max attempts + `EXHAUSTED` visibility |

| Checkpoint | Green state |
|---|---|
| C1 | 0023 + render/list/read; suite green |
| C2 | delivery retry/attempts; AC-17 test green |
| C3 | fan-out + sweep; suite green |
| C4 | FCM/OTP behind interfaces; fake transport green |
| Final | mutations/audits/DoD observed; §15 record written |

---

## 13. Appendix — verbatim requirements quoted

- **FR-NOT-01** The system shall send these notifications:

| Event | Recipient |
|---|---|
| Proposal ready | The reviewer (the sender) only |
| Task assigned (EXPLICIT) | Each named assignee |
| Open task available | Eligible volunteers of the responsible vertical |
| Assignee accepted or denied | The creator |
| Volunteer marked their work completed | The creator |
| Task edited, reassigned or cancelled | The affected assignees |
| Cross-vertical request | All AHs and the Head of the responsible vertical |
| Cross-vertical decision | The requester |
| Head's cross-vertical task created | The Head of the responsible vertical |
| Announcement | Its audience |

- **FR-NOT-02** In the MVP there is one notification per proposal, with no batching.
- **FR-NOT-03** Notification wording shall match the real state. "Assigned to X, awaiting
  acceptance" is different from "X accepted".
- **FR-NOT-04** A notification failure shall not roll back or block the change that caused
  it. The failure shall be recorded and retried.
- **FR-NOT-05** A retry shall not send a duplicate notification for the same event and
  recipient.
- **FR-NOT-06** Heads shall be able to push announcements and notifications to their
  vertical. AHs shall be able to push information to the volunteers under them.
- **FR-NOT-07** Notifications shall reach users as push notifications on their phone and
  appear in an in-app list. A proposal-ready notification shall open the review screen
  directly.
- **FR-REV-10** An accepted ANNOUNCEMENT shall be shown to its audience (a Head's
  vertical, or the sender AH's volunteers) and shall not create a task.
- **FR-ROS-03** Every sign-in to the app shall resolve to exactly one roster person. The
  person signs in with their institute roll number, and a one-time password (OTP) is sent as
  a direct message from the ASEC bot to the Discord ID the roster holds for that roll number,
  never to an account or number typed at sign-in. A roster person with no Discord ID cannot
  sign in until the Admin supplies one before the festival (FR-ROS-02).
- **EIR-04** Notification delivery. Push notifications to phones through Firebase Cloud
  Messaging. Sign-in OTPs are delivered as direct messages from the ASEC bot through the
  Discord interface (EIR-01, FR-ROS-03).
- **AC-17** Notification fails → Task change committed; failure recorded; retried without
  duplicates.
- **NFR-SEC-01** All access requires authentication. Discord, model and push credentials
  are held in secure configuration and never exposed to users or logged in errors.
- **NFR-SEC-03 (excerpt)** Phone numbers and message content are visible to a person only
  for people below them in the roster hierarchy.

---

## 15. Implementation record

Executed 2026-11-09 as Phase 11 Session B (W5, W4, W6). Nothing was staged, committed or
pushed. W1-W3 were already present in the working tree from Session A (C1-C3); this record
covers the whole Phase 11 tree at the final checkpoint.

### 15.1 Files actually changed

| File | Status | Content |
|---|---|---|
| `backend/notification_delivery.py` | new (W1-W6) | Rendering, self-only list, read state, endpoint helpers, `deliver_pending`, announcement fan-out/sweep, `PushSender`, `OtpSender`, `RecordingOtpSender`, `DevelopmentOtpSender`, `DiscordDmOtpSender`, `OtpSenderNotConfigured`, `require_otp_sender`, `run_delivery_tick`. |
| `backend/fcm_sender.py` | new (W4) | `FcmPushSender`, `TokenProvider`, lazy `GoogleServiceAccountTokenProvider`, `FcmTransport`/`UrllibFcmTransport`, HTTP v1 payload and response mapping, `PushProviderNotConfigured`. |
| `backend/discord_adapter.py` | modified (W5, additive) | `PostTransport` protocol and `UrllibTransport.post`; the polling `Transport` protocol is unchanged. |
| `backend/worker.py` | modified (W6, additive) | Optional `delivery_tick` callable on `run_live_tick`, called after `run_pipeline_tick`; delivery exceptions are logged and never escape the pipeline tick. |
| `backend/config.py` | modified (Session A, W2) | `NotificationSettings` / `load_notification_settings` with O11-3 values; no new key was added in Session B. |
| `backend/notification_events.py` | modified (Session A, W3) | `EVENT_ANNOUNCEMENT` and `announcement_key`. |
| `backend/db/migrations/0023_push_endpoints.sql` | new (Session A, W1) | `push_endpoint` and `notification_read` exactly as plan section 5. |
| `tests/test_otp_sender.py` | new (11 tests) | OTP protocol, DM channel/message, failed-DM shape, sanitisation, production refusal, unconfigured path. |
| `tests/test_fcm_sender.py` | new (18 tests) | HTTP v1 payload/auth, 401/403 permanent, 429/5xx retry, malformed/timeout, lazy extra, no provider import. |
| `tests/test_delivery_tick.py` | new (9 tests) | Sweep-before-deliver, configured batch/retry settings, explicit limit, SENDING recovery, zero-recipient sweep, assignee limitation, worker isolation. |
| `tests/test_notification_rendering.py` | modified (+1 test) | `test_otp_code_has_no_in_app_renderer`. |
| `docs/KNOWN_GAPS.md` | modified | Single-process rule, assignee naming, zero-recipient sweep, no OTP row, no live FCM/Discord verification, push extra not declared. |
| `docs/PHASE11_PROGRESS.md` | modified | C4 and final checkpoint lines with real command output. |
| `docs/PHASE11_PLAN.md` | modified | This section 15. |
| `docs/PLANNING_PROGRESS.md` | modified | One Phase 11 status line. |
| `pyproject.toml` | unchanged | The owner pin was blank, so no optional `push` extra was declared. |

### 15.2 Real test totals and RED evidence

| Check | Real output |
|---|---|
| Full suite, `.venv\Scripts\python.exe -m pytest` | **1623 passed in 313.03s (0:05:13)**, exit 0 |
| `tests/test_notification_delivery.py tests/test_notification_rendering.py tests/test_notification_list.py` | **55 passed in 5.34s** |
| `tests/test_announcement_fanout.py tests/test_push_endpoints.py tests/test_fcm_sender.py tests/test_otp_sender.py` | **52 passed in 2.96s** |
| `tests/test_review_service.py tests/test_task_service.py tests/test_approval_service.py` | **192 passed in 20.01s** |
| `tests/test_otp_sender.py` (W5 targeted) | **11 passed in 0.08s** |
| `tests/test_fcm_sender.py` (W4 targeted) | **18 passed in 1.23s** |
| `tests/test_delivery_tick.py` (W6 targeted) | **9 passed in 1.78s** |
| `.venv\Scripts\python.exe -m ruff check .` | **All checks passed!** |
| `.venv\Scripts\python.exe -m mypy` | **Success: no issues found in 31 source files** |
| `.venv\Scripts\python.exe -m backend --version` | **0.0.1** |

RED evidence was observed before each implementation:

- W5: `ImportError: cannot import name 'DevelopmentOtpSender' from 'backend.notification_delivery'`.
- W4: `ModuleNotFoundError: No module named 'backend.fcm_sender'`.
- W6: `ImportError: cannot import name 'run_delivery_tick' from 'backend.notification_delivery'`.

### 15.3 Mutation battery (15/15 killed, all restores hash-matched)

Pre-mutation SHA-256 values: `backend/notification_delivery.py`
`539B0B632B266985D99B2C61D69305FD73D3DAF6ABAD349B657DA7979D32E20D`;
`backend/fcm_sender.py`
`7D14FBBACB74281B3A70D690ECEFC180B4AC8023C82D145CA3E1300EB11359D1`;
`backend/worker.py`
`5C37986C4802A704A97C41EEC61CC51226BEDD8894B5AD9CCD860FD7851DF913`.
Every restore printed `HASH_MATCH True` and the same SHA-256.

| # | Mutation | Target test | Observed red |
|---|---|---|---|
| 1 | Insert a second notification row on retry | `test_transient_failure_retries_the_same_row_after_backoff` | `assert (1, 1, 0, 0) == (0, 0, 0, 0)` (duplicate row claimed) |
| 2 | Let a delivery failure roll back the source change | `test_failed_delivery_does_not_roll_back_the_source_change` | `assert 'ROLLED_BACK' == 'clip the wires'` |
| 3 | Include inactive recipients in fan-out | `test_inactive_members_are_excluded` | `assert 5 not in {1, 2, 3, 4, 5}` |
| 4 | Read the dropped `audience_scope` | `test_vertical_audience_is_every_active_member` | `IndexError: No item with that key` |
| 5 | Log the OTP code | `test_otp_code_and_bot_token_never_appear_in_repr_or_logs` | `'FAKE-OTP-CODE-123456'` found in `caplog.text` |
| 6 | Return another person's notifications | `test_list_is_self_only` | `assert [2] == [1]` |
| 7 | Render "accepted" for an awaiting assignee | `test_awaiting_assignee_is_never_rendered_as_accepted` | `'awaiting your response' in 'You accepted this task: ...'` failed |
| 8 | Remove the max-attempt cap | `test_max_attempts_exhausts_the_row` | `assert 0 == 1` on `second.exhausted` |
| 9 | Add an OTP in-app renderer that exposes the code | `test_otp_code_has_no_in_app_renderer` | `IndexError: No item with that key` at the added OTP branch |
| 10 | Skip the announcement sweep | `test_tick_sweeps_then_delivers_using_the_configured_batch_limit` | `assert (0, 0, 0, 0) == (2, 2, 0, 0)` |
| 11 | Treat FCM 401 as retryable | `test_401_is_permanent_and_never_copies_the_provider_message` | `assert False is True` on `error.permanent` |
| 12 | Drop `message.android.collapse_key` | `test_payload_and_bearer_token_match_http_v1_contract` | payload comparison showed `android` missing |
| 13 | Make a failed OTP DM return a different result | `test_failed_dm_returns_the_same_shape_and_a_sanitised_outcome` | `assert False is None` |
| 14 | Import the FCM provider at module load | `test_notification_delivery_does_not_import_the_provider_module` | `ImportError: ... circular import` |
| 15 | Let the delivery tick raise into the worker | `test_worker_delivery_hook_failure_does_not_fail_the_pipeline_tick` | `RuntimeError: push failed` escaped |

No mutation or scratch file remained; `git status --short` showed only the intended Phase 11
tree and nothing staged.

### 15.4 Final battery (exact commands, real observed output)

| Check | Result |
|---|---|
| `.venv\Scripts\python.exe -m pytest` | `1623 passed in 313.03s (0:05:13)`, exit 0 |
| `.venv\Scripts\python.exe -m ruff check .` | `All checks passed!`, exit 0 |
| `.venv\Scripts\python.exe -m mypy` | `Success: no issues found in 31 source files`, exit 0 |
| `.venv\Scripts\python.exe -m backend --version` | `0.0.1`, exit 0 |
| Fresh-database probe (disposable DB outside the repo) | `first 23 1 23`; `second []`; `foreign_key_check []`; `integrity_check ['ok']`; tables `['notification_read', 'push_endpoint']` |
| `git diff --check` | clean (no output) |
| `git status --short` | intended Phase 11 files only; nothing staged |

### 15.5 Read-only audit (file:line evidence)

| Audit question | Evidence |
|---|---|
| Is the OTP code kept out of logs, errors, reprs and in-app bodies? | Code appears only in the DM request body (`notification_delivery.py:267`); `RecordingOtpSender`/`DevelopmentOtpSender`/`DiscordDmOtpSender` reprs omit it (`:161`, `:180`, `:224`); the failure log carries only the sanitised outcome (`:235`); the blank-input `ValueError` carries no values (`:230`); `test_otp_code_has_no_in_app_renderer` proves there is no OTP in-app renderer. |
| Are tokens and service-account content kept out of logs, errors and reprs? | FCM payload construction is the only place the device token is used (`fcm_sender.py:257-268`); `FcmPushSender.__repr__` exposes only the project id (`:229`); all `PushDeliveryError` values carry status/provider code only (`:249`, `:253`, `:280`, `:287`, `:295`); the service account is read through `Config.require` (`:212`) and never logged or stored. |
| Are attempt errors sanitised? | `_sanitized_push_error` returns only `HTTP <status>` and the provider code (`notification_delivery.py:840-849`); attempt inserts use that value (`:723`). |
| Does `backend.notification_delivery` import a provider module? | No `fcm_sender` import; `test_notification_delivery_does_not_import_the_provider_module` runs a fresh interpreter and asserts `backend.fcm_sender` is not in `sys.modules`. |
| Is every `INSERT INTO notification` in `notification_events.py`? | `notification_events.py:67` is the only match; `notification_delivery.py:621` is `notification_read` and `:723` is `notification_attempt`. |
| Is the dropped `audience_scope` absent? | No `audience_scope` match in `backend` Python; fan-out reads `audience_type`/`audience_vertical_id`/`audience_ah_person_id` (`notification_delivery.py:1034-1055`). |
| Is the FCM URL confined to the sender? | `https://fcm.googleapis.com` appears only in `backend/fcm_sender.py:31`. |
| Are credentials in the repository? | No credential, token, phone number, OTP code or service-account content was added; tests use obviously fake values; `pyproject.toml` is unchanged. |

### 15.6 Decisions, deviations and gaps

- **O11 answers used.** O11-1 (d): OTP is a Discord DM from the ASEC bot to the roster
  Discord id; no SMS, email or admin-code fallback. O11-2 (b): FCM HTTP v1 through an
  optional `push` extra behind `PushSender`. O11-3 (a): 5 attempts, 5 s base, 300 s cap,
  50 per batch, all configuration. O11-5: deep links remain opaque path strings such as
  `/review/{proposal_id}`.
- **Android only.** The payload is Android-shaped (`message.android.collapse_key`); no iOS,
  APNs or platform field is emitted.
- **Push pin not supplied.** `PUSH_EXTRA_PIN` was blank, so `pyproject.toml` was not edited.
  The signing step is behind `TokenProvider`; the real signer lazily imports
  `google.auth`/`google.oauth2` inside `GoogleServiceAccountTokenProvider.token` and raises
  `PushProviderNotConfigured` at send time when the extra is absent. Declaring and pinning
  the `push` extra is an owner step.
- **At-least-once caveat.** A crash after the provider call but before the state update is
  recovered as a `WORKER_CRASH_AFTER_SEND` failed attempt and the row is sent again. The
  provider `collapse_key` is the notification id and the duplicate is visible in the attempt
  ledger; Phase 11 does not claim exactly-once push.
- **Single-delivery-process rule (hard deployment rule).** `deliver_pending` treats any
  `SENDING` row as a lost claim and resends it, so two concurrent delivery processes could
  double-send. The delivery tick must run in exactly one process (the worker). The
  `test_sending_recovery_documents_the_single_delivery_process_rule` test and
  `docs/KNOWN_GAPS.md` record this.
- **Assignee naming limitation.** `ASSIGNEE_ACCEPTED`/`ASSIGNEE_DENIED` name the assignee
  with the most recent `agreed_at` for that agreement. The notification row stores the task
  and recipient, not the acting assignee, so two assignees accepting the same task cannot be
  attributed per row. `test_two_acceptances_cannot_be_distinguished_by_the_notification_row`
  pins the limitation; wording was not changed to hide it.
- **Zero-recipient announcements.** An AH with no reports stages zero rows and never falls
  back to the vertical. Each sweep re-examines it with one bounded query and no row churn;
  `test_zero_recipient_announcement_sweep_is_idempotent_and_cheap` records it.
- **No OTP notification row.** The notification schema has no OTP event or OTP row.
  `run_delivery_tick` accepts `otp_sender` for the Phase 12 boundary but does not invent a
  row; Phase 12 calls `OtpSender.send` directly after resolving the roster Discord id.
- **Worker hook.** `run_live_tick(..., delivery_tick=None)` is the minimal additive hook. It
  runs the delivery callable after `run_pipeline_tick`; a delivery failure is logged and
  never fails the pipeline tick. `backend/__main__.py` is deliberately not wired because no
  push sender can be configured while the `push` extra is undeclared; the deployment or
  Phase 12 wiring supplies the callable. `run_delivery_tick` is exposed for that caller.
- **Not live-verified.** FCM HTTP v1 and Discord DM sending have not been exercised against
  the real services. Every test uses a fake transport and a fake `TokenProvider`; the suite
  makes no network call. Phase 11 is therefore not claimed as verified live.
- **Not built, awaiting owner decision.** A second notification channel (for example a
  Discord DM notice when push fails) and an Admin-issued sign-in code were not built; both
  would invent scope the SRS does not grant in this phase.

Requirement coverage implemented and tested: **FR-NOT-01..07**, **FR-ROS-03** (interface
and Discord DM sender), **EIR-04**, **FR-AUT-07** (inactive recipients never receive a
provider call), **NFR-SEC-01**, **NFR-SEC-03**, **NFR-AUD-03** (append-only attempts and read
markers), **NFR-REL-05** (delivery failure does not fail the pipeline tick), **AC-17** and
**AC-22** (failure recorded, retried without a duplicate notification row).
