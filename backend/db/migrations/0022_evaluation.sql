-- Append-only evaluation provenance. Vocabularies are an encoding choice
-- (D1 rule): the SRS names the two AI functions and end-to-end evaluation;
-- it defines no token set, so no CHECK is added on outcome text.
CREATE TABLE evaluation_run (
    id              INTEGER PRIMARY KEY,
    function        TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    dataset_sha256  TEXT NOT NULL,
    case_count      INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    metrics_json    TEXT NOT NULL,
    CHECK (function IN ('RELEVANCE', 'PROPOSAL', 'END_TO_END')),
    CHECK (length(trim(model_id)) > 0),
    CHECK (length(trim(model_version)) > 0),
    CHECK (length(trim(prompt_version)) > 0),
    CHECK (length(trim(dataset_sha256)) > 0),
    CHECK (case_count >= 0),
    CHECK (length(trim(started_at)) > 0),
    CHECK (finished_at IS NULL OR length(trim(finished_at)) > 0),
    CHECK (length(trim(metrics_json)) > 0)
);

CREATE TABLE evaluation_case_result (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL,
    case_id        TEXT NOT NULL,
    expected_json  TEXT NOT NULL,
    predicted_json TEXT,
    correct        INTEGER NOT NULL,
    CHECK (length(trim(case_id)) > 0),
    CHECK (length(trim(expected_json)) > 0),
    CHECK (predicted_json IS NULL OR length(trim(predicted_json)) > 0),
    CHECK (correct IN (0, 1)),
    UNIQUE (run_id, case_id),
    FOREIGN KEY (run_id) REFERENCES evaluation_run (id) ON DELETE RESTRICT
);
