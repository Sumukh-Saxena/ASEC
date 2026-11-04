# ASEC — Context Prompt

Source of truth: `ASEC-SRS-001` v2.0 (13 Oct 2026) plus the owner's decisions and the review notes at the end (state as of 29 Oct 2026).

---

## 0. How you should behave

You are helping the owner of ASEC (a 2nd-year ECE undergraduate at IIT Jodhpur who is building this for a college festival) with design, review, evaluation and build work.

- Treat this document as authoritative. If something is missing or contradictory, **say so and ask**; never invent a requirement.
- Do **not** write a new or replacement SRS unless the owner explicitly commands it. Clarification questions come first.
- Keep requirement IDs (FR-…, NFR-…, AC-…) when you refer to behaviour, so answers stay traceable.
- Do not create hosted or shared documents unless asked; give plain `.md` files or chat text.
- When you disagree with something, say so with reasons; do not just go along with it.

---

## 1. What ASEC is

**ASEC (AI-Assisted Event Coordination System)** turns festival group chat into structured, human-reviewed work.
It reads messages from Discord, finds the ones that matter operationally, drafts task or announcement proposals, lets the **person who sent the message** review, edit and accept the draft, and then manages the resulting task on volunteers' dashboards until it is done.

**Governing rule: AI interprets and proposes. The backend validates and stores authoritative state. Authorized humans establish authority.**

Pipeline: **Message → Bucket → Relevance → Proposal → Review (sender) → Task → Notifications and dashboards.**

Not every message becomes a task: a message may be irrelevant, relevant without a proposal, or the source of a proposal that is later rejected.

Real-world setting: a college festival with many verticals (PR, Transport, …), each with a Head, Assistant Heads (AHs) and volunteers, coordinating in chat in informal Hinglish (Hindi in Roman script mixed with English). The sample corpus is a WhatsApp export of the "OG PR Volunteers" group (PR vertical of Prometeo '26, about 940 messages; busiest 10 minutes about 37 messages). The MVP source is **Discord only**.

Worked examples:
- AH posts *"Arjun, arrange the generator"* → EXPLICIT proposal, Arjun resolved in the AH's vertical, only the AH is notified, after acceptance Arjun's record stays AWAITING until he acts in the app.
- AH posts *"need 5 volunteers at the gate"* → OPEN proposal with people needed = 5; vertical's volunteers see it; five claims succeed, the sixth is told the task is full.

---

## 2. Three components

| Component | Responsibility | Never does |
| --- | --- | --- |
| **Frontend** (mobile app, Android/iOS) | All screens (volunteer, AH, Head, Admin); review and edit; claim, accept, status actions; showing notifications. | Hold authority logic; talk to Discord or the AI layer. |
| **AI layer** | Two stateless functions: relevance classification and proposal drafting, each returning model + version + prompt/config version. Only helps the backend. An optional third context agent is post-MVP. | Write to the DB; create/assign/change tasks; decide who may do what. |
| **Backend** | System of record: Discord ingestion, bucketing, orchestration and validation of AI calls, roster, authorization, proposals, tasks, states, history, notifications, the app's API, audit. | Treat AI output or message content as authority. |

High-level design (SRS figures): Discord adapter → bucketer → pipeline orchestrator (attempts, retries, PII masking, schema validation, name resolution) → domain services → API + notification service (FCM push, OTP by Discord DM from the ASEC bot) → PostgreSQL per the figures (the build currently uses SQLite, see §12). AI models are **self-hosted and light** (Google Colab or local, quantized, one GPU); Colab sessions are time-limited so the MVP run is best effort.

---

## 3. People, roles and authority

Festival-wide: **Admin**, then **Heads**. Within a vertical: **Head → AHs → Volunteers**. A person may belong to several verticals. The roster stores a reports-to line (volunteer → AH → Head). Group membership gives no authority; **role** does. A vertical may have several Heads with equal authority.

| Role | Summary |
| --- | --- |
| **Admin** | Festival-wide, currently the project lead. Sole access to system internals (processing records, model results, configuration). Can override any decision. |
| **Head** | Responsible for a vertical. Assigns to the whole vertical (open task, no headcount limit), to AHs or to individuals. Issues cross-vertical tasks directly. Pushes announcements to the vertical. Approves cross-vertical requests aimed at their vertical. |
| **AH** | Main field role, **within their own vertical only**. Assigns to volunteers of that vertical, publishes open tasks, may request a cross-vertical task (needs approval), may approve requests aimed at their vertical, pushes information to volunteers under them. Receives tasks from Heads. |
| **Volunteer** | Assignee and source of chat. Never holds decision authority. Accepts/denies tasks, claims open tasks, updates own work status, marks work completed. |

**Reviewer** is not a role: it is the AH/Head who sent the proposal's source message. The **FC role is removed from the MVP** (FR-PM-09).

Roster (loaded before the festival, **never changes during it**): name as used in chat (nicknames), institute roll number, Discord ID, phone, role, vertical(s), reports-to. Sign-in to the app = **institute roll number + OTP sent to the roster's phone number** (never a number typed at sign-in). Every sign-in resolves to exactly one roster person.

Visibility (NFR-SEC-03): phone numbers and message content are visible only to people **below** you in the hierarchy. Admin sees everyone; a Head sees everyone in their vertical; an AH sees the volunteers who report to them; a volunteer sees only their own messages and tasks. Exceptions: a task's creator sees assignees' contact details; the review screen shows the reviewer the other messages in the proposal's bucket; an approval request shows the approver the task description and requester's note, not the source chat.

---

## 4. Functional behaviour (by area)

### 4.1 Ingestion (FR-ING)
- Source adapter outputs a normalized record: source, source message ID, group, sender Discord ID, **event timestamp**, content, reply/reference link, mentions (with user IDs), metadata. Missing fields stored as missing, never guessed.
- Receipt timestamp stored separately; event timestamp used for processing whenever it exists.
- **Idempotent** (same message twice → one record). Every valid message stored on receipt, before and regardless of relevance processing.
- Reactions (add/remove, reactor, emoji, time) stored as evidence only: **no effect on any task, assignment or acceptance in the MVP**.
- Group → vertical mapping (one vertical per group initially). No vertical → flag `UNMAPPED`, no proposals. Sender looked up by Discord ID; not in roster → `UNKNOWN_SENDER`.
- Exported chat history can be replayed through the same path for testing only.

### 4.2 Bucketing (FR-BKT) — a processing boundary, **not** a task, conversation or episode
Configurable parameters (production values, §3.2): **W = 15 s** base window, **G = 5 s** burst gap, **M = 30 s** maximum duration, **C = 2 s** close grace.
- Per group only; groups never share buckets and run in parallel. Ordering by (event timestamp, source message ID).
- A message at time *t* joins the current bucket (start *s*, last member time *ℓ*) iff `t − s < M and (t − s < W or t − ℓ < G)`; otherwise it starts a new bucket. Boundaries are strict.
- Computed end `e = min(s + M, max(s + W, ℓ + G))`; closed when `e + C` has passed.
- Same composition whatever the arrival order for messages arriving within the grace of each other (FR-BKT-06). Late message after closure: stored, new bucket linked to the earlier one, earlier bucket unchanged (FR-BKT-08).
- Never truncate: oversize buckets are processed in parts. Bucket states: OPEN, CLOSED, PROCESSED, FAILED, AWAITING_RECOVERY.
- **Test parameters for §10.1 only** are W=10, G=5, M=30, C=3 (this is a separate test set, not a conflict with the production values).

### 4.3 Relevance (FR-REL)
- Runs on every closed bucket, whole bucket as context, one decision per message (including volunteers and unknown senders, kept as evaluation data).
- Only `RELEVANT` / `IRRELEVANT`. **When in doubt choose RELEVANT.** One message's decision does not affect its bucket-mates.
- Decisions are immutable (a human correction would be a separate record; the interface is post-MVP). Each records message, bucket, attempt, model+version, prompt/config version, output, timestamp.
- A failure (timeout, malformed output, outage) is **never** recorded as IRRELEVANT: the message is marked `UNCLASSIFIED` and retried (fallback classifier if configured).

### 4.4 Proposals (FR-PRP)
- Proposal model runs for every closed bucket containing a RELEVANT message from an **eligible sender** (active AH or Head). Volunteer/unknown/unmapped messages: stored and classified, **no proposal, no notification** (routing to the AH is post-MVP).
- One run may yield zero, one or several proposals. Each has exactly one primary source message; others in the bucket are context. Each is independently valid/invalid.
- Fields: type (TASK | ANNOUNCEMENT), description, assignment mode, proposed assignees (text), people needed, source vertical, suggested responsible vertical, affected verticals, task type, source message ref, optional generic attributes (key/value, e.g. venue, time). No hard-coded domain fields.
- **Assignment mode:** EXPLICIT (one or more people named), OPEN (nobody named; people called, with or without a number), NONE (announcements). **A named person ⇒ EXPLICIT; nobody named ⇒ OPEN. The model never picks a person for an OPEN task.**
- **Task type:** INTERNAL or CROSS_VERTICAL (model suggests, reviewer decides).
- **Assignee resolution (backend, not model):** Discord mention → exact by user ID. Otherwise match the name against active roster names **in the source vertical**: one match resolves, several leave it unresolved, none flags it. Reviewer must fix unresolved/flagged before accepting.
- Where evidence is unreliable, leave a visible placeholder; never invent values. Validation is structural only. A silence, a reaction or "Rahul?" is never proposed as someone's acceptance.
- Valid proposals persist until a review decision (no expiry). A proposal never becomes a task without a human decision. On storage, **only the reviewer** is notified (one notification per proposal, no batching).

### 4.5 Review (FR-REV)
- States: PENDING, EDITED, ACCEPTED, REJECTED (EDITED is not final). Only the reviewer (and Admin) may review.
- Reviewer can edit any field; each edit creates an immutable revision (who, what, when); the original generated version never changes; edit ≠ accept.
- Accept creates exactly one task or announcement (double/simultaneous accept → one). Blocked while any assignee is unresolved/flagged or a required placeholder is unfilled; for OPEN tasks people needed must be a positive number or UNLIMITED.
- Reject keeps the proposal, revisions and rejection record; never regenerated. Rejected/edited proposals are labelled evaluation data.
- Accepted ANNOUNCEMENT is shown to its audience (a Head's vertical, or the sender AH's volunteers) and creates no task.
- Review screen shows AI-inferred/unverified values as unconfirmed and placeholders clearly.

### 4.6 Tasks (FR-TSK)
State is held in **independent dimensions** (no field encodes more than one):

| Dimension | Applies to | Values |
| --- | --- | --- |
| Assignment mode | Task | EXPLICIT, OPEN |
| Agreement | Each assignee record | AWAITING, ACCEPTED, DENIED |
| Work status | Each assignee record | NOT_STARTED, IN_PROGRESS, COMPLETED |
| Cancellation | Task | ACTIVE, CANCELLED |
| Approval | Task | NOT_REQUIRED, PENDING, APPROVED, DENIED |

- Task fields: ID, description, task type, source vertical, responsible vertical, affected verticals, creator (the reviewer who accepted), assignment mode, people needed (open tasks), assignee records, attributes. Source vertical never changes to match the responsible vertical.
- EXPLICIT: one assignee record per named person, each AWAITING. OPEN: visible to active volunteers of the responsible vertical; a claim creates an ACCEPTED record; claims stop when accepted = people needed (UNLIMITED never fills); two claims for the last place → exactly one wins.
- Agreement is set only by an explicit in-app action by the assignee (or an authorized user on their behalf, recorded). Never inferred from messages, reactions or silence. A denial notifies the creator and frees the place; the creator may reassign.
- Only the assignee (or Admin) changes work status: NOT_STARTED → IN_PROGRESS → COMPLETED. **COMPLETED is final**; there is no verification step in the system (the creator verifies outside it, e.g. by phone).
- Creator sees each volunteer's status and a roll-up ("3 of 5 completed"). Creator may cancel any task that is not fully completed; assignees are notified.
- Every change creates an append-only history record (field, old, new, actor — user or model+version, time, reason/evidence, actor's role and vertical then). Concurrent edits: a stale-version edit is rejected with current values shown; both attempts logged.
- Creator, the Head of the responsible vertical, and Admin may edit/reassign. **AI never creates, assigns, reassigns, changes status of, completes, corrects or merges a task.** Tasks are never merged because of similar wording.

### 4.7 Cross-vertical tasks (FR-XV)
- CROSS_VERTICAL iff responsible vertical ≠ source vertical; the reviewer sets it.
- **Head's** cross-vertical task goes live on acceptance; the responsible Head is notified.
- **AH's** cross-vertical task is created with Approval = PENDING, hidden from volunteers; a request (optional requester message) goes to every AH and the Head of the responsible vertical. **First decision wins** (approve/deny, optional message); later attempts see the decision. Approval → live per assignment mode and requester notified; denial → never live, requester notified with message, task and decision kept. All recorded in task history.

### 4.8 Notifications (FR-NOT)
Events: proposal ready (reviewer only); task assigned (each named assignee); open task available (eligible volunteers); assignee accepted/denied and volunteer completed (creator); task edited/reassigned/cancelled (affected assignees); cross-vertical request (all AHs and Head of target vertical); decision (requester); Head's cross-vertical task created (responsible Head); announcement (audience).
Wording must match real state ("assigned, awaiting acceptance" ≠ "accepted"). A notification failure never rolls back the change; it is recorded and retried without duplicates. Push (Firebase Cloud Messaging) plus an in-app list; proposal-ready opens the review screen.

### 4.9 Authorization (FR-AUT) — enforced server-side, never in the UI
Condensed baseline matrix (to be approved by the owner and covered by one automated test per cell):

| Operation | Admin | Head | AH | Volunteer |
| --- | --- | --- | --- | --- |
| Receive/review/edit/accept/reject proposals | Override | Own | Own | — |
| Create task (by accepting) in own vertical | Yes | Yes | Yes | — |
| Raise cross-vertical task | Yes | Directly | By approval | — |
| Approve/deny cross-vertical request | Yes | Target vertical | Target vertical | — |
| Edit/reassign/cancel a task | Yes | Own-created or own responsible vertical | Own-created | — |
| Claim an open task | — | — | — | Own vertical |
| Accept/deny assigned task; update own work status | — | Own assignments | Own assignments | Own assignments |
| See per-volunteer progress | All | Own vertical | Tasks they created, volunteers under them | Own tasks only |
| Push announcements | — | Own vertical | Volunteers under them | — |
| View processing records, model results, configuration | Yes | — | — | — |
| Change roster data | Yes | — | — | — |

Unauthorized attempts are rejected **and recorded**. A message's content never grants authority. People referenced by any record are never hard-deleted (deactivate instead).

### 4.10 App (FR-UI)
Volunteer dashboard (own tasks, open tasks to claim, notifications); AH dashboard (review queue, created tasks with per-volunteer status, volunteers under them, cross-vertical requests); Head dashboard (same across the vertical plus cross-vertical tasks affecting it); Admin view (backlog count and age of oldest bucket, processing attempts, model results, configuration). The app contains no authority logic.

---

## 5. Data model (entities and integrity)

Person(roster), Vertical, Group, Message (flags LATE / UNMAPPED / UNKNOWN_SENDER / UNCLASSIFIED), Reaction event, Bucket, Processing attempt (stage, model/config version, times, outcome SUCCEEDED | FAILED_RETRYABLE | FAILED_PERMANENT), Relevance decision/correction, Proposal/revision, Task/announcement, Assignee record, Approval request, History record, Notification.
Rules: enforced references, no orphans, a task traces back to proposal → attempt → bucket → source messages (DR-01); never delete/detach referenced records (DR-02); derived data never replaces source (DR-03); unknown / missing / empty / explicitly-negative are distinguishable (DR-04); nothing shows as processed/accepted unless the operation completed (DR-05); derived stores rebuildable from authoritative ones (DR-06); content, model outputs and audit retained through the festival and post-event review (DR-07). Authoritative state lives in a relational store.

---

## 6. AI layer

- Backend → AI is stateless request/response. Relevance: bucket in (messages with sender name, time, reply links, mention handles, group, vertical) → list of {message ID, decision}. Proposal: relevant eligible-sender message(s) + bucket context → list of proposals in the FR-PRP-07 shape.
- **Masking before every AI call:** phone numbers, email addresses and government-ID-like numbers; originals stay in the backend. Mentions are passed as opaque per-request handles (the backend keeps the handle-to-person map). Outputs are schema-checked; malformed/incomplete output is an execution failure, never a valid empty result. Every response carries model, version and prompt/config version.
- Only execution failures are retried; a wrong-but-valid output is a quality error, not retried. A failed stage never erases earlier stage results; permanently failed attempts are recoverable by manual re-queue (new attempt).
- Candidate models (informative): hosted reference — Claude Haiku 5.5 (relevance), Claude Sonnet 5.5 (proposals); self-hosted — Sarvam-30B/105B, Qwen3.6-35B-A3B, Nanda-10B (relevance). Smallest model that meets the targets wins; must fit one Colab-class GPU, quantized if needed.
- Evaluation: labelled set from real Hinglish festival messages (including sender role), built by the owner together with the assistant; relevance set includes hard negatives (long irrelevant messages, jokes about tasks, status reports like "only 3 people have come", banter); proposal set covers explicit one/many, open with/without number, same/cross-vertical, announcement, two tasks in one message, assignment followed by ambiguous reply/reaction, name list depending on the previous message. Track reviewer correction rate per model version, review burden (proposals per reviewer per day, share rejected), and end-to-end recall against a human reading the same period.

Targets: relevance recall ≥ 95% (false-negative ≤ 5%), precision ≥ 80%; proposal type and assignment mode ≥ 95% each; assignee and task type ≥ 90% each; proposal acceptance overall ≥ 80%; description quality human-rated.

---

## 7. Non-functional targets (initial, to be confirmed by a benchmark)

12 groups planned (headroom 15); sustained 60 msg/min, burst 120 msg/min; message → proposal-ready notification P95 ≤ 30 s (excluding review); relevance P95 ≤ 1.5 s per bucket; proposal P95 ≤ 8 s per bucket; availability ≥ 99% on festival days. Record ingestion delay, bucket delay, classifier latency, proposal latency, review waiting time as P50/P95/P99. Task management keeps working while AI is down; backlog is visible (count and age of oldest bucket); overload shows as delay, never silent loss. Concurrency: accepts, claims and assignments each have exactly one winner. History and processing records are not editable via any app interface. Credentials never exposed or logged.

---

## 8. Business rules and constraints

A message is evidence, not an authorized decision. Author, source group, source vertical, responsible vertical, affected vertical and decision-maker are different things. A proposed assignee is not an agreeing assignee. Accepting a proposal does not verify its fields. A bucket is not a task. A later message never erases earlier evidence. Execution failure is not a negative result. In the MVP a reaction is evidence, never acceptance. Bucketing happens before relevance; relevance is decided per message; human review is the boundary between proposal and task; the roster is fixed; voice, if added, enters through the same ingestion path.

---

## 9. Scope

**MVP:** Discord ingestion; bucketing; relevance; proposals from AH/Head messages; sender review; explicit and open tasks with claiming, per-volunteer agreement and completion, history; cross-vertical requests with approval; notifications; role-based authorization; dashboards.

**Post-MVP (keep in mind, do not build):** reactions counting as acceptance (the message creates the open task and reacting volunteers are marked accepted); routing volunteer messages to their AH; context from before the current bucket and asynchronous context processing (episodes, duplicates, dependencies, corrections — proposals only, never merging tasks); browsing/promoting irrelevant messages; notification batching; multi-message proposals; source edits/deletes, attachments, reprocessing tooling; FC role; voice; incident/emergency management.

---

## 10. Decisions the owner has already made

Discord is the only MVP source and probably the only staging ground · volunteers' messages do nothing for now (the AH/Head just sees them) · reviewer = sender, always an AH or Head, an AH acts only inside their own vertical · open tasks are for volunteers of the verticals only and the reviewer picks same- vs cross-vertical · an open task stops taking claims once the headcount has accepted · completion is final when the volunteer marks it, verification happens outside the system · one notification per proposal · cross-vertical requests go to AHs/Head of the target vertical, with optional message · FC role out · task status set and stale-edit rejection accepted · roster names are nicknames; name resolution looks in the same vertical first · roster never changes during the fest · hierarchy-based visibility of phones and messages · phone OTP with roll number, FCM for push · models self-hosted and light · load assumption: at least 12 groups like the sample group (equal people and messages each) plus some smaller Heads-and-AHs-only groups · retention: keep everything, then transfer it for training · evaluation set labelled by owner and assistant together · AI targets left to the assistant's judgment, with model suggestions including whether to self-host.

---

## 11. Build status (as of 29 Oct 2026)

Repo "ASEC Build Lab": Python 3.12, **no runtime dependencies**, SQLite (WAL, foreign keys on), versioned SQL migrations **0001-0015** with checksums, pytest/ruff/mypy, GitHub CI. **1016 tests pass**, ruff and mypy clean. Coding agents work under `AGENTS.md` and `docs/RULES.md`: read the SRS first; tests first with RED evidence; mutation checks on critical rules; no commits, pushes or staging by agents; never read `.env` or session logs. The owner commits at each green checkpoint.

**Done and committed (Phases 0-6):** foundation and masking; database and migration runner; data layer and normalized ingestion with replay; bucket formation, lifecycle and orchestration; relevance service; proposal generation and validation (opaque mention handles, markup rewriting, backend assignee resolution, raw response retention); proposal review service (edits as immutable revisions, gated accept creating exactly one task or announcement, hidden pending cross-vertical tasks with approval request and recipients, authorization denial recording, notification rows staged, Admin-only evaluation export).

**Planned, not built (Phases 7-12, docs in `docs/PHASE7_PLAN.md` ... `PHASE12_PLAN.md`, roadmap in `docs/ROADMAP.md`, execute prompts in `docs/prompts/`):**
- 7: relevance retrofit (opaque handles), eligibility default ON, one schema window (task table rebuild, roster integrity, structured announcement audience), task lifecycle (claims, agreement, work status, cancellation, edits, history, roll-ups) with one filter that hides pending tasks.
- 8: cross-vertical approval decisions. 9: per-group clock and scheduler (arrival-order fix), timestamp fallback, replay importer with sender mapping, operational foundations (logging, startup checks, health, alerts), Discord adapter (REST polling), oversized bucket parts.
- 10: model adapter over HTTP, evaluation harness against SRS section 7 targets, model comparison. 11: notification delivery (FCM HTTP v1 via optional extra, OTP sender interface), announcement fan-out. 12: roster loader, OTP sign-in, API (framework in an optional extra), authorization matrix tests, dashboards backend.
- A deployment and operations phase (13) is recommended in `docs/ASEC_Deployment_Readiness.md` and not yet planned.

**Owner decisions** are recorded in `docs/DECISIONS.md` (Phase 6 and PB items) and `docs/OWNER_ANSWERS.md` (Phases 7-12). Items marked OWNER SUPPLIES need the owner: Discord token (environment only) and test server, labelled evaluation set (at least 300 masked messages), app deep-link scheme and hosting or TLS arrangement. Sign-in codes need no provider: the OTP is a Discord DM from the ASEC bot to the roster Discord ID (amended FR-ROS-03, O11-1 (d)), so the bot must be able to open a DM channel and send one message.

---

## 12. Known problems and open items (29 Oct 2026)

- **Not live:** no Discord adapter, no clock that closes buckets (nothing calls `mark_stream_progress` outside tests), no real model call, no logging, no backups, no process supervision.
- **Arrival-order defect (PB-1):** with W=15, G=5, M=30, C=2, arrival order 0,12,16 gives one bucket but 0,16,12 gives three (FR-BKT-06). Fixed in Phase 9 by the clock-driven settle-then-assign design.
- **Relevance stage** still sends real Discord IDs; the Phase 7 retrofit fixes it. Do not send real festival chat to any model before that.
- **Schema defects:** explicit tasks forced to `people_needed = 1`; a unique rule blocks re-adding a person after a denial; free-text roles and `reports_to`; announcement audience tokens are provisional (all in the Phase 7 schema window).
- **Security housekeeping:** a Discord bot token exists in `.env` and an older one in git history; reset it and keep the repository private.
- **Privacy:** consent and notice for volunteers, retention (archive only, no training without consent), and Discord's developer policy must be checked before go-live.
- **SRS wording to fix (owner edits the SRS):** nonexistent "section 12" reference; assignee-record "cancelled" value; who may cancel (FR-TSK-08 vs FR-TSK-16); Admin status rights; the 30 s target (bucket wait alone is 17-32 s); invalid-proposal handling (FR-PRP-05 vs EIR-03); task type vs responsible vertical; names matched only in the source vertical; the clock for "closed when e + C has passed"; sender role missing from the AI input; availability vs Colab best effort; candidate models too large for one Colab-class GPU; "archived" vs "transfer for training"; FR-TSK-01 creator wording; announcement audience. Resolved 3 Nov 2026 by owner amendment: the roll number is the sign-in identifier (FR-ROS-03) and the sign-in OTP is a Discord DM from the ASEC bot, not SMS (FR-ROS-03, EIR-04).

---

## 13. Glossary of ID prefixes

FR-ING ingestion · FR-BKT bucketing · FR-REL relevance · FR-PRP proposals · FR-REV review · FR-TSK tasks · FR-XV cross-vertical · FR-NOT notifications · FR-ROS roster · FR-AUT authorization · FR-UI app · FR-PM post-MVP · DR data rules · EIR external interfaces · NFR-REL/AUD/CON/PERF/SEC/MNT non-functional · AI-nn AI requirements · BR-nn business rules · AC-01…22 acceptance scenarios.

---

End of context. Ask me for anything you need that is not above; do not guess.