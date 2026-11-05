# ASEC — Decision Log (owner decisions delegated to the assistant)

Put this file at `docs/DECISIONS.md`. Dated 25 Oct 2026. The owner delegated these decisions to the assistant, who decided with the whole project in mind: smallest risk now, no blocking of later phases, SRS followed unless it contradicts itself. The owner can overturn any line by editing it; coding agents must treat each line as approved unless the owner says otherwise. Nothing here edits the SRS; where the SRS wording must change, that is listed at the end for the owner.

## A. Phase 6 decisions (approved for implementation)

| ID | Decision | Why |
| --- | --- | --- |
| OD-REV-1 | Accept materializes the task or announcement in the same transaction (task row, assignee rows, history row, attributes, affected verticals; or the announcement row). Phase 7 only adds the volunteer lifecycle. | FR-REV-05 requires exactly one entity; leaving an ACCEPTED proposal without one is an illegal state. |
| OD-REV-2 | Name resolution stays inside the **source vertical** (Phase 5 DC-6 unchanged). The reviewer fixes cross-vertical assignees by direct roster selection (`MANUAL` route). | Matches FR-PRP-10 and the pinned tests; the manual route removes the practical problem without changing a pinned rule. |
| OD-REV-3 | Revision-keyed assignee storage: new table `revision_assignee` in migration 0015 (routes MENTION, NAME_MATCH, MANUAL). Original `proposal_assignee` rows are never changed or deleted. | FR-REV-03 and Figure 6: the original generated version stays unchanged; evaluation data (FR-REV-08) needs both. |
| OD-REV-4 | `ptype` (task vs announcement) is **immutable** during review. To change it, reject and repost. | A task and an announcement have different shapes and audiences; the SRS field list omits it. Low cost, prevents odd states. |
| OD-REV-5 | Announcement audience uses provisional scope tokens `VERTICAL` (sender is a Head) and `AH_VOLUNTEERS` (sender is an AH), with `audience_vertical_id` = source vertical. The AH is derived from the proposal's primary message sender. Structured columns come with PB-6. | Keeps the Head/AH distinction FR-REV-10 requires without inventing identifiers inside free text. |
| OD-REV-6 | `task.creator_id` = the primary message sender (reviewer of record). An Admin accepting on their behalf is recorded as the actor in `task_history` and `proposal_decision`. | If the Admin became creator, the AH would lose notifications and management of their own task. A deliberate reading of FR-TSK-01; SRS wording to be clarified. |
| OD-REV-7 | For an AH-raised cross-vertical task, the accept transaction creates the `approval_request` and recipient snapshot; the decision logic stays in Phase 8. An empty recipient set blocks the accept. | FR-XV-03; avoids a hidden task that can never be approved. |
| Misc | AH-raised versus Head-raised follows the **sender's** role, not the accepting Admin's. An edit that changes nothing is rejected. A cleared description is stored as NULL. `accept_proposal` also checks `expected_revision_number`. Admin token is `Admin`, configurable and case-insensitive. Isolate the "EXPLICIT stores people_needed = 1" mapping in one helper so PB-3 can change it in one place. | Consistency and safety. |

## B. Open PB items, decided so later phases are not blocked (implement in the order of section C)

| ID | Decision | Why |
| --- | --- | --- |
| PB-1 | Adopt the **clock-driven settle-then-assign** design: per-group clock (live = receipt time, replay = replay time); a message is settled when `clock ≥ t + C`; assign only settled messages sorted by (event time, source message ID); close when `clock ≥ e + C`; late only if the bucket it would have joined has already closed. Add the scheduler that advances the clock. | The current code breaks FR-BKT-06 and never closes buckets in a live run. This is the only design that satisfies both. |
| PB-2 | Bucket time = valid event time, else receipt time with a stored marker; stored event time is never overwritten. Replay importer takes an explicit time zone (default Asia/Kolkata for the WhatsApp sample). A naive timestamp from live Discord is a recorded error, not a silent skip. | FR-BKT-01 (every message in exactly one bucket) and no silent loss. |
| PB-3 | Rebuild the task tables once: EXPLICIT tasks store `people_needed` NULL; `task_assignee` gets a surrogate id and a **partial unique index on active rows** (not denied, not cancelled) so a person can be re-added after a denial; keep the separate `cancelled` flag on assignee records; INTERNAL tasks store `responsible_vertical_id` = source vertical (NOT NULL). | Removes four real modelling errors before Phase 7 builds on them. |
| PB-4 | **Keep SQLite** for the MVP (WAL, `BEGIN IMMEDIATE`, one backend host). Revisit PostgreSQL only if workers move to separate hosts or sustained writes exceed what one host handles. Update the SRS figure note. | The SRS mandates no database, load is 60–120 msg/min, and PostgreSQL adds operations work on a laptop or Colab-adjacent deployment. |
| PB-5 | Role vocabulary: `Admin`, `Head`, `AH`, `Volunteer` with a CHECK; `reports_to` becomes a foreign key; `phone` and `roll_number` NOT NULL; one role per person for the MVP (the roster in the SRS has a single role). Do this in the same rebuild window as PB-3, before any roster load. | The roster is not loaded yet, so this is cheap now and costly later. |
| PB-6 | Structured announcement audience: `audience_type` in (`VERTICAL`, `AH_VOLUNTEERS`) plus nullable `audience_ah_person_id`. Phase 6's provisional tokens map one-to-one. Do before notification fan-out. | Needed for FR-NOT audience delivery. |
| PB-7 | Limit `predecessor_bucket_id` to late buckets (the SRS only asks for that link) and drop the fallback chain. Do inside the PB-1 work. | Avoids buckets being read as conversations. |
| Relevance retrofit | **Approved**: relevance requests get opaque handles and the same markup rewriting as Phase 5; no real Discord ID leaves the backend. Do before any live data or hosted model. | EIR-03 and NFR-SEC-04; consistency between the two AI stages. |
| Eligibility across verticals | Default **ON**: an AH or Head's message produces a proposal only if the sender belongs to the group's vertical. Heads-and-AHs-only groups stay unmapped until a design for them exists. | AH authority is limited to their own vertical. |
| Retention and training | Default: **archive only** (superseded by DR-07); do not use festival chat for model training until a consent decision exists. This one is the owner's to change. | Privacy and consent; the SRS says "archived", not "training". |

## C. Order of work

1. Phase 6 (review service) with 0015.
2. Small slices: relevance retrofit and eligibility default.
3. One schema window: PB-3, PB-5 and PB-6 (rebuild migrations, before the roster is loaded).
4. Phase 7 (tasks), Phase 8 (cross-vertical).
5. PB-1, PB-2, PB-7 with the clock scheduler, then the Discord adapter.
6. Phase 9 (notification delivery), Phase 10 (API, dashboards, evaluation and benchmark).

## D. SRS wording for the owner to fix (nothing here is changed by agents)

"Section 12" reference that does not exist · roll number "fallback" vs primary credential · assignee "cancelled" value · who may cancel a task (FR-TSK-08 vs FR-TSK-16/matrix) · Admin status rights · the ≤ 30 s target (bucket wait alone is 17–32 s) · invalid-proposal handling (FR-PRP-05 vs EIR-03) · task type vs responsible vertical · name matching only in the source vertical · the clock for "closed when e + C has passed" · sender role missing from the AI input · availability ≥ 99% vs Colab best effort · candidate models too large for one Colab-class GPU · "archived" vs "transfer for training" · FR-TSK-01 "creator is the reviewer who accepted" (OD-REV-6 reading) · bucket state for a two-stage pipeline · announcement audience.