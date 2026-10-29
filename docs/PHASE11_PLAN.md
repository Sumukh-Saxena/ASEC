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
- No SMS provider selection unless the owner decides (O11-1); the plan ships a fake/test
  sender only.
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
    def send(self, *, phone: str, code: str) -> None: ...

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
  silently dropped. OTP rows use the roster phone; OTP delivery is Phase 12's caller.
- A source change is never rolled back (FR-NOT-04): delivery runs after the domain
  transaction committed; a failure only writes attempts.

### 4.3 FCM sender (EIR-04, FR-NOT-07)

- `FcmPushSender` implements `PushSender` over stdlib `urllib.request` + `json`.
- Two transport modes are possible; the owner decides (O11-2):
  - **Legacy HTTP** (`https://fcm.googleapis.com/fcm/send`, `Authorization: key=...`):
    no new dependency, simple, but deprecated by Google.
  - **HTTP v1** (`https://fcm.googleapis.com/v1/projects/{id}/messages:send`, OAuth2
    service-account JWT): needs RS256 signing and therefore an approved dependency
    (`cryptography`/`google-auth`) or an external token provider.
- The plan's recommendation is legacy HTTP for the MVP **only if the owner accepts the
  deprecation risk**; otherwise v1 with an approved dependency. The interface isolates
  the choice, so switching does not touch the worker.
- Payload: `to`/`token`, `notification.title`, `notification.body`, `data.deep_link`,
  `collapse_key`. Response `401/403` is permanent (no retry, mark `EXHAUSTED`), `429/5xx`
  retry, malformed response fails the attempt. The server key/service account is read
  with `Config.require`, never logged or stored in the database.
- Endpoint registration (0023) is written by the future API (Phase 12); Phase 11 provides
  `register_endpoint`/`deactivate_endpoint` helpers for tests and the API to call. Token
  uniqueness is enforced; a rotated token deactivates the old row.

### 4.4 OTP interface (FR-ROS-03 boundary)

`OtpSender.send(phone, code)` is the only dependency Phase 12 needs. Phase 11 ships:

- the protocol and a `RecordingOtpSender` for tests (never used in production),
- a `NotificationOtpSender` that stages an `OTP_CODE` notification row when no SMS
  provider is configured — this keeps sign-in testable without inventing a provider,
- an explicit `SmsProviderNotConfigured` error for production paths until O11-1 is
  answered.

The SRS figure names SMS to the roster phone; EIR-04 names FCM push. The contradiction is
recorded in §10 and is the owner's to resolve. No code invents an SMS vendor.

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
def run_delivery_tick(connection, *, push, sms=None, limit=50) -> DeliveryBatch
```

Order: sweep announcements → deliver pending (push for notification rows; OTP rows only
when an `OtpSender` is supplied and the row is an OTP challenge). The worker from Phase 9
calls this after the pipeline tick, or a separate process calls it; either way it holds no
in-memory queue and is restart-safe.

### 4.7 Security

- Push tokens and OTP codes are secrets: never in `repr`, logs, attempt errors, app
  notification bodies (the code goes only to the SMS provider, not the in-app list), or
  test fixtures beyond obviously fake values.
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
| FR-ROS-03 (interface) | `OtpSender`, no provider invented | OTP interface test |
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
| `tests/test_fcm_sender.py` (new) | legacy payload, auth header, 401 permanent, 429/5xx retry, collapse key |
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

W4 is gated on the FCM mode decision and credentials; W1–W3 and W5 run without it.

---

## 9. OWNER decisions for this phase

| ID | Question | Options | Recommendation |
|---|---|---|---|
| O11-1 | OTP/SMS provider and credentials; resolve EIR-04 (FCM) vs SRS figure (SMS) | (a) SMS vendor for OTP + FCM for push; (b) FCM-only; (c) defer, use in-app code for testing only | (a) matches FR-ROS-03's roster-phone wording; owner supplies credentials |
| O11-2 | FCM mode | (a) legacy HTTP key (no dependency, deprecated); (b) HTTP v1 + approved `google-auth`/`cryptography` dependency; (c) owner-run push gateway | (a) for the MVP only if deprecation is accepted; otherwise (b) |
| O11-3 | Retry policy: max attempts, backoff base/cap, batch limit | (a) 5 attempts, 5 s base, 5 min cap, 50/batch; (b) owner values | (a), configuration-only |
| O11-4 | Confirm `VERTICAL` announcement audience = all active members of the vertical | (a) all active members; (b) volunteers only | (a), literal FR-REV-10; confirm |
| O11-5 | Deep-link scheme for the mobile app (out of repo) | `asec://review/{id}` etc. | owner supplies; Phase 11 uses an opaque path string |

---

## 10. SRS ambiguities carried by this phase

1. **OTP delivery channel** — FR-ROS-03 says SMS to the roster phone; EIR-04 names FCM
   push. O11-1 decides; no vendor is invented.
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
| FCM deprecation | interface isolates transport; O11-2 |
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
  person signs in with their institute roll number, and a one-time password (OTP) is sent
  to the phone number the roster holds for that roll number, never to a number typed at
  sign-in.
- **EIR-04** Notification delivery. Push notifications to phones through Firebase Cloud
  Messaging.
- **AC-17** Notification fails → Task change committed; failure recorded; retried without
  duplicates.
- **NFR-SEC-01** All access requires authentication. Discord, model and push credentials
  are held in secure configuration and never exposed to users or logged in errors.
- **NFR-SEC-03 (excerpt)** Phone numbers and message content are visible to a person only
  for people below them in the roster hierarchy.
