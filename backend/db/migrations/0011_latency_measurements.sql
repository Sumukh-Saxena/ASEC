-- 0011_latency_measurements.sql (Phase 2, Slice 2.11)
--
-- Latency instrumentation records (NFR-PERF-02, SRS line 615: "The system
-- shall record ingestion delay, bucket delay, classifier latency, proposal
-- latency and review waiting time, as P50, P95 and P99").
--
-- One row per raw measurement. These rows support later computation of
-- P50/P95/P99 at read time; this slice implements no percentile computation
-- (DR-03: derived data never replaces source data) and no percentile rows
-- are stored. value_seconds names its unit in the column;
-- the SRS defines no unit vocabulary. metric carries the service's metric
-- label as free text (the SRS names the five kinds in prose; no invented
-- token vocabulary per the D1 rule — same rationale as 0008's event_type).
--
-- Each measurement points at exactly one existing record — source_message,
-- bucket, processing_attempt or proposal — via the 0008 exactly-one-subject
-- CHECK-sum pattern, keeping every measurement traceable to its
-- authoritative record chain (DR-01). Which metric pairs with which subject
-- type and how each value is derived from source timestamps are
-- service-level decisions (Phase 3); the schema does not enforce them.
--
-- The §6.4 target cells (SRS lines 608–611) are blank owner decisions:
-- targets are evaluation inputs, not recorded data, and none is invented
-- here. Recording itself (this table) is Phase 2; computing and comparing
-- against targets is later-phase work. Foreign keys RESTRICT everywhere per
-- DR-01/DR-02.

CREATE TABLE latency_measurement (
    id                   INTEGER PRIMARY KEY,
    metric               TEXT NOT NULL,
    value_seconds        REAL NOT NULL,
    recorded_at          TEXT NOT NULL,
    source_message_id    INTEGER,
    bucket_id            INTEGER,
    processing_attempt_id INTEGER,
    proposal_id          INTEGER,
    CHECK (value_seconds >= 0),
    CHECK (
        (source_message_id IS NOT NULL) + (bucket_id IS NOT NULL)
        + (processing_attempt_id IS NOT NULL) + (proposal_id IS NOT NULL) = 1
    ),
    CHECK (length(trim(metric)) > 0),
    CHECK (length(trim(recorded_at)) > 0),
    FOREIGN KEY (source_message_id)
        REFERENCES source_message (id) ON DELETE RESTRICT,
    FOREIGN KEY (bucket_id) REFERENCES bucket (id) ON DELETE RESTRICT,
    FOREIGN KEY (processing_attempt_id)
        REFERENCES processing_attempt (id) ON DELETE RESTRICT,
    FOREIGN KEY (proposal_id) REFERENCES proposal (id) ON DELETE RESTRICT
);
