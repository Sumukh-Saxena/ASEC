-- 0007_cross_vertical_approvals.sql (Phase 2, Slice 2.8)
--
-- Cross-vertical approval flow storage (SRS 3.7, FR-XV-01..06; §4.1 Approval
-- request). An approval request is keyed to its task: UNIQUE (task_id) — one
-- request per task (FR-XV-03: "A request ... shall be sent to every AH and
-- the Head of the responsible vertical"). A Head's own cross-vertical task
-- needs no request (FR-XV-02, AC-14); the request table only holds the
-- FR-XV-03 AH flow, where the accepted task starts with Approval = PENDING.
--
-- approval_recipient stores the persons the request was sent to at request
-- time — a snapshot of "every AH and the Head of the responsible vertical"
-- (FR-XV-03); eligibility by role/vertical-at-that-time is roster-state and
-- therefore service-level (FR-ROS-01). Recipient rows are not a delivery
-- ledger (FR-NOT-01 cross-vertical request notification is 2.9, 0008).
--
-- approval_decision is the single decision record: UNIQUE (request_id) is
-- the schema half of first-decision-wins ("The first decision wins. Later
-- attempts see the decision and change nothing", FR-XV-04, AC-15). The
-- vocabulary APPROVED|DENIED mirrors the FR-TSK-02 Approval dimension and
-- FR-TSK-13's transition language. The optional decision message
-- (FR-XV-04) and optional requester message (FR-XV-03) are stored verbatim;
-- optional-but-never-blank. Reporting the decision to the requester with its
-- message (FR-XV-05) and recording requests/decisions/messages in the task
-- history with actor role and vertical (FR-XV-06) are service writes into
-- the 0006 task_history table.
--
-- Disjointness by design: approval decisions are FR-TSK-02 Approval state —
-- they must never be conflated with assignee agreement (FR-TSK-13) or work
-- status (FR-TSK-06), which live on task_assignee rows.
--
-- The schema cannot enforce that decided_by was a recipient at decision
-- time (roster evolution, FR-XV-04 "any one of those people") — service
-- guarantees; the storage keeps rows independently, per DR-01/DR-02 with
-- RESTRICT foreign keys.

CREATE TABLE approval_request (
    id              INTEGER PRIMARY KEY,
    task_id         INTEGER NOT NULL UNIQUE,
    requested_by    INTEGER NOT NULL,
    request_message TEXT,
    created_at      TEXT NOT NULL,
    CHECK (request_message IS NULL OR length(trim(request_message)) > 0),
    CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (task_id) REFERENCES task (id) ON DELETE RESTRICT,
    FOREIGN KEY (requested_by) REFERENCES person (id) ON DELETE RESTRICT
);

CREATE TABLE approval_recipient (
    request_id INTEGER NOT NULL,
    person_id  INTEGER NOT NULL,
    UNIQUE (request_id, person_id),
    FOREIGN KEY (request_id) REFERENCES approval_request (id) ON DELETE RESTRICT,
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT
);

CREATE TABLE approval_decision (
    id               INTEGER PRIMARY KEY,
    request_id       INTEGER NOT NULL UNIQUE,
    decided_by       INTEGER NOT NULL,
    decision         TEXT NOT NULL,
    decision_message TEXT,
    decided_at       TEXT NOT NULL,
    CHECK (decision IN ('APPROVED', 'DENIED')),
    CHECK (decision_message IS NULL OR length(trim(decision_message)) > 0),
    CHECK (length(trim(decided_at)) > 0),
    FOREIGN KEY (request_id) REFERENCES approval_request (id) ON DELETE RESTRICT,
    FOREIGN KEY (decided_by) REFERENCES person (id) ON DELETE RESTRICT
);

