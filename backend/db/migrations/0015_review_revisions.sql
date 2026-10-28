-- 0015_review_revisions.sql (Phase 6, WS-1)
--
-- Two additive review-phase tables; no committed table is altered.
--
-- revision_assignee (FR-REV-03, FR-REV-09, DR-03; OD-REV-3, amendments
-- A2/A6/A14): the complete effective assignee set of one proposal_revision.
-- Phase 5's proposal_assignee (0012) keeps the original generated
-- resolution untouched; every edit snapshots its own effective set here, so
-- the original version stays unchanged and both are available as labelled
-- evaluation data (FR-REV-08, DR-07). The resolution vocabulary mirrors
-- 0012 verbatim (RESOLVED/UNRESOLVED/FLAGGED) and adds only the MANUAL
-- route for a reviewer's direct roster selection (OD-REV-2) next to the
-- MENTION and NAME_MATCH routes resolved by the backend.
--
-- A14 partial unique index: a person may appear at most once per revision
-- (first occurrence wins before storage). UNRESOLVED/FLAGGED rows carry no
-- person (the 0012 CHECK makes a guess unrepresentable), so the index is
-- partial and those rows are not collapsed.
--
-- authorization_denial (FR-AUT-06, owner amendment A17): the recorded half
-- of "unauthorized attempts shall be rejected and recorded". The service
-- inserts one row in its own committed transaction BEFORE raising
-- ReviewAuthorizationError. operation / subject_type / reason are fixed
-- service tokens, never caller-supplied prose, so no message content or
-- contact data can enter this table. actor_person_id is nullable because an
-- unknown caller id has no roster row (missing stored as missing, DR-04).
--
-- FKs follow DR-01/DR-02/RESTRICT: no person, revision or proposal deletion
-- detaches stored review history or audit rows. Append-only behavior is a
-- service guarantee (no UPDATE/DELETE path; NFR-AUD-03).

CREATE TABLE revision_assignee (
    id            INTEGER PRIMARY KEY,
    revision_id   INTEGER NOT NULL,
    assignee_text TEXT NOT NULL,
    resolution    TEXT NOT NULL,
    person_id     INTEGER,
    resolved_via  TEXT,
    CHECK (resolution IN ('RESOLVED', 'UNRESOLVED', 'FLAGGED')),
    CHECK (resolved_via IS NULL OR resolved_via IN ('MENTION', 'NAME_MATCH', 'MANUAL')),
    CHECK (length(trim(assignee_text)) > 0),
    CHECK (
        (resolution = 'RESOLVED'
         AND person_id IS NOT NULL
         AND resolved_via IS NOT NULL)
        OR
        (resolution IN ('UNRESOLVED', 'FLAGGED')
         AND person_id IS NULL
         AND resolved_via IS NULL)
    ),
    FOREIGN KEY (revision_id) REFERENCES proposal_revision (id) ON DELETE RESTRICT,
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX ux_revision_assignee_person
    ON revision_assignee (revision_id, person_id)
    WHERE person_id IS NOT NULL;

CREATE TABLE authorization_denial (
    id              INTEGER PRIMARY KEY,
    actor_person_id INTEGER,
    operation       TEXT NOT NULL,
    subject_type    TEXT NOT NULL,
    subject_id      INTEGER,
    reason          TEXT NOT NULL,
    attempted_at    TEXT NOT NULL,
    CHECK (length(trim(operation)) > 0),
    CHECK (length(trim(subject_type)) > 0),
    CHECK (length(trim(reason)) > 0),
    CHECK (length(trim(attempted_at)) > 0),
    FOREIGN KEY (actor_person_id) REFERENCES person (id) ON DELETE RESTRICT
);
