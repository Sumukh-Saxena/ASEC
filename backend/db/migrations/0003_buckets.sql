-- Slice 2.4: bucket tables per docs/SRS.md FR-BKT-01, FR-BKT-02, FR-BKT-08,
-- FR-BKT-10, FR-BKT-12 and the §4.1 Bucket entity ("Window bounds, parameters
-- used, member messages, processing state, link to earlier bucket").
-- FR-BKT-04/FR-BKT-05 bodies are blank in the SRS; this slice stores the schema
-- fields only and infers no formation semantics.
-- FR-BKT-12 names the state vocabulary verbatim; transitions belong to later
-- processing slices.

CREATE TABLE bucket (
    id INTEGER PRIMARY KEY,

    -- FR-BKT-02: a bucket contains messages from one group only.
    group_id INTEGER NOT NULL,

    -- FR-BKT-08 / §4.1: a late message forms a new bucket linked to the earlier
    -- bucket. NULL for the first bucket of a sequence; no unique rule (several
    -- successors are possible).
    predecessor_bucket_id INTEGER,

    -- §4.1 "Window bounds" (FR-BKT-03, FR-ING-02: missing stored as missing).
    start_event_timestamp TEXT,
    end_event_timestamp TEXT,

    -- §4.1 "parameters used" (§3.2 parameter table); stored as supplied.
    parameters_used TEXT,

    -- FR-BKT-12: the SRS names these five states verbatim.
    processing_state TEXT NOT NULL DEFAULT 'OPEN',

    CHECK (start_event_timestamp IS NULL OR length(trim(start_event_timestamp)) > 0),
    CHECK (end_event_timestamp IS NULL OR length(trim(end_event_timestamp)) > 0),
    CHECK (parameters_used IS NULL OR length(trim(parameters_used)) > 0),
    CHECK (processing_state IN (
        'OPEN', 'CLOSED', 'PROCESSED', 'FAILED', 'AWAITING_RECOVERY'
    )),

    FOREIGN KEY (group_id) REFERENCES source_group (id) ON DELETE RESTRICT,
    FOREIGN KEY (predecessor_bucket_id) REFERENCES bucket (id) ON DELETE RESTRICT
);

-- FR-BKT-01: every message is in at most one bucket; UNIQUE (message_id)
-- enforces that membership shape and supports retry safety against the SRS
-- concurrency needs (NFR-REL-04) by making a duplicate retry a no-op failure.
--
-- FR-BKT-02 is enforced by the database: group_id is stored once per
-- membership row (approved integrity-only denormalization) and pinned by two
-- composite foreign keys to equal both the message's group and the bucket's
-- group, so a cross-group membership cannot be stored by any writer.
CREATE TABLE bucket_message (
    bucket_id  INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    group_id   INTEGER NOT NULL,

    PRIMARY KEY (bucket_id, message_id),
    UNIQUE (message_id),

    FOREIGN KEY (message_id, group_id)
        REFERENCES source_message (id, group_id) ON DELETE RESTRICT,
    FOREIGN KEY (bucket_id, group_id)
        REFERENCES bucket (id, group_id) ON DELETE RESTRICT
);

-- Parent-side coverage for the composite foreign keys above. Semantically
-- vacuous (id is already unique); required so SQLite can match the FK pairs.
CREATE UNIQUE INDEX ux_source_message_id_group
    ON source_message (id, group_id);
CREATE UNIQUE INDEX ux_bucket_id_group ON bucket (id, group_id);
