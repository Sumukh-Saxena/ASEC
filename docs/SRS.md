# ASEC — Software Requirements Specification

Status: Requirements baseline draft. This document is self-contained.

## 1. Introduction

### 1.1 Purpose

This SRS defines what ASEC shall do. Architecture, design, test and deployment documents derive from it and shall not change it.

ASEC turns festival group chat into structured, human-reviewed work. It reads messages from Discord, finds the ones that matter operationally, drafts task proposals, lets the person who sent the message review and edit the draft, and then manages the resulting task on volunteers’ dashboards until it is done.

### 1.2 System at a glance

ASEC has three components.

| Component | In one line |
|---|---|
| Frontend | A mobile app: dashboards, the review screen and notifications that people use. |
| AI layer | Two stateless AI functions (relevance classification and proposal drafting) that the backend calls. It only helps the backend. It holds no state and no authority. An optional third agent for preserving context might be considered for post MVP build. |
| Backend | The system of record: ingestion, bucketing, orchestration of the AI layer, database, task states, authorization, notifications, and the API the app uses. |

Governing rule: AI interprets and proposes. The backend validates and stores authoritative state. Authorized humans establish authority.

### 1.3 Scope

**In the MVP**

- Receive messages from Discord and preserve them.
- Group messages into short per-group processing windows (buckets).
- Classify each message as relevant or irrelevant.
- Draft task and announcement proposals from relevant messages sent by AHs and Heads.
- Review: the sender edits, accepts or rejects.
- Tasks: explicit assignment, open assignment with a headcount, claiming, per-volunteer agreement and completion, history.
- Cross-vertical requests with approval.
- Notifications, role-based authorization, and dashboards for each role.

**Post-MVP (kept in mind, not built now)**

- Reactions counting as acceptance (FR-PM-01).
- Routing volunteer messages to their AH (FR-PM-02).
- Context from before the current bucket, and context processing: episodes, duplicates, dependencies, corrections (FR-PM-03, FR-PM-04).
- Browsing and promoting irrelevant messages (FR-PM-05), notification batching, proposals backed by several messages.
- Source edits and deletes, attachments, full reprocessing tooling.
- FC role, voice input, incident and emergency management.

### 1.4 Definitions

| Term | Meaning |
|---|---|
| Source | Platform that supplies messages. MVP: Discord. |
| Group | A channel on the source. Each group maps to one vertical for initial building phase. |
| Vertical | An organizational unit of the festival (for example PR, Transport). |
| Roster | The stored list of festival people: name (as used in chat), Discord ID, phone number, role, vertical(s), reports-to line, institute roll number. Fixed during the festival. |
| Message | One communication with sender, timestamps, content, mentions, and reply data. |
| Eligible sender | An active AH or Head in the roster. Only their messages can produce proposals. |
| Bucket | A temporary per-group window of messages used as classification input. Not to be considered same as a contextual episode |
| Relevance decision | RELEVANT or IRRELEVANT, attached to one message. |
| Proposal | A machine-generated candidate task or announcement. It has no authority until a human accepts it. |
| Reviewer | The sender of a proposal’s source message. Not a separate role. |
| Task | An accepted, actionable work item with identity, creator, assignee records, state, source reference and history. |
| Assignee record | One person’s part in a task: their agreement and their work status. |
| Source vertical | The vertical of the group a message came from. |
| Responsible vertical | The vertical that must do the work. Usually the source vertical after analysis of data, but discretion must remain. |
| Affected vertical | A vertical whose participation or interests are affected. |
| Processing attempt | One recorded execution of a pipeline stage. |

### 1.5 Conventions

Shall = mandatory.

Should = recommended.

Items marked [Post-MVP] are outside MVP acceptance. IDs are permanent. Parameters called “configuration” are changed without code changes.

## 2. Overall Description

### 2.1 How ASEC works

1. An AH or Head posts in a Discord group, either naming someone (“Arjun, arrange the generator”) or calling for people (“need 5 volunteers at the gate”).
2. The backend stores the message, groups it with its neighbours into a bucket, and sends the closed bucket to the AI layer.
3. The AI layer classifies each message as relevant or irrelevant, and for relevant messages from an AH or Head it drafts a proposal.
4. The backend validates the draft and resolves any named person against the roster.
5. The sender, and only the sender, gets a notification that the proposal is ready.
6. The sender reviews it in the app, edits anything, and accepts. This is the only way a proposal becomes a task.
7. The task enters the system. Named volunteers see it awaiting their acceptance. For an open task, eligible volunteers see it and claim a place.
8. Volunteers accept, do the work, and mark it completed on their dashboards.
9. The sender is notified and sees on their dashboard which volunteers completed which task. Verifying the work (for example by phone) happens outside the system.

Pipeline: Message → Bucket → Relevance → Proposal → Review (sender) → Task → Notifications and dashboards.

Not every message becomes a task. A message may be irrelevant, relevant without producing a proposal, or the source of a proposal that is later rejected. Relevant messages that shouldn’t produce a proposal are to be considered out of scope for the MVP.

### 2.2 Component responsibilities

| Component | Owns | Never does |
|---|---|---|
| Frontend | All screens (volunteer, AH, Head, Admin); review and edit; claim, accept and status actions; showing notifications. | Hold authority logic; talk to Discord or the AI layer directly. |
| AI layer | Relevance decisions and proposal drafts, each with model and version information. | Write to the database; create, assign or change tasks; decide who may do what. |
| Backend | Discord ingestion; bucketing; calling and validating the AI layer; roster; authorization; proposals, tasks, states and history; notifications; the app’s API; audit. | Treat AI output or message content as authority. |

### 2.3 Organization and roles

Festival-wide: Admin, then Heads. Note:- A single vertical MAY have more than one heads, in which case each head is to be considered with the same authority.

Within a vertical: Head → Assistant Heads (AHs) → Volunteers. A person MAY belong to several verticals.

The roster stores each person’s reports-to line (volunteer → AH → Head). Membership in a group does not by itself give authority. Authority comes from role.

| Role | Description |
|---|---|
| Admin | Festival-wide. Sole access to system internals (processing records, model results, configuration). Can override any decision.Currently to be considered the project lead. |
| Head | Responsible for a vertical. Can assign tasks to the whole vertical (an open task with no headcount limit), to AHs, or to individual volunteers. Issues cross-vertical tasks directly. Pushes announcements. Approves cross-vertical requests aimed at their vertical. |
| AH | The main field role, with responsibility within their own vertical only. Assigns tasks to volunteers of that vertical, publishes open tasks, may request a cross-vertical task (needs approval), and may approve requests aimed at their vertical. Receives tasks from Heads. |
| Volunteer | Assignee, source of communication, discussion participant. Never holds decision authority. Accepts or denies tasks, claims open tasks, updates their own work status and marks their work completed. |

The Reviewer is not a role. The reviewer of a proposal is the AH or Head who sent its source message. The FC role is removed from the MVP (FR-PM-09).

### 2.4 Operating context and assumptions

- Discord is the only source for the MVP. A dry run on a separate Discord server, with the real group structure and roster, shall be completed before the festival.
- The roster is loaded before the festival and never changes during it.
- Planned scale: about 12 groups, each similar to the sample group (about 940 messages over the festival; busiest 10 minutes about 37 messages).
- Chat is informal, fragmentary Hinglish: Hindi in Roman script mixed with English.
- The source may deliver duplicates, late events and out-of-order events. A bucket can contain unrelated discussions. A later message can correct an earlier one. A named person has not necessarily agreed to the work. AI output can be incomplete or wrong.
- Volunteers often react to a message to show availability. In the MVP such reactions are evidence only.

### 2.5 High-level design

The figures in this section and in sections 3 to 5 are informative views of the requirements. Where a figure and a requirement disagree, the requirement governs (section 12). Editable Excalidraw sources of every figure are kept alongside this document.

Figure 1 shows the three components (frontend, AI layer, backend) and the external services around them.

**[Figure 1 image: System context and components]**

*Figure 1 — System context and components (high-level design)*

Figure 2 follows one message from the sender to a completed task, lane by lane.

**[Figure 2 image: End-to-end flow from message to completed task]**

*Figure 2 — End-to-end flow from message to completed task (high-level design)*

Figure 3 shows where each part runs in the small MVP run, including the self-hosted model.

**[Figure 3 image: Deployment view for the MVP run]**

*Figure 3 — Deployment view for the MVP run (high-level design)*

## 3. Functional Requirements

All requirements are MVP unless marked [Post-MVP].

### 3.1 Ingestion

- **FR-ING-01** The system shall receive messages from Discord through a source adapter that outputs a normalized message record.
- **FR-ING-02** Each record shall keep, where Discord provides them: source, source message ID, group, sender’s Discord ID, event timestamp, content, reply or reference link, mentions (with user IDs), and other metadata needed for traceability. A missing field shall be stored as missing, never as a guessed value.
- **FR-ING-03** The receipt timestamp shall be stored separately from the event timestamp. The event timestamp shall be used for processing decisions whenever it exists.
- **FR-ING-04** Ingestion shall be idempotent. The same message delivered twice shall produce one record.
- **FR-ING-05** Every valid message shall be stored on receipt, before and regardless of relevance processing. A later failure shall not lose it.
- **FR-ING-06** Reply and reference links shall be preserved as supplied.
- **FR-ING-07** Reactions (additions and removals: reactor, emoji, time) shall be stored as evidence only attached to the message. In the MVP they shall have no effect on any task, assignment or acceptance. In the MVP, reaction polling is deferred (owner decision); this requirement is not met until a later release. See docs/KNOWN_GAPS.md.
- **FR-ING-08** The system shall be able to ingest an exported chat history through the same path as live messages, for replay and testing only. Exports that lack second-level timestamps, message IDs or reactions give indicative results only.
- **FR-ING-09** Each message shall be mapped to its group and to that group’s vertical. A message from a group with no vertical shall be stored and flagged UNMAPPED, and shall produce no proposals.
- **FR-ING-10** The sender of each message shall be looked up in the roster by Discord ID. A sender not in the roster shall be flagged UNKNOWN_SENDER.
- **FR-ING-11** [Post-MVP] Source edits and deletions shall be stored as new versions or flags. The original received content shall be retained.
- **FR-ING-12** [Post-MVP] Attachments shall be stored or referenced.

### 3.2 Bucketing

Buckets let the AI read a message together with its neighbours. All parameters are configuration, set for the fastest processing that keeps related messages together, and tuned on live Discord data.

| Parameter | Symbol | Value |
|---|---|---|
| Base window | W | 15 s |
| Burst gap | G | 5s |
| Maximum duration | M | 30 s |
| Close grace | C | 2s |

- **FR-BKT-01** The system shall place every message in exactly one bucket.
- **FR-BKT-02** A bucket shall contain messages from one group only. Different groups never share a bucket, whatever their timing, and their buckets form independently and in parallel.
- **FR-BKT-03** Buckets shall be formed on event timestamps. Messages shall be ordered by (event timestamp, source message ID).
- **FR-BKT-04** A message with event time t joins the current bucket (start s, last member time ℓ) if and only if t − s < M and (t − s < W or t − ℓ < G). Otherwise it starts a new bucket. A message exactly M after the start never joins. A gap of exactly G outside the base window never joins.
- **FR-BKT-05** A bucket’s computed end is e = min(s + M, max(s + W, ℓ + G)). The bucket is closed when e + C has passed.
- **FR-BKT-06** For messages arriving within the close grace of each other, bucket composition shall be the same whatever the arrival order.
- **FR-BKT-07** Closing a bucket and adding messages to it for the same group shall not run concurrently in a way that gives two different results.
- **FR-BKT-08** A message arriving after its bucket has closed shall be stored and placed in a new bucket by the same rule, linked to the earlier bucket. The earlier bucket and its results shall not change.
- **FR-BKT-09** A bucket is a processing boundary only. It shall not be treated as a task, conversation or episode, and its messages shall not be assumed to be related.
- **FR-BKT-10** Bucket identity shall be separate from task and proposal identity. Links may be recorded for traceability.
- **FR-BKT-11** No message shall ever be silently truncated or deleted. A bucket too large for a model’s input shall be processed in parts, never cut.
- **FR-BKT-12** The system shall record whether each bucket is OPEN, CLOSED, PROCESSED, FAILED or AWAITING_RECOVERY.

Figure 4 shows the join rule of FR-BKT-04 and the bucket states of FR-BKT-12.

**[Figure 4 image: Bucket join rule and bucket states]**

*Figure 4 — Bucket join rule and bucket states (low-level design, FR-BKT-04 to FR-BKT-12)*

### 3.3 Relevance classification

- **FR-REL-01** The classifier shall run on every closed bucket, using the whole bucket as context, and produce one decision per message. This includes messages from volunteers and unknown senders, which are kept as evaluation data.
- **FR-REL-02** The only decisions are RELEVANT and IRRELEVANT. There is no UNCERTAIN state.
- **FR-REL-03** When in doubt the classifier shall choose RELEVANT. Missing real work is worse than reviewing extra proposals.
- **FR-REL-04** A relevant message does not make its bucket-mates relevant, and an irrelevant one does not make them irrelevant.
- **FR-REL-05** Each decision shall record: message, bucket, processing attempt, model and version, prompt or configuration version, output, timestamp.
- **FR-REL-06** A decision is immutable. A human correction would be a separate record, and the corrected result is the one the system would use from that moment. (The correction interface is Post-MVP, FR-PM-05.)
- **FR-REL-07** A classifier failure (timeout, model unavailable, malformed output, infrastructure error) shall never be recorded as IRRELEVANT. Affected messages shall be marked UNCLASSIFIED and retried, using a fallback classifier if one is configured.
- **FR-REL-08** Irrelevant messages shall stay stored with their decisions as source and evaluation data. Only RELEVANT messages from eligible senders enter the proposal stage.

### 3.4 Proposal generation

- **FR-PRP-01** For every closed bucket that contains a RELEVANT message from an eligible sender, the system shall run the proposal model. There is no second relevance filter after it.
- **FR-PRP-02** Messages from volunteers, unknown senders and unmapped groups shall be stored and classified but shall produce no proposal and no notification in the MVP. They may appear as read-only context. [Routing them to the sender’s AH is Post-MVP, FR-PM-02.]
- **FR-PRP-03** Each proposal shall have exactly one primary source message (a relevant message from an eligible sender). Other messages from the bucket may be attached as context. [Proposals backed by several primary messages are Post-MVP.]
- **FR-PRP-04** One run may produce zero, one or several proposals. Zero is a valid result, not a failure.
- **FR-PRP-05** Each proposal is independently identifiable, reviewable and validated. An invalid proposal shall not invalidate the others from the same run.
- **FR-PRP-06** Proposal type is TASK or ANNOUNCEMENT.
- **FR-PRP-07** A proposal shall contain: type, description, assignment mode, proposed assignee(s) as text, people needed (open tasks), source vertical, suggested responsible vertical, affected verticals, task type, reference to the source message, and optional generic attributes (key and value, for example venue or time). The core schema shall not hard-code domain-specific fields.
- **FR-PRP-08** Assignment mode is EXPLICIT (the message names one or more people), OPEN (no one is named; people are called to do it, with or without a number), or NONE (announcements only). If no person is named in a task, the model shall treat it as OPEN. The model shall never pick a person for an OPEN task.
- **FR-PRP-09** Task type is INTERNAL (work inside the source vertical) or CROSS_VERTICAL. The model suggests it and the reviewer decides.
- **FR-PRP-10** The model outputs assignees as text only. The backend resolves them: (a) a Discord mention resolves exactly by user ID; (b) otherwise the name is matched against active roster names in the source vertical: one match resolves, several matches leave it unresolved, no match flags it. The reviewer must fix unresolved or flagged assignees before accepting.
- **FR-PRP-11** Where the evidence does not give a reliable value, the field shall hold a visible placeholder for the reviewer to fill. The model shall not invent a value.
- **FR-PRP-12** Each proposal shall record its generation run, source and context messages, model and version, prompt or configuration version, and timestamp.
- **FR-PRP-13** Validation is structural only (required fields, types, allowed values). A valid structure is not treated as correct content.
- **FR-PRP-14** Valid proposals shall be stored and stay retrievable until a review decision is made, including across worker failure. They do not expire automatically.
- **FR-PRP-15** A silence, a reaction, or a question like “Rahul?” shall not be proposed as someone’s acceptance.
- **FR-PRP-16** A proposal shall never become a task without a human review decision.
- **FR-PRP-17** When a valid proposal is stored, the backend shall notify its reviewer, and no one else.

Figure 5 shows the order of calls from a Discord message to the proposal-ready notification.

**[Figure 5 image: From message to proposal-ready notification]**

*Figure 5 — From message to proposal-ready notification (low-level design, sections 3.1 to 3.4)*

### 3.5 Proposal review

- **FR-REV-01** Review states are PENDING, EDITED, ACCEPTED and REJECTED. EDITED is not final. Accepting an edited proposal creates a task from the edited values.
- **FR-REV-02** The reviewer of a proposal is the sender of its primary source message. Only the reviewer (and Admin) can review it. The review screen shows the proposal, its source message with context (sender, group, time, replies), and the provenance of each value.
- **FR-REV-03** The reviewer shall be able to edit any field: description, assignment mode, assignees, people needed, task type (same vertical or cross-vertical), responsible vertical, affected verticals, and attributes. Each edit creates a revision recording who changed what and when. The original generated version stays unchanged.
- **FR-REV-04** Saving an edit shall not accept the proposal. Edit and accept are separate actions, although the app may present them as one flow.
- **FR-REV-05** Accepting shall create exactly one task or announcement. Two simultaneous accepts, or a repeated accept, shall produce one.
- **FR-REV-06** Accepting shall be blocked while any assignee is unresolved or flagged, or a required placeholder is unfilled. For an OPEN task the people needed shall be a positive number or UNLIMITED.
- **FR-REV-07** Rejecting shall keep the proposal, its revisions and the rejection record. A rejected proposal is never regenerated or reconsidered automatically.
- **FR-REV-08** Rejected and edited proposals shall be usable as labelled data for model evaluation. Evaluation data to be kept beyond the retention date shall be exported masked, with the consent of the people concerned, before that date.
- **FR-REV-09** For each proposal the system shall record which fields the reviewer changed.
- **FR-REV-10** An accepted ANNOUNCEMENT shall be shown to its audience (a Head’s vertical, or the sender AH’s volunteers) and shall not create a task.

Figure 6 shows the review states of FR-REV-01.

**[Figure 6 image: Proposal review states]**

*Figure 6 — Proposal review states (low-level design, section 3.5)*

### 3.6 Tasks

- **FR-TSK-01** A task shall have: persistent ID, description, task type, source vertical, responsible vertical, affected verticals, creator, assignment mode, people needed (open tasks), assignee records, and attributes. The creator is the reviewer who accepted the proposal. These are separate fields and are not assumed to be the same person or vertical.
- **FR-TSK-02** Task state shall be held in independent dimensions. No single field shall encode more than one of them.

| Dimension | Applies to | Values |
|---|---|---|
| Assignment mode | Task | EXPLICIT, OPEN |
| Agreement | Each assignee record | AWAITING, ACCEPTED, DENIED |
| Work status | Each assignee record | NOT_STARTED, IN_PROGRESS, COMPLETED |
| Cancellation | Task | ACTIVE, CANCELLED |
| Approval | Task | NOT_REQUIRED, PENDING, APPROVED, DENIED (cross-vertical requests from AHs, section 3.7) |

- **FR-TSK-03** Creating an EXPLICIT task shall create one assignee record per named person, each AWAITING. It shall not be ACCEPTED.
- **FR-TSK-04** An OPEN task shall be visible to the active volunteers of its responsible vertical, who can claim a place. A successful claim creates an assignee record that is ACCEPTED. Claims stop when the number accepted equals people needed. UNLIMITED never fills. If two volunteers claim the last place at once, exactly one succeeds and the other is told the task is full.
- **FR-TSK-05** Agreement is set only by an explicit in-app action by the assignee, or by an authorized user on the assignee’s behalf with that fact recorded. It shall never be inferred from messages, reactions or silence. A denial notifies the creator and frees the place; the creator may reassign.
- **FR-TSK-06** Only the assignee (or Admin) changes their work status: NOT_STARTED → IN_PROGRESS → COMPLETED. COMPLETED is final. The system has no verification step; the creator verifies outside the system.
- **FR-TSK-07** The creator shall see each volunteer’s status on each task, plus a roll-up (for example “3 of 5 completed”).
- **FR-TSK-08** The creator may cancel any task that is not fully completed. Cancelling marks all non-completed assignee records cancelled and notifies the assignees.
- **FR-TSK-09** The source vertical is the vertical of the message’s group. It shall not be changed to match the responsible vertical.
- **FR-TSK-10** Authority comes from role, not authorship. Being the sender of a message does not make anyone the owner of a task unless they hold the creator role by accepting its proposal.
- **FR-TSK-11** Every change to a task shall create an append-only history record: field, old value, new value, actor (user, or model with version), time, reason or evidence, and the actor’s role and vertical at that time. A changed value shall not overwrite history.
- **FR-TSK-12** Concurrent task edits shall not silently overwrite each other. An edit made on a stale version shall be rejected with the current values shown, and both attempts shall appear in the history.
- **FR-TSK-13** The system shall enforce the work-status transitions in FR-TSK-06 and the agreement transitions AWAITING → ACCEPTED or DENIED.
- **FR-TSK-14** An AI component shall never create, assign, reassign, change the status of, complete, correct or merge a task.
- **FR-TSK-15** Tasks shall never be merged because of similar wording, a shared bucket or a shared episode.
- **FR-TSK-16** The creator, a Head of the responsible vertical, and Admin may edit or reassign a task. Assignees are notified of changes.

**[Figure 7 image: Task and assignee record state model]**

*Figure 7 — Task and assignee record state model (low-level design, section 3.6)*

### 3.7 Cross-vertical tasks

- **FR-XV-01** A task is CROSS_VERTICAL when its responsible vertical differs from its source vertical. The reviewer sets this during review (FR-REV-03).
- **FR-XV-02** A Head’s cross-vertical task goes live as soon as it is accepted. The Head of the responsible vertical shall be notified.
- **FR-XV-03** An AH’s cross-vertical task, when accepted, shall be created with Approval = PENDING and shall not be visible to volunteers. A request, with an optional message from the requester, shall be sent to every AH and every Head of the responsible vertical.
- **FR-XV-04** Any one of those people may approve or deny the request, with an optional message. The first decision wins. Later attempts see the decision and change nothing.
- **FR-XV-05** On approval the task goes live to the responsible vertical’s volunteers according to its assignment mode, and the requester is notified. On denial the task never goes live, the requester is notified with the message, and the task and decision are kept.
- **FR-XV-06** Requests, decisions and messages shall be recorded in the task history with the actor’s role and vertical.

Figure 8 shows how an AH’s cross-vertical request is approved or denied (FR-XV-03 to FR-XV-05).

**[Figure 8 image: Cross-vertical approval sequence]**

*Figure 8 — Cross-vertical approval sequence (low-level design, section 3.7)*

### 3.8 Notifications and announcements

- **FR-NOT-01** The system shall send these notifications:

| Event | Recipient |
|---|---|
| Proposal ready | The reviewer (the sender) only |
| Task assigned (EXPLICIT) | Each named assignee |
| Open task available | Eligible volunteers of the responsible vertical |
| Assignee accepted or denied | The creator |
| Volunteer marked their work completed | The creator |
| Task edited, reassigned or cancelled | The affected assignees |
| Cross-vertical request | All AHs and Heads of the responsible vertical |
| Cross-vertical decision | The requester |
| Head’s cross-vertical task created | A Head of the responsible vertical |
| Announcement | Its audience |

- **FR-NOT-02** In the MVP there is one notification per proposal, with no batching. [Batching is Post-MVP, FR-PM-06.]
- **FR-NOT-03** Notification wording shall match the real state. “Assigned to X, awaiting acceptance” is different from “X accepted”.
- **FR-NOT-04** A notification failure shall not roll back or block the change that caused it. The failure shall be recorded and retried.
- **FR-NOT-05** A retry shall not send a duplicate notification for the same event and recipient.
- **FR-NOT-06** Heads shall be able to push announcements and notifications to their vertical. AHs shall be able to push information to the volunteers under them.
- **FR-NOT-07** Notifications shall reach users as push notifications on their phone and appear in an in-app list. A proposal-ready notification shall open the review screen directly.

### 3.9 Roster, identity and authorization

- **FR-ROS-01** The roster (name as used in chat, institute roll number, Discord ID, phone number, role, vertical(s), reports-to line) shall be loaded before the festival.
- **FR-ROS-02** The roster is fixed during the festival. The MVP has no import, sync or edit flows, and only Admin can change roster data.
- **FR-ROS-03** Every sign-in to the app shall resolve to exactly one roster person. The person signs in with their institute roll number, and a one-time password (OTP) is sent as a direct message from the ASEC bot to the Discord ID the roster holds for that roll number, never to an account or number typed at sign-in. A roster person with no Discord ID cannot sign in until the Admin supplies one before the festival (FR-ROS-02). MVP assumption: all roster persons are members of the single festival Discord server and accept DMs from server members.
- **FR-AUT-01** The backend shall authorize every protected operation using server-side identity, role and scope. Hiding a button is not authorization. A direct API call shall get the same result as the app.
- **FR-AUT-02** Protected operations include: proposal review, task creation and edit, assignment, reassignment, cancellation, status change, cross-vertical approval, announcements, viewing system internals, and roster changes.
- **FR-AUT-03** A volunteer shall not assign tasks to others, review proposals or create tasks.
- **FR-AUT-04** The matrix below is the baseline. It shall be approved by the project owner before production, and each cell shall have an automated test.
- **FR-AUT-05** Authorization uses the person’s current role. History keeps the role and vertical in force at the time of each action.
- **FR-AUT-06** Unauthorized attempts shall be rejected and recorded.
- **FR-AUT-07** A person referenced by any record shall not be hard-deleted. Deactivation shall block sign-in and new assignment while keeping attribution. Exception: the retention procedure (DR-07).
- **FR-AUT-08** A message’s content shall never grant authority. Authority comes only from the authorization model.

| Operation | Admin | Head | AH | Volunteer |
|---|---|---|---|---|
| Receive and review own proposals | Override | Yes | Yes | — |
| Edit, accept or reject a proposal | Override | Own proposals | Own proposals | — |
| Create a task in own vertical (by accepting) | Yes | Yes | Yes | — |
| Raise a cross-vertical task | Yes | Directly | By approval | — |
| Approve or deny a cross-vertical request | Yes | Target vertical | Target vertical | — |
| Edit, reassign or cancel a task | Yes | Tasks they created, or whose responsible vertical is theirs | Tasks they created | — |
| Claim an open task | — | — | — | Own vertical |
| Accept or deny an assigned task; update own work status | — | Own assignments | Own assignments | Own assignments |
| See per-volunteer progress | All | Own vertical | Tasks they created, and volunteers under them | Own tasks only |
| Push announcements | — | To own vertical | To volunteers under them | — |
| View processing records, model results, configuration | Yes | — | — | — |
| Change roster data | Yes | — | — | — |

### 3.10 App and dashboards

- **FR-UI-01** Volunteer functions (view, claim, accept or deny, update status) shall work on Android or iOS
- **FR-UI-02** Volunteer dashboard: own tasks, open tasks to claim in their vertical, notifications.
- **FR-UI-03** AH dashboard: review queue, tasks they created with each volunteer’s status, volunteers under them, cross-vertical requests awaiting their decision.
- **FR-UI-04** Head dashboard: the same across the vertical, plus cross-vertical tasks that affect it.
- **FR-UI-05** Admin view: pipeline backlog (count and age of the oldest bucket), processing attempts, model results, configuration.
- **FR-UI-06** The review screen shall show AI-inferred and unverified values as unconfirmed, and placeholders clearly.
- **FR-UI-07** The app contains no authority logic. All decisions are made by the backend.
- **FR-UI-08** [Optional] A web dashboard shall provide the AH, Head and Admin dashboards (FR-UI-03, FR-UI-04, FR-UI-05) in a browser, using the same backend API as the mobile app.

### 3.11 Post-MVP requirements

- **FR-PM-01** Reactions as acceptance. A reaction by an eligible volunteer on a task’s source message shall count as that volunteer’s acceptance: any eligible volunteer for an OPEN task, only the named person for an EXPLICIT task. Reactions made before the task exists are applied when it is created, and later ones while it is open. Details such as whether removing a reaction withdraws acceptance are fixed when this is scheduled.
- **FR-PM-02** Relevant messages from volunteers shall be routed to the volunteer’s AH (from the reports-to line) for review.
- **FR-PM-03** Proposal generation may receive messages from before the current bucket, for example the last few minutes in the group.
- **FR-PM-04** Context processing shall run asynchronously and never block review or task management. It may propose relationships (episode association, dependency, possible duplicate, correction, context association). Relationships are proposals only: they shall never merge tasks, change task identity or modify task fields. Duplicates and corrections need explicit confirmation by an authorized person. Relationships may stay unresolved, and retrieval shall be incremental and bounded.
- **FR-PM-05** Authorized people shall be able to browse irrelevant and unclassified messages and promote one to relevant. The promotion is a separate correction record, makes the message eligible for proposal generation, and that processing is linked to the correction.
- **FR-PM-06** Notification batching for rapid-fire messages.
- **FR-PM-07** Proposals backed by several primary source messages.
- **FR-PM-08** Source edit and delete versions, attachments, and full reprocessing and recovery tooling.
- **FR-PM-09** An FC role for communication between Heads and FCs.
- **FR-PM-10** Voice input through the same ingestion path, and incident and emergency management.

## 4. Data Requirements

### 4.1 Entities

| Entity | Holds |
|---|---|
| Person (roster) | Name, Discord ID, phone, role, vertical memberships, reports-to, active flag, institute roll number (sign-in identifier, FR-ROS-03) |
| Vertical, Group | Organizational units and the group-to-vertical mapping |
| Message | Content, metadata, mentions, flags (LATE, UNMAPPED, UNKNOWN_SENDER, UNCLASSIFIED) |
| Reaction event | Message, reactor, emoji, added or removed, time |
| Bucket | Window bounds, parameters used, member messages, processing state, link to earlier bucket |
| Processing attempt | Stage, input reference, model and configuration version, times, outcome (SUCCEEDED, FAILED_RETRYABLE, FAILED_PERMANENT), error |
| Relevance decision / correction | Immutable model decision; separate human override |
| Proposal / revision | Candidate, generation run, validation status, review state; one immutable revision per edit |
| Task / announcement | Accepted record with the state dimensions of FR-TSK-02 |
| Assignee record | Person, agreement, work status, timestamps |
| Approval request | Cross-vertical request, recipients, decision, messages |
| History record | Append-only change log |
| Notification | Event, recipient, delivery state |
| Episode, relationship | [Post-MVP] |

Figure 9 shows the main records and how they relate.

**[Figure 9 image: Data model]**

*Figure 9 — Data model (low-level design, section 4)*

### 4.2 Integrity rules

- **DR-01** Records shall be connected by enforced references, so a task traces back to its proposal, generating attempt, bucket and source messages, and forward again. No orphans.
- **DR-02** Deleting or deactivating a person, group or vertical shall not delete or detach records that reference it. Exception: the retention procedure (DR-07).
- **DR-03** Derived data (classifications, proposals, task fields) never replaces source content.
- **DR-04** Unknown, missing, empty and explicitly negative values shall be distinguishable.
- **DR-05** A record shall not show as processed, accepted or confirmed unless the operation completed.
- **DR-06** Any index or store derived from the data shall hold references to authoritative records and be rebuildable from them.
- **DR-07** Source messages (content, raw metadata, mentions and reactions), the masked AI input copies, AI outputs, processing and audit logs, proposals, tasks, announcements, notifications, history and roster contact details (phone number, Discord ID, roll number) shall be retained through the festival and for 3 months after the date the Admin marks the festival as ended. After that date, a retention procedure run by the Admin shall delete them, including all backups and log files, and record that it ran.

## 5. External Interfaces

- **EIR-01** Discord. The adapter delivers normalized events with source IDs and group boundaries preserved, and tolerates duplicate, late and out-of-order delivery without loss. It captures messages, mentions and reaction events.
- **EIR-02** App and backend. The app talks only to the backend API. Every rule in this SRS is enforced there.
- **EIR-03** Backend and AI layer. Calls are stateless request and response.
  - Relevance: one bucket in (messages with sender name, time, reply links, mention handles, group and vertical), a list of {message ID, decision} out.
  - Proposal: the relevant message(s) from eligible senders plus bucket context in, a list of proposals out in the shape of FR-PRP-07.
  - Mentions are passed as opaque handles, and the backend maps them back to people.
  - Phone numbers, email addresses and government-ID-like numbers are masked before every call (NFR-SEC-04).
  - Outputs are schema-checked before use. A malformed or incomplete output is an execution failure, never a valid empty result.
  - Every response carries the model and version and the prompt or configuration version.
  - The AI layer never writes to the database.
- **EIR-04** Notification delivery. Push notifications to phones through Firebase Cloud Messaging. Sign-in OTPs are delivered as direct messages from the ASEC bot through the Discord interface (EIR-01, FR-ROS-03).
- **EIR-05** Authorization. Every interface enforces the same rules.

Figure 10 shows one AI call, from masking to retry or permanent failure.

**[Figure 10 image: AI layer call contract and failure handling]**

*Figure 10 — AI layer call contract and failure handling (low-level design, EIR-03)*

## 6. Non-Functional Requirements

### 6.1 Reliability

- **NFR-REL-01** Pipeline stages shall be recorded processing attempts. Each stage shall be safe to retry without creating duplicate decisions, proposals, tasks or notifications.
- **NFR-REL-02** Only execution failures are retried automatically. A model result that a human later judges wrong is a quality error, not a failure, and is not retried.
- **NFR-REL-03** A failed stage shall not erase earlier stage results. A permanently failed attempt shall stay recoverable by manual re-queue, which creates a new attempt.
- **NFR-REL-04** A worker crash shall be recoverable without loss or duplicates.
- **NFR-REL-05** Task management shall keep working while any AI component is down. Messages received during an outage shall be processed after recovery, without manual re-entry, and none shall be marked irrelevant.
- **NFR-REL-06** A backlog of unprocessed buckets shall be visible (count and age of the oldest). Overload shall show as delay or backlog, never as silent loss.
- **NFR-REL-07** If a backup model is used, it runs only after the primary fails. Both attempts are recorded, and the effective result is the successful one.

### 6.2 Traceability and audit

- **NFR-AUD-01** For any task the system shall answer: source message, generating attempt and model, original proposed values, reviewer edits, who accepted, creator and assignees, and later changes with their evidence.
- **NFR-AUD-02** Every AI output shall record model and version, prompt or configuration version, and enough input reference to reconstruct what the model saw (the masked input). Identical output on replay is not required.
- **NFR-AUD-03** History and processing records shall not be editable through any application interface.

### 6.3 Concurrency

- **NFR-CON-01** Concurrent ingestion shall not lose, duplicate or mis-assign a message.
- **NFR-CON-02** Simultaneous accepts, claims and assignments shall each have exactly one winner.

### 6.4 Performance and capacity

| Measure | Target |
|---|---|
| Planned load | 12 groups, with headroom for 15 |
| Sustained ingest | 60 messages per minute across all groups |
| Burst ingest | 120 messages per minute |
| Message to proposal-ready notification, P95 | ≤ 30 s, excluding review time |
| Relevance classification, P95 per bucket | ≤ 1.5 s |
| Proposal generation, P95 per bucket | ≤8s |
| Availability during festival days | ≥ 99% |

- **NFR-PERF-01** The targets above are initial and shall be confirmed by a benchmark on live data.
- **NFR-PERF-02** The system shall record ingestion delay, bucket delay, classifier latency, proposal latency and review waiting time, as P50, P95 and P99.
- **NFR-PERF-03** Load tests shall cover many groups, bursts, a high share of irrelevant messages, and a run where most buckets are relevant, to find whether proposal generation is the bottleneck.

### 6.5 Security and privacy

- **NFR-SEC-01** All access requires authentication. Discord, model and push credentials are held in secure configuration and never exposed to users or logged in errors.
- **NFR-SEC-02** Source evidence, processing records and audit history shall be protected from unauthorized access or change.
- **NFR-SEC-03** Phone numbers and message content are visible to a person only for people below them in the roster hierarchy. Admin sees everyone, a Head sees everyone in their vertical, and an AH sees the volunteers who report to them. A volunteer sees only their own messages and tasks. Exceptions:
  - (a) a task’s creator sees the contact details of its assignees,
  - (b) the review screen shows the reviewer the other messages in the proposal’s bucket, and
  - (c) an approval request (FR-XV-03) shows the approver the task description and the requester’s note, not the source chat.
- **NFR-SEC-04** Messages contain personal data such as names, phone numbers and ID details. Before any AI call, phone numbers, email addresses and government-ID-like numbers shall be masked. The originals stay in the backend only.
- **NFR-SEC-05** The AI models are self-hosted, on Google Colab or a local machine for the small MVP run. Colab is Google-operated infrastructure, so text sent there is masked (NFR-SEC-04) exactly as for any hosted service.
- **NFR-SEC-06** All communication between the app and the backend shall use HTTPS. An OTP shall expire after 5 minutes and allow at most 5 attempts. OTP requests and sign-in attempts shall be rate limited per person and per source address. A signed-in session shall expire 7 days after sign-in. Deactivating a person (FR-AUT-07) shall revoke their sessions immediately. OTP codes shall never be written to logs.

### 6.6 Maintainability

- **NFR-MNT-01** Bucket parameters, thresholds, model IDs and prompt versions shall be configuration.
- **NFR-MNT-02** Adding a source shall need only a new adapter.
- **NFR-MNT-03** Replacing a model shall not change the data model or the meaning of tasks, proposals, provenance or authority.
- **NFR-MNT-04** Bucketing, authorization, state changes and idempotency shall be testable without any AI model.

## 7. AI Requirements

- **AI-01** AI is used only for relevance classification and proposal generation (and, Post-MVP, context analysis). These stay logically separate.
- **AI-02** AI output is advisory. It shall never be written directly to authoritative state.
- **AI-03** Human edits shall be stored separately from the model’s original values.
- **AI-04** Each AI function is evaluated separately, then the whole pipeline end to end.
- **AI-05** A representative labelled evaluation set shall exist before production, built from real festival messages in Hinglish and romanized Hindi, and including the sender’s role. The sample group chat is the starting corpus. Evaluation data to be kept beyond the retention date shall be exported masked, with the consent of the people concerned, before that date.
  - The relevance set shall include hard negatives: long irrelevant messages, jokes about tasks, past-tense or status reports (“only 3 people have come”), banter, and chatter that mentions work without requesting it.
  - The proposal set shall cover: explicit assignment to one person and to several, an open call with and without a number, a same-vertical task, a cross-vertical task, an announcement, a message with two tasks, an assignment followed by an ambiguous reply or reaction, and a name list that depends on the preceding message.
- **AI-06** Candidate models shall be compared on the same data, recording accuracy on Hinglish, latency, throughput, memory, cost and data handling. The smallest model that meets the targets is preferred. The MVP run uses self-hosted models on Google Colab or a local machine, so candidates shall be small enough to run on one GPU of that kind, quantized if needed. Hosted models stay in the comparison as a reference only.
- **AI-07** The system shall track the reviewer correction rate per proposal across model versions.
- **AI-08** End-to-end evaluation shall compare the actionable items the system finds with those a person finds by reading the same period. It shall also report review burden: proposals per reviewer per day and the share rejected.
- **AI-09** Measured quality shall be reported with a 95% confidence interval. The lower bound shall meet the target, or the owner shall accept the interval in writing.

Targets (latency targets are in section 6.4)

| Measure | Target |
|---|---|
| Relevance recall (false-negative rate ≤ 5%) | ≥ 95% |
| Relevance precision | ≥ 80% |
| Proposal type, assignment mode | ≥ 95% each |
| Assignee, task type | ≥ 90% each |
| Proposal acceptance overall | ≥ 80% |
| Description quality | Human-rated, no percentage |

## 8. Business Rules

- **BR-01** A message is evidence, not an authorized decision.
- **BR-02** Message author, source group, source vertical, responsible vertical, affected vertical and decision-maker are different things.
- **BR-03** A proposed assignee is not an agreeing assignee.
- **BR-04** Accepting a proposal does not verify its fields. Status does not imply confirmation.
- **BR-05** A bucket is not a task or a conversation.
- **BR-06** A later message can change understanding but never erases earlier evidence.
- **BR-07** Execution failure is not a negative result.
- **BR-08** Evidence that conflicts is preserved until an authorized decision resolves it.
- **BR-09** In the MVP a reaction is evidence, never acceptance.
- **BR-10** Only the reviewer, or Admin, can turn a proposal into a task.

## 9. Constraints

- Bucketing occurs before relevance classification.
- Relevance is decided per message.
- Human review is the boundary between a proposal and a task.
- Authoritative state lives in a relational store with enforced references.
- No database, queue, framework, model or language is mandated, and no model needs training from scratch.
- The roster is fixed during the festival.
- Voice, if added later, shall enter through the same ingestion and pipeline, not a separate task model.

## 10. Verification and Acceptance

### 10.1 Bucket test cases

Parameters: W = 15, G = 5, M = 30, C = 2. Times are in seconds.

| Message times | Expected |
|---|---|
| 0, 3, 8 | One bucket |
| 0, 10 | One bucket (10 < W) |
| 0, 15 | Two buckets (exactly W, gap not under G) |
| 0, 12 | One bucket |
| 0, 17 | Two buckets |
| 0, 4, 8, 12, 16 | One bucket (16 joins on the burst rule) |
| 0, 12, 17 | Two buckets (gap exactly G, outside the base window) |
| 0, 4, 8, 12, 16, 20, 24, 28, 30 | The message at 30 starts a new bucket |
| Every 4 s from 0 to 60 | Bucket 1 holds 0 to 28; bucket 2 holds 32 to 60 |
| One message at 0 | Closes at 17 (end 15, plus grace 2) |
| 0, 4, 8, 12, 16 | Closes at 23 (end 21, plus grace 2) |
| Two groups interleaved | Two independent buckets |
| Same set in any arrival order within grace | Identical buckets |
| Duplicate delivery | One message |
| Message after closure | Stored, new bucket, earlier bucket unchanged |

### 10.2 Acceptance scenarios

| ID | Scenario | Required result |
|---|---|---|
| AC-01 | Bucket with relevant and irrelevant messages | Independent decision per message; bucket not treated as a task. |
| AC-02 | One message with two tasks | Two independent proposals; one failing validation leaves the other. |
| AC-03 | Relevant message, no task | Zero proposals; not a failure. |
| AC-04 | Reject | Proposal retained; source untouched; no task. |
| AC-05 | Edit without accepting | Edited value kept apart from the model value; proposal not accepted. |
| AC-06 | Accept with AI-inferred values the reviewer did not change | Task created; those values stay marked as unconfirmed. |
| AC-07 | An AH posts “Arjun, arrange the generator” | EXPLICIT proposal; Arjun resolved within the AH’s vertical; only the AH is notified; after acceptance Arjun’s record is AWAITING until he acts in the app. |
| AC-08 | An AH posts “need 5 volunteers at the gate” | OPEN proposal with people needed 5; after acceptance the vertical’s volunteers see it; five claims succeed and a sixth is told the task is full; two simultaneous claims for the last place give exactly one winner. |
| AC-09 | A name, then “Rahul?”, a reaction, then “I’ll do it” from another person | All evidence kept; no acceptance inferred; Rahul’s agreement stays AWAITING until Rahul acts in the app. |
| AC-10 | Relevant message from a volunteer or unknown sender | Stored and classified; no proposal and no notification. |
| AC-11 | Name resolution | A mention resolves exactly; one name match in the source vertical resolves; several matches stay unresolved; no match is flagged; accept is blocked until fixed. |
| AC-12 | Classifier or proposal failure | Marked as failure, not irrelevant; source kept; retry creates no duplicates. |
| AC-13 | Classifier outage | Messages UNCLASSIFIED, processed after recovery, none lost. |
| AC-14 | A Head raises a cross-vertical task | Source vertical unchanged; responsible vertical set; task goes live; the responsible Head is notified. |
| AC-15 | An AH requests a cross-vertical task | Task PENDING and hidden; AHs and Head of the target vertical notified; first decision wins, with its message; approval makes it live, denial keeps it closed and tells the requester. |
| AC-16 | A volunteer completes a task | The creator is notified and sees which volunteer completed which task; completion is final. |
| AC-17 | Notification fails | Task change committed; failure recorded; retried without duplicates. |
| AC-18 | Two accepts at once | One task. |
| AC-19 | Stale edit | Rejected with current values shown; both attempts logged. |
| AC-20 | A volunteer calls the assign API directly | Rejected and logged. |
| AC-21 | Message containing phone numbers | Numbers masked in every AI input; original stored unchanged. |
| AC-22 | A reaction on a task’s source message | Stored as evidence; no change to agreement, status or assignment. |

Post-MVP scenarios: reaction as acceptance (FR-PM-01), promoting an irrelevant message (FR-PM-05), routing a volunteer message to their AH (FR-PM-02), possible duplicate tasks, and a later message that corrects an earlier one (FR-PM-04).

### 10.3 MVP acceptance

The MVP is accepted when:

- Every mandatory MVP requirement is verified.
- All MVP scenarios above pass.
- The permission matrix is approved and tested for each role.
- Retention rules are approved.
- Idempotency and recovery are tested.
- Model and configuration versions are recorded for every AI output, and masking is verified.
- A latency benchmark is recorded.
- Evaluation on real festival data is completed and recorded.
- The AI targets in section 7 are met.

## Appendix A — Candidate models (informative, under consideration, not mandated)

To be compared as AI-06 requires. No model is selected. Candidates that cannot run on one GPU of the MVP host are reference points only.

- Hosted: Claude Haiku 5.5 for relevance and Claude Sonnet 5.5 for proposals, and a comparable pair from another provider.
- Self-hosted: Sarvam-30B or Sarvam-105B, Qwen3.6-35B-A3B, and Nanda-10B (relevance).
- The choice depends on measured Hinglish accuracy, the hardware available, and approval of data handling.