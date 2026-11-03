-- 0019_source_message_time_basis.sql (Phase 9 — PB-2 time basis)
--
-- Which timestamp formed the bucket for this message: a usable
-- timezone-aware event_timestamp ('EVENT') or received_at ('RECEIPT'), the
-- PB-2 fallback that keeps FR-BKT-01 ("every message in exactly one bucket")
-- true instead of silently skipping a message whose event time is unusable.
-- The stored event_timestamp itself is never overwritten (FR-ING-02, DR-03);
-- this column records the selection, it does not replace the source value.
--
-- Additive and nullable: legacy rows keep NULL and no backfill is invented.
-- The assignment service writes the value when it places a message.

ALTER TABLE source_message
    ADD COLUMN time_basis TEXT NULL
    CHECK (time_basis IS NULL OR time_basis IN ('EVENT', 'RECEIPT'));
