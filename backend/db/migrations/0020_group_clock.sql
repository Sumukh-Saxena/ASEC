-- 0020_group_clock.sql (Phase 9 — PB-1 per-group clock)
--
-- Durable monotonic clock per source group, the authority for PB-1
-- settle-then-assign and FR-BKT-05 closure ("a bucket is closed when e + C has
-- passed"). ``clock_at`` never moves backwards, ``mode`` is locked on the
-- first write (LIVE or REPLAY; replay and live data must not mix in one
-- group's clock), and the row survives a restart.
--
-- Unseeded by design (O9-5 option (a)): no seed rows, the first authoritative
-- advance_group_clock call creates the row with its mode. A seeded LIVE row
-- would make a replay-first database fail the mode lock.
--
-- The group reference is RESTRICT per DR-01/DR-02 so deleting a group cannot
-- detach its clock; UTC ISO-8601 normalization is service-level (DR-04).

CREATE TABLE source_group_clock (
    group_id   INTEGER PRIMARY KEY,
    clock_at   TEXT NOT NULL,
    mode       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (length(trim(clock_at)) > 0),
    CHECK (mode IN ('LIVE', 'REPLAY')),
    CHECK (length(trim(updated_at)) > 0),
    FOREIGN KEY (group_id) REFERENCES source_group (id) ON DELETE RESTRICT
);
