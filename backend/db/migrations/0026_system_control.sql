-- 0026_system_control.sql (Phase 12 D2, owner decision C)
--
-- Two independent operator switches: PROCESSING and DELIVERY. A pause stops
-- the worker's next processing tick or its next delivery pass; ingestion is
-- never paused (owner decision C). The SRS has no pause requirement, so this
-- table is an owner-added operational control and every route that writes it
-- is listed in NON_MATRIX_ROUTE_OPERATIONS rather than invented as an
-- FR-AUT-04 row.
--
-- One row per control, upserted by backend.system_control. The absence of a
-- row means "resumed", so the migration seeds nothing and a fresh database
-- needs no state row before the first read. updated_by records the last Admin
-- who changed the switch (NULL only for a row written without a person).

CREATE TABLE system_control (
    name       TEXT PRIMARY KEY,
    paused     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    updated_by INTEGER NULL,

    CHECK (name IN ('PROCESSING', 'DELIVERY')),
    CHECK (paused IN (0, 1)),
    CHECK (length(trim(updated_at)) > 0),

    FOREIGN KEY (updated_by) REFERENCES person (id) ON DELETE RESTRICT
);
