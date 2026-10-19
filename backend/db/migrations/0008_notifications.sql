-- 0008_notifications.sql (Phase 2, Slice 2.9)
--
-- Notification storage (SRS 3.8, §4.1 Notification: "Event, recipient,
-- delivery state"). One notification row per event per recipient
-- (FR-NOT-02: one notification per proposal, no batching).
--
-- Each notification points at exactly one existing record — proposal, task,
-- approval request or announcement — via a single NOT NULL foreign key
-- (CHECK sum = 1), keeping every notification connected to its source chain
-- (DR-01). The FR-NOT-01 table lists events in prose; event_type therefore
-- carries the service's event label as free text (no invented token
-- vocabulary; recording only).
--
-- Deduplication (FR-NOT-05 "A retry shall not send a duplicate notification
-- for the same event and recipient", NFR-REL-01): dedupe_key is UNIQUE and
-- the service composes it from the event, recipient and subject, so a retry
-- inserts nothing instead of duplicating.
--
-- Attempt tracking (FR-NOT-04 "A notification failure shall not roll back or
-- block the change that caused it. The failure shall be recorded and
-- retried", NFR-REL-02, AC-17): notification_attempt rows are append-only
-- attempt records ordered by attempt_number (UNIQUE per notification). A
-- failed attempt's outcome and error text live only in attempt rows — the
-- committed change behind the notification is never touched, so recording a
-- failure cannot undo it. delivery_state (§4.1) carries the service's state
-- word; the SRS defines no token set, so no vocabulary CHECK is imposed.
--
-- Delivery itself (workers, push channels, FR-NOT-06 push capability,
-- FR-NOT-07 phone push/in-app list surfaces, FR-SEC credential handling) is
-- out of Phase 2: no delivery integration is implemented here. Foreign keys
-- RESTRICT everywhere per DR-01/DR-02.

CREATE TABLE notification (
    id                  INTEGER PRIMARY KEY,
    event_type          TEXT NOT NULL,
    recipient_person_id INTEGER NOT NULL,
    dedupe_key          TEXT NOT NULL UNIQUE,
    proposal_id         INTEGER,
    task_id             INTEGER,
    approval_request_id INTEGER,
    announcement_id     INTEGER,
    delivery_state      TEXT NOT NULL DEFAULT 'PENDING',
    created_at          TEXT NOT NULL,
    CHECK (
        (proposal_id IS NOT NULL) + (task_id IS NOT NULL)
        + (approval_request_id IS NOT NULL) + (announcement_id IS NOT NULL) = 1
    ),
    CHECK (length(trim(event_type)) > 0),
    CHECK (length(trim(dedupe_key)) > 0),
    CHECK (length(trim(delivery_state)) > 0),
    CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (recipient_person_id) REFERENCES person (id) ON DELETE RESTRICT,
    FOREIGN KEY (proposal_id) REFERENCES proposal (id) ON DELETE RESTRICT,
    FOREIGN KEY (task_id) REFERENCES task (id) ON DELETE RESTRICT,
    FOREIGN KEY (approval_request_id) REFERENCES approval_request (id) ON DELETE RESTRICT,
    FOREIGN KEY (announcement_id) REFERENCES announcement (id) ON DELETE RESTRICT
);

CREATE TABLE notification_attempt (
    id              INTEGER PRIMARY KEY,
    notification_id INTEGER NOT NULL,
    attempt_number  INTEGER NOT NULL,
    outcome         TEXT NOT NULL,
    error           TEXT,
    recorded_at     TEXT NOT NULL,
    CHECK (attempt_number >= 1),
    CHECK (length(trim(outcome)) > 0),
    CHECK (error IS NULL OR length(trim(error)) > 0),
    CHECK (length(trim(recorded_at)) > 0),
    UNIQUE (notification_id, attempt_number),
    FOREIGN KEY (notification_id)
        REFERENCES notification (id) ON DELETE RESTRICT
);
