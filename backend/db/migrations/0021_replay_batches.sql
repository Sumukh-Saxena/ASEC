-- 0021_replay_batches.sql (Phase 9 — FR-ING-08 replay provenance)
--
-- One row per replay import attempt. A batch is provenance for imported
-- messages, not an owner of them: there are deliberately no foreign keys, the
-- messages keep their ordinary source_message rows and group references, and
-- deleting a batch never deletes evidence (DR-03).
--
-- outcome is free text (the SRS defines no vocabulary for an import outcome,
-- same rationale as 0008's event_type). counts_json records inserted,
-- duplicate, rejected, skipped, unknown-sender and indicative counts plus the
-- time zone used; the original export is identified by file_sha256. One row
-- per (file_sha256, time_zone): re-importing the same file is reported, never
-- duplicated.

CREATE TABLE replay_batch (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    time_zone   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    outcome     TEXT NOT NULL,
    counts_json TEXT NOT NULL,
    CHECK (length(trim(source)) > 0),
    CHECK (length(trim(file_sha256)) > 0),
    CHECK (length(trim(time_zone)) > 0),
    CHECK (length(trim(started_at)) > 0),
    CHECK (finished_at IS NULL OR length(trim(finished_at)) > 0),
    CHECK (length(trim(outcome)) > 0),
    CHECK (length(trim(counts_json)) > 0),
    UNIQUE (file_sha256, time_zone)
);
