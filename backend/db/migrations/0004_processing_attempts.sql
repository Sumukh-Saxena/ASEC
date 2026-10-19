-- Slice 2.5: processing attempts, relevance decisions, relevance corrections
-- per docs/SRS.md FR-REL-01 .. FR-REL-07, FR-PM-05 (correction interface is
-- Post-MVP; only the record exists here), NFR-REL-01/02/03, NFR-AUD-02,
-- NFR-CON-01, DR-01/02/04/07 and the §4.1 "Processing attempt" and
-- "Relevance decision / correction" entities.
--
-- processing_attempt.bucket_id is the §4.1 "input reference" (FR-REL-01:
-- the classifier runs on each closed bucket) and is NOT NULL, the approved
-- Slice 2.5 design choice: every MVP processing stage recorded here is
-- bucket-scoped (FR-REL-01, FR-PRP-01). A future non-bucket-scoped stage
-- would require a new rebuild-type migration (open item AU-1,
-- docs/PHASE2_PLAN.md).
--
-- outcome vocabulary is §4.1 verbatim; decision and correction vocabularies
-- are FR-REL-02 verbatim ("There is no UNCERTAIN state"). Stage tokens carry
-- no vocabulary: the SRS does not define one.
--
-- Correction timestamps are stored verbatim. No recency/effective-correction
-- selection is implemented or guaranteed here (FR-REL-06 "from that moment"
-- remains a service-level rule for a later slice; open items AU-3a/AU-3b/
-- AU-3c in docs/PHASE2_PLAN.md).

CREATE TABLE processing_attempt (
    id              INTEGER PRIMARY KEY,

    -- §4.1 "Stage": free text, no SRS-defined vocabulary.
    stage           TEXT NOT NULL,

    -- §4.1 "input reference"; always the bucket processed (FR-REL-01).
    bucket_id       INTEGER NOT NULL,

    -- §4.1 "model and configuration version"; may be absent when a stage
    -- failed before the model ran (missing stored as missing, DR-04).
    model_id        TEXT,
    model_version   TEXT,
    config_version  TEXT,

    -- §4.1 "times"
    started_at      TEXT NOT NULL,
    finished_at     TEXT,

    -- §4.1 verbatim vocabulary.
    outcome         TEXT NOT NULL,

    -- §4.1 "error"; NULL for clean runs, never empty.
    error           TEXT,

    CHECK (stage = trim(stage) AND length(stage) > 0),
    CHECK (model_id        IS NULL OR length(trim(model_id)) > 0),
    CHECK (model_version   IS NULL OR length(trim(model_version)) > 0),
    CHECK (config_version  IS NULL OR length(trim(config_version)) > 0),
    CHECK (length(trim(started_at)) > 0),
    CHECK (finished_at IS NULL OR length(trim(finished_at)) > 0),
    CHECK (outcome IN ('SUCCEEDED', 'FAILED_RETRYABLE', 'FAILED_PERMANENT')),
    CHECK (error IS NULL OR length(trim(error)) > 0),

    FOREIGN KEY (bucket_id) REFERENCES bucket (id) ON DELETE RESTRICT
);

-- FR-REL-01/FR-REL-05: one decision per (attempt, message) pair, recording
-- message, bucket, processing attempt, model and version, prompt or
-- configuration version, output and timestamp. Provenance is copied onto the
-- decision row so the original stays intact (owner decision, approved).
--
-- FR-REL-06: a decision is immutable. SQLite enforcement covers existence and
-- shape only; append-only write discipline is service-level (no triggers).
--
-- FR-REL-01/FR-REL-05 composite keys: (bucket_id, message_id) references the
-- real bucket_message membership row (the decision's message must belong to
-- the named bucket — FR-REL-05); (attempt_id, bucket_id) pins the attempt to
-- the same bucket as the decision (FR-REL-01 whole-bucket context).
CREATE TABLE relevance_decision (
    id             INTEGER PRIMARY KEY,
    bucket_id      INTEGER NOT NULL,
    message_id     INTEGER NOT NULL,
    attempt_id     INTEGER NOT NULL,

    -- FR-REL-02 verbatim.
    decision       TEXT NOT NULL,

    -- FR-REL-05 provenance, copied per decision.
    model_id       TEXT NOT NULL,
    model_version  TEXT NOT NULL,
    prompt_version TEXT NOT NULL,

    decided_at     TEXT NOT NULL,

    CHECK (decision IN ('RELEVANT', 'IRRELEVANT')),
    CHECK (length(trim(model_id)) > 0),
    CHECK (length(trim(model_version)) > 0),
    CHECK (length(trim(prompt_version)) > 0),
    CHECK (length(trim(decided_at)) > 0),

    UNIQUE (attempt_id, message_id),

    FOREIGN KEY (bucket_id, message_id)
        REFERENCES bucket_message (bucket_id, message_id) ON DELETE RESTRICT,
    FOREIGN KEY (attempt_id, bucket_id)
        REFERENCES processing_attempt (id, bucket_id) ON DELETE RESTRICT,
    FOREIGN KEY (bucket_id) REFERENCES bucket (id) ON DELETE RESTRICT
);

-- FR-REL-06: a human correction is a separate record; the original decision
-- is preserved unchanged. The corrected value uses the FR-REL-02 vocabulary.
-- corrected_at is stored verbatim; recency/effective-result selection is a
-- later service-level slice.
CREATE TABLE relevance_correction (
    id                 INTEGER PRIMARY KEY,
    decision_id        INTEGER NOT NULL,
    person_id          INTEGER NOT NULL,
    corrected_decision TEXT NOT NULL,
    corrected_at       TEXT NOT NULL,

    CHECK (corrected_decision IN ('RELEVANT', 'IRRELEVANT')),
    CHECK (length(trim(corrected_at)) > 0),

    FOREIGN KEY (decision_id) REFERENCES relevance_decision (id) ON DELETE RESTRICT,
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT
);

-- Parent-side coverage for relevance_decision (attempt_id, bucket_id).
-- Semantically vacuous (id is already unique); required so SQLite can match
-- the composite FK, same pattern as the slice 2.4.1 correction.
CREATE UNIQUE INDEX ux_processing_attempt_id_bucket
    ON processing_attempt (id, bucket_id);
