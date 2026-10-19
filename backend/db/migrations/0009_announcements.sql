-- Slice 2.10: accepted announcements per docs/SRS.md FR-REV-10 ("shall be
-- shown to its audience (a Head's vertical, or the sender AH's volunteers)
-- and shall not create a task"), FR-REV-01 (accepting creates the record)
-- and FR-REV-07 (the proposal and its history are kept after the decision).
--
-- Each row is the accepted-announcement record for one proposal: who accepted
-- (the reviewer or Admin, per FR-REV-02), when, and the audience (a named
-- vertical via foreign key, or a free-text scope otherwise; the SRS defines
-- no audience vocabulary, so no CHECK applies).
--
-- Acceptance integrity: UNIQUE (proposal_id) makes a proposal the origin of
-- at most one accepted record. The task/announcement XOR (exactly one of
-- task or announcement per accepted proposal; FR-REV-05, AC-18) spans this
-- table and the Slice 2.7 task table (task UNIQUE (proposal_id)) and is a
-- service-level guarantee.
--
-- "Shall not create a task" is a service-level rule; without the 2.7 tables
-- the schema cannot reference tasks. Announcement distribution (which
-- volunteers read it) is display/service behavior outside storage.
CREATE TABLE announcement (
    id INTEGER PRIMARY KEY,

    -- Origin proposal (FR-REV-10); at most one accepted record per proposal.
    proposal_id      INTEGER NOT NULL,

    -- FR-REV-01/FR-REV-02: the accepting person and the acceptance time,
    -- stored verbatim.
    accepted_by      INTEGER NOT NULL,
    accepted_at      TEXT NOT NULL,

    -- Audience: a named vertical, or the sender AH's volunteers expressed as
    -- free-text scope. The SRS defines no audience value vocabulary.
    audience_vertical_id INTEGER,
    audience_scope   TEXT NOT NULL,

    CHECK (length(trim(accepted_at)) > 0),
    CHECK (length(trim(audience_scope)) > 0),

    UNIQUE (proposal_id),

    FOREIGN KEY (proposal_id) REFERENCES proposal (id) ON DELETE RESTRICT,
    FOREIGN KEY (accepted_by) REFERENCES person (id) ON DELETE RESTRICT,
    FOREIGN KEY (audience_vertical_id) REFERENCES vertical (id) ON DELETE RESTRICT
);
