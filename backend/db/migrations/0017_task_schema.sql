-- 0017_task_schema.sql: Rebuild task and task_assignee tables (PB-3)

PRAGMA defer_foreign_keys = ON;

CREATE TABLE task_new (
    id INTEGER PRIMARY KEY,
    proposal_id INTEGER NOT NULL UNIQUE,
    task_type TEXT NOT NULL,
    source_vertical_id INTEGER NOT NULL,
    responsible_vertical_id INTEGER NOT NULL,
    creator_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    assignment_mode TEXT NOT NULL,
    people_needed INTEGER,
    people_needed_unlimited INTEGER NOT NULL DEFAULT 0,
    approval_state TEXT NOT NULL DEFAULT 'NOT_REQUIRED',
    cancellation TEXT NOT NULL DEFAULT 'ACTIVE',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    CHECK (task_type IN ('INTERNAL', 'CROSS_VERTICAL')),
    CHECK (assignment_mode IN ('EXPLICIT', 'OPEN')),
    CHECK (approval_state IN ('NOT_REQUIRED', 'PENDING', 'APPROVED', 'DENIED')),
    CHECK (cancellation IN ('ACTIVE', 'CANCELLED')),
    CHECK ((task_type = 'INTERNAL' AND responsible_vertical_id = source_vertical_id)
        OR (task_type = 'CROSS_VERTICAL' AND responsible_vertical_id != source_vertical_id)),
    CHECK (people_needed IS NULL OR people_needed > 0),
    CHECK (people_needed_unlimited IN (0, 1)),
    CHECK (people_needed_unlimited = 0 OR people_needed IS NULL),
    CHECK (assignment_mode != 'OPEN' OR ((people_needed IS NULL) = (people_needed_unlimited = 1))),
    CHECK (assignment_mode != 'EXPLICIT' OR (people_needed IS NULL AND people_needed_unlimited = 0)),
    CHECK (version >= 1),
    CHECK (length(trim(description)) > 0),
    CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (proposal_id) REFERENCES proposal (id) ON DELETE RESTRICT,
    FOREIGN KEY (source_vertical_id) REFERENCES vertical (id) ON DELETE RESTRICT,
    FOREIGN KEY (responsible_vertical_id) REFERENCES vertical (id) ON DELETE RESTRICT,
    FOREIGN KEY (creator_id) REFERENCES person (id) ON DELETE RESTRICT
);

INSERT INTO task_new (id, proposal_id, task_type, source_vertical_id,
                      responsible_vertical_id, creator_id, description,
                      assignment_mode, people_needed, people_needed_unlimited,
                      approval_state, cancellation, version, created_at)
SELECT id, proposal_id, task_type, source_vertical_id,
       CASE WHEN task_type = 'INTERNAL' THEN source_vertical_id
            ELSE responsible_vertical_id END,
       creator_id, description, assignment_mode,
       CASE WHEN assignment_mode = 'EXPLICIT' THEN NULL ELSE people_needed END,
       people_needed_unlimited, approval_state, cancellation, version, created_at
FROM task;

CREATE TABLE task_assignee_new (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    agreement TEXT NOT NULL DEFAULT 'AWAITING',
    agreed_at TEXT,
    work_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
    cancelled INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL,
    CHECK (agreement IN ('AWAITING', 'ACCEPTED', 'DENIED')),
    CHECK (work_status IN ('NOT_STARTED', 'IN_PROGRESS', 'COMPLETED')),
    CHECK (cancelled IN (0, 1)),
    CHECK (agreement = 'AWAITING' OR (agreed_at IS NOT NULL AND length(trim(agreed_at)) > 0)),
    CHECK (agreement != 'AWAITING' OR agreed_at IS NULL),
    CHECK (length(trim(added_at)) > 0),
    FOREIGN KEY (task_id) REFERENCES task_new (id) ON DELETE RESTRICT,
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT
);

INSERT INTO task_assignee_new (task_id, person_id, agreement, agreed_at,
                               work_status, cancelled, added_at)
SELECT task_id, person_id, agreement, agreed_at, work_status, cancelled, added_at
FROM task_assignee;

CREATE UNIQUE INDEX ux_task_assignee_active
    ON task_assignee_new (task_id, person_id)
    WHERE agreement != 'DENIED' AND cancelled = 0;

DROP TABLE task_assignee;
DROP TABLE task;
ALTER TABLE task_new RENAME TO task;
ALTER TABLE task_assignee_new RENAME TO task_assignee;
