-- 0006_tasks.sql (Phase 2, Slice 2.7)
--
-- Task storage per FR-TSK-01 (SRS 3.6), §4.1 Task/Assignee record/History
-- record. Every accepted TASK proposal (FR-PRP-06, 0005_proposals.sql) has at
-- most one task row: UNIQUE (task.proposal_id) is the schema side of the
-- FR-REV-05/AC-18 single-object rule (the task/announcement XOR is completed
-- by `UNIQUE (announcement.proposal_id)` in 0009 and the service layer).
--
-- FR-TSK-02 state dimensions stay independent fields: assignment mode /
-- cancellation / approval state on the task; agreement / work status /
-- cancelled flag on each assignee record. No single field encodes more than
-- one dimension.
--
-- Vocabulary CHECKs use the SRS tokens verbatim (FR-PRP-06/FR-TSK-02).
-- The PEOPLE-NEEDED encoding rides on the owner-approved proposal encoding
-- (0005): people_needed IS NULL **iff** UNLIMITED (people_needed_unlimited
-- = 1); a positive integer is a specific count, and people_needed_unlimited
-- is 0 in that case. EXPLICIT tasks always have people_needed = 1
-- (single effective slot; FR-TSK-03 assignee-record count is service-level).
--
-- Task type is consistent with the verticals (FR-XV-01: CROSS_VERTICAL iff a
-- responsible vertical exists and differs from the source vertical; INTERNAL
-- tasks have no responsible value, FC-16, or keep the source vertical).
--
-- Assignee records (FR-TSK-03/FR-TSK-05, §4.1 "person, agreement, work
-- status, timestamps"): agreement defaults to AWAITING and is never ACCEPTED
-- at creation; work status defaults to NOT_STARTED. agreed_at records the
-- agreement decision time and exists iff agreement != AWAITING. Removal of an
-- assignee frees the place (FR-TSK-05); the creator can reassign.
--
-- task_history is the append-only task change log (FR-TSK-11, NFR-AUD-01,
-- §4.1 History record): field, old value, new value, time, reason, and the
-- actor's role and vertical. The actor is either a person or a model with
-- version (FR-TSK-11 wording); FR-TSK-14 (AI never changes tasks) and
-- history immutability via application interfaces (NFR-AUD-03) are
-- service-level.
--
-- Not schema-enforceable under the project's no-trigger storage pattern:
-- work-status transition legality and COMPLETED-final (FR-TSK-06/13 —
-- service-level; the schema stores the current dimension values plus history
-- rows for each recorded change), claim capacity against people needed
-- (FR-TSK-04, AC-08, NFR-CON-02), stale-version rejection (FR-TSK-12 — the
-- version column is what the service compares), and authorization of changes
-- (FR-TSK-16). FKs follow DR-01/DR-02/DR-04 (RESTRICT everywhere; no person
-- or roster deletion detaches task records).

CREATE TABLE task (
    id                      INTEGER PRIMARY KEY,
    proposal_id             INTEGER NOT NULL UNIQUE,
    task_type               TEXT NOT NULL,
    source_vertical_id      INTEGER NOT NULL,
    responsible_vertical_id INTEGER,
    creator_id              INTEGER NOT NULL,
    description             TEXT NOT NULL,
    assignment_mode         TEXT NOT NULL,
    people_needed           INTEGER,
    people_needed_unlimited INTEGER NOT NULL DEFAULT 0,
    approval_state          TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
    cancellation            TEXT NOT NULL DEFAULT 'ACTIVE',
    version                 INTEGER NOT NULL DEFAULT 1,
    created_at              TEXT NOT NULL,
    CHECK (task_type IN ('INTERNAL', 'CROSS_VERTICAL')),
    CHECK (assignment_mode IN ('EXPLICIT', 'OPEN')),
    CHECK (approval_state IN ('NOT_REQUIRED', 'PENDING', 'APPROVED', 'DENIED')),
    CHECK (cancellation IN ('ACTIVE', 'CANCELLED')),
    CHECK (
        (task_type = 'INTERNAL'
         AND (responsible_vertical_id IS NULL OR responsible_vertical_id = source_vertical_id))
        OR
        (task_type = 'CROSS_VERTICAL'
         AND responsible_vertical_id IS NOT NULL
         AND responsible_vertical_id != source_vertical_id)
    ),
    CHECK (people_needed IS NULL OR people_needed > 0),
    CHECK (people_needed_unlimited IN (0, 1)),
    CHECK (people_needed IS NULL OR people_needed_unlimited = 0),
    CHECK (people_needed IS NOT NULL OR people_needed_unlimited = 1),
    CHECK (assignment_mode != 'EXPLICIT' OR (people_needed = 1 AND people_needed_unlimited = 0)),
    CHECK (version >= 1),
    CHECK (length(trim(description)) > 0),
    CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (proposal_id) REFERENCES proposal (id) ON DELETE RESTRICT,
    FOREIGN KEY (source_vertical_id) REFERENCES vertical (id) ON DELETE RESTRICT,
    FOREIGN KEY (responsible_vertical_id)
        REFERENCES vertical (id) ON DELETE RESTRICT,
    FOREIGN KEY (creator_id) REFERENCES person (id) ON DELETE RESTRICT
);

CREATE TABLE task_assignee (
    task_id        INTEGER NOT NULL,
    person_id      INTEGER NOT NULL,
    agreement      TEXT NOT NULL DEFAULT 'AWAITING',
    agreed_at      TEXT,
    work_status    TEXT NOT NULL DEFAULT 'NOT_STARTED',
    cancelled      INTEGER NOT NULL DEFAULT 0,
    added_at       TEXT NOT NULL,
    CHECK (agreement IN ('AWAITING', 'ACCEPTED', 'DENIED')),
    CHECK (work_status IN ('NOT_STARTED', 'IN_PROGRESS', 'COMPLETED')),
    CHECK (cancelled IN (0, 1)),
    CHECK (agreement = 'AWAITING' OR (agreed_at IS NOT NULL AND length(trim(agreed_at)) > 0)),
    CHECK (agreement != 'AWAITING' OR agreed_at IS NULL),
    CHECK (length(trim(added_at)) > 0),
    UNIQUE (task_id, person_id),
    FOREIGN KEY (task_id) REFERENCES task (id) ON DELETE RESTRICT,
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT
);

CREATE TABLE task_history (
    id                 INTEGER PRIMARY KEY,
    task_id            INTEGER NOT NULL,
    field_name         TEXT NOT NULL,
    old_value          TEXT,
    new_value          TEXT,
    changed_at         TEXT NOT NULL,
    actor_person_id    INTEGER,
    actor_model_id     TEXT,
    actor_model_version TEXT,
    actor_role         TEXT NOT NULL,
    actor_vertical_id  INTEGER,
    reason             TEXT NOT NULL,
    CHECK (
        (actor_person_id IS NOT NULL
         AND actor_model_id IS NULL
         AND actor_model_version IS NULL)
        OR
        (actor_person_id IS NULL
         AND actor_model_id IS NOT NULL
         AND length(trim(actor_model_id)) > 0
         AND actor_model_version IS NOT NULL
         AND length(trim(actor_model_version)) > 0)
    ),
    CHECK (length(trim(field_name)) > 0),
    CHECK (length(trim(changed_at)) > 0),
    CHECK (length(trim(actor_role)) > 0),
    CHECK (length(trim(reason)) > 0),
    FOREIGN KEY (task_id) REFERENCES task (id) ON DELETE RESTRICT,
    FOREIGN KEY (actor_person_id) REFERENCES person (id) ON DELETE RESTRICT,
    FOREIGN KEY (actor_vertical_id) REFERENCES vertical (id) ON DELETE RESTRICT
);

CREATE TABLE task_attribute (
    task_id    INTEGER NOT NULL,
    attr_key   TEXT NOT NULL,
    attr_value TEXT,
    CHECK (length(trim(attr_key)) > 0),
    UNIQUE (task_id, attr_key),
    FOREIGN KEY (task_id) REFERENCES task (id) ON DELETE RESTRICT
);

CREATE TABLE task_affected_vertical (
    task_id     INTEGER NOT NULL,
    vertical_id INTEGER NOT NULL,
    UNIQUE (task_id, vertical_id),
    FOREIGN KEY (task_id) REFERENCES task (id) ON DELETE RESTRICT,
    FOREIGN KEY (vertical_id) REFERENCES vertical (id) ON DELETE RESTRICT
);
