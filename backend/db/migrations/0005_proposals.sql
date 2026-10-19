-- Slice 2.6: proposal candidates, edit revisions and related links
-- per docs/SRS.md FR-PRP-01 .. FR-PRP-13, FR-REV-01 .. FR-REV-09,
-- §4.1 "Proposal"/"Candidate"/"Proposal revision", NFR-AUD-01/02,
-- NFR-CON-01/02, DR-01/03/04/07 and AC-02/03/05/06.
--
-- The generating run is represented by reusing a processing_attempt row
-- (Slice 2.5, open item AU-2): proposal attempts target the whole closed
-- bucket (FR-PRP-01), so (attempt_id, bucket_id) is pinned below.
--
-- There is deliberately NO UNIQUE (attempt_id, primary_message_id):
-- AC-02 allows several proposals for the same source message in one run
-- (e.g. a task plus an announcement). FR-PRP-04 "never discarded" shape
-- and NFR-REL-01/03 duplicate-run prevention are service-level rules.
--
-- ptype / assignment_mode / task_type vocabularies are FR-PRP-06/08/09
-- verbatim; review_state is FR-REV-01 verbatim. structurally_valid and
-- people_needed_unlimited are 0/1 encoding choices (no invented words;
-- D1 rule). review_state is a mutable column: state transitions and the
-- human-in-the-loop review flow are service-level (FR-REV-01; NFR-CON-02).
-- The storing service supplies the reviewer and the edit context
-- (FR-REV-02, FR-REV-03, DR-03); the schema only guards shape.
--
-- "Unconfirmed" markers (AC-06: every edited ingredient marked on
-- presentation) are derived at the service layer from
-- proposal_revision.changed_fields; nothing is stored or defaulted here.
--
-- Accepted-proposal acceptance integrity (FR-REV-05/FR-REV-10: exactly one
-- task or announcement per accepted proposal) is established by the 2.7/2.10
-- UNIQUE (proposal_id) constraints plus a service-level cross-table rule.
--
-- Provenance (generated_at, model_id, model_version, prompt_version) is
-- copied from the run attempt onto every proposal (FR-PRP-12, NFR-AUD-01/02)
-- and never altered by edits (FR-REV-03).
CREATE TABLE proposal (
    id                  INTEGER PRIMARY KEY,
    attempt_id          INTEGER NOT NULL,
    bucket_id           INTEGER NOT NULL,
    primary_message_id  INTEGER NOT NULL,

    -- FR-PRP-06 verbatim: a task or an announcement.
    ptype               TEXT NOT NULL,

    -- FR-PRP-11 / DR-04: missing stored as missing; never empty.
    description         TEXT,
    assignment_mode     TEXT NOT NULL,
    proposed_assignees_text TEXT,

    -- FR-REV-06: a positive count, or UNLIMITED as a 0/1 encoding; never
    -- both at once; an unfilled count (NULL, 0) is also valid.
    people_needed           INTEGER,
    people_needed_unlimited INTEGER NOT NULL DEFAULT 0,

    -- FR-PRP-09 verbatim.
    task_type           TEXT NOT NULL,

    source_vertical_id              INTEGER NOT NULL,
    suggested_responsible_vertical_id INTEGER,

    -- FR-PRP-13: structurally-valid boolean, 0/1 encoding.
    structurally_valid  INTEGER NOT NULL,

    -- FR-PRP-12: pinned run provenance (NFR-AUD-01/02).
    generated_at        TEXT NOT NULL,
    model_id            TEXT NOT NULL,
    model_version       TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,

    -- FR-REV-01 verbatim vocabulary; mutable column (service-level flow).
    review_state        TEXT NOT NULL DEFAULT 'PENDING',

    CHECK (ptype IN ('TASK', 'ANNOUNCEMENT')),
    CHECK (description IS NULL OR length(trim(description)) > 0),
    CHECK (assignment_mode IN ('EXPLICIT', 'OPEN', 'NONE')),
    CHECK (proposed_assignees_text IS NULL OR length(trim(proposed_assignees_text)) > 0),
    CHECK (people_needed IS NULL OR people_needed > 0),
    CHECK (people_needed_unlimited IN (0, 1)),
    CHECK (people_needed IS NULL OR people_needed_unlimited = 0),
    CHECK (task_type IN ('INTERNAL', 'CROSS_VERTICAL')),
    CHECK (structurally_valid IN (0, 1)),
    CHECK (length(trim(generated_at)) > 0),
    CHECK (length(trim(model_id)) > 0),
    CHECK (length(trim(model_version)) > 0),
    CHECK (length(trim(prompt_version)) > 0),
    CHECK (review_state IN ('PENDING', 'EDITED', 'ACCEPTED', 'REJECTED')),
    -- FR-PRP-08: "NONE (announcements only)" — the NONE mode does not occur
    -- on TASK proposals. The converse (every announcement is NONE) is not
    -- stated by the SRS and is not enforced here.
    CHECK (assignment_mode != 'NONE' OR ptype = 'ANNOUNCEMENT'),

    FOREIGN KEY (attempt_id, bucket_id)
        REFERENCES processing_attempt (id, bucket_id) ON DELETE RESTRICT,
    FOREIGN KEY (bucket_id, primary_message_id)
        REFERENCES bucket_message (bucket_id, message_id) ON DELETE RESTRICT,
    FOREIGN KEY (bucket_id) REFERENCES bucket (id) ON DELETE RESTRICT,
    FOREIGN KEY (source_vertical_id) REFERENCES vertical (id) ON DELETE RESTRICT,
    FOREIGN KEY (suggested_responsible_vertical_id)
        REFERENCES vertical (id) ON DELETE RESTRICT
);

-- FR-PRP-07: attribute links (e.g. venue, time). Optional value; NULL stores
-- "missing stored as missing" (DR-04) and blank is rejected.
CREATE TABLE proposal_attribute (
    proposal_id INTEGER NOT NULL,
    attr_key    TEXT NOT NULL,
    attr_value  TEXT,

    CHECK (length(trim(attr_key)) > 0),
    CHECK (attr_value IS NULL OR length(trim(attr_value)) > 0),

    UNIQUE (proposal_id, attr_key),
    FOREIGN KEY (proposal_id) REFERENCES proposal (id) ON DELETE RESTRICT
);

-- FR-PRP-07: affected verticals (e.g. only chairperson vertical, all
-- verticals). No default row; absent links mean "not specified".
CREATE TABLE proposal_affected_vertical (
    proposal_id INTEGER NOT NULL,
    vertical_id INTEGER NOT NULL,

    PRIMARY KEY (proposal_id, vertical_id),
    FOREIGN KEY (proposal_id) REFERENCES proposal (id) ON DELETE RESTRICT,
    FOREIGN KEY (vertical_id) REFERENCES vertical (id) ON DELETE RESTRICT
);

-- FR-PRP-03: contextual messages, kept as bucket-member links to the same
-- source content (no duplication). A message appears at most once per
-- proposal; composite (proposal, bucket) pins it to the proposal's bucket.
-- Parent-side coverage for (proposal_id, bucket_id) is the semantically
-- vacuous ux_proposal_id_bucket below. Included messages ARE members
-- (FR-PRP-03), represented as regular rows in this same table.
CREATE TABLE proposal_context_message (
    proposal_id INTEGER NOT NULL,
    bucket_id   INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,

    PRIMARY KEY (proposal_id, bucket_id, message_id),

    UNIQUE (proposal_id, message_id),
    FOREIGN KEY (proposal_id, bucket_id)
        REFERENCES proposal (id, bucket_id) ON DELETE RESTRICT,
    FOREIGN KEY (bucket_id, message_id)
        REFERENCES bucket_message (bucket_id, message_id) ON DELETE RESTRICT
);

-- Parent-side coverage for proposal_context_message (proposal_id, bucket_id),
-- matching the slice 2.4.1 pattern. Semantically vacuous (id is already
-- unique); required so SQLite can match the composite FK.
CREATE UNIQUE INDEX ux_proposal_id_bucket ON proposal (id, bucket_id);

-- One proposal_revision row per human edit (FR-REV-03): edit provenance
-- (person, changed_fields, changed_at) plus a full snapshot of the editable
-- content fields at that edit. The proposal row is never mutated by an edit;
-- each revision is a separate record. changed_fields is stored as supplied
-- (FR-REV-09, JSON schema is a Phase 3 service concern); determining the
-- current value and "unconfirmed" presentation markers (AC-06) is
-- service-level (latest revision selection). Snapshot CHECKs mirror the
-- proposal vocabularies (FR-REV-06 encoding, FR-PRP-08/09 verbatim).
CREATE TABLE proposal_revision (
    id              INTEGER PRIMARY KEY,
    proposal_id     INTEGER NOT NULL,
    revision_number INTEGER NOT NULL,

    -- FR-REV-03 edit provenance.
    person_id       INTEGER NOT NULL,
    changed_fields  TEXT NOT NULL,
    changed_at      TEXT NOT NULL,

    -- Editable snapshot: FR-REV-04 values as supplied after the edit.
    description             TEXT,
    assignment_mode         TEXT NOT NULL,
    proposed_assignees_text TEXT,
    people_needed           INTEGER,
    people_needed_unlimited INTEGER NOT NULL DEFAULT 0,
    task_type               TEXT NOT NULL,
    suggested_responsible_vertical_id INTEGER,

    CHECK (revision_number > 0),
    CHECK (length(trim(changed_fields)) > 0),
    CHECK (length(trim(changed_at)) > 0),
    CHECK (description IS NULL OR length(trim(description)) > 0),
    CHECK (assignment_mode IN ('EXPLICIT', 'OPEN', 'NONE')),
    CHECK (proposed_assignees_text IS NULL OR length(trim(proposed_assignees_text)) > 0),
    CHECK (people_needed IS NULL OR people_needed > 0),
    CHECK (people_needed_unlimited IN (0, 1)),
    CHECK (people_needed IS NULL OR people_needed_unlimited = 0),
    CHECK (task_type IN ('INTERNAL', 'CROSS_VERTICAL')),

    UNIQUE (proposal_id, revision_number),
    FOREIGN KEY (proposal_id) REFERENCES proposal (id) ON DELETE RESTRICT,
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT,
    FOREIGN KEY (suggested_responsible_vertical_id)
        REFERENCES vertical (id) ON DELETE RESTRICT
);

-- FR-REV-03: attribute and affected-vertical links are part of the editable
-- content and are snapshotted per revision.
CREATE TABLE revision_attribute (
    revision_id INTEGER NOT NULL,
    attr_key    TEXT NOT NULL,
    attr_value  TEXT,

    CHECK (length(trim(attr_key)) > 0),
    CHECK (attr_value IS NULL OR length(trim(attr_value)) > 0),

    UNIQUE (revision_id, attr_key),
    FOREIGN KEY (revision_id)
        REFERENCES proposal_revision (id) ON DELETE RESTRICT
);

CREATE TABLE revision_affected_vertical (
    revision_id INTEGER NOT NULL,
    vertical_id INTEGER NOT NULL,

    PRIMARY KEY (revision_id, vertical_id),
    FOREIGN KEY (revision_id)
        REFERENCES proposal_revision (id) ON DELETE RESTRICT,
    FOREIGN KEY (vertical_id) REFERENCES vertical (id) ON DELETE RESTRICT
);
