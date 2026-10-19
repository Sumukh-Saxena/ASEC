-- 0010_source_group_progress.sql (Phase 2 — bucket formation service)
--
-- Durable, monotonic accepted event-time progress per source group, the
-- authority for FR-BKT-05 closure: a bucket may be closed iff
-- e + C <= accepted progress P, and assignment never closes a bucket or
-- advances progress (docs/SRS.md FR-BKT-05, pending amendment P2/P6).
-- One row per group (primary key = group); progress_to is the boundary the
-- formation service uses, updated only by authoritative callers inside one
-- transaction together with the closures it authorizes. UTC ISO-8601
-- normalization is service-level; the column stays free text like every
-- other SRS timestamp column (DR-04). Retention follows DR-07; the group
-- reference is RESTRICT per DR-01/DR-02, so deleting a group that holds
-- progress cannot detach the record.

CREATE TABLE source_group_progress (
    group_id     INTEGER PRIMARY KEY,
    progress_to  TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    CHECK (length(trim(progress_to)) > 0),
    CHECK (length(trim(updated_at)) > 0),
    FOREIGN KEY (group_id) REFERENCES source_group (id) ON DELETE RESTRICT
);
