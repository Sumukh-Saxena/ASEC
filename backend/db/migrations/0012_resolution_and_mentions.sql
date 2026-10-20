-- 0012_resolution_and_mentions.sql (Phase 2, Slice A4)
--
-- Three storage additions completing the review/assignment resolution surface:
--
-- proposal_decision (FR-REV-05, FR-REV-07, AC-04): the sender's single
-- review outcome per proposal. UNIQUE (proposal_id) is the schema side of
-- "one decision per proposal"; earlier review activity (edits) lives in
-- proposal_revision (0005). The note records the sender's optional comment
-- made together with the decision. Authorization of the decision
-- (FR-REV-09, the sender is the only reviewer) is service-level.
--
-- proposal_assignee (FR-PRP-10): the assignee(s) exactly as written in the
-- proposal text, with the backend's resolution state. RESOLVED rows carry
-- the resolved person and the resolution route (a mention resolved by
-- user ID, or a name matched against the roster); UNRESOLVED rows mark
-- several plausible matches and FLAGGED rows mark no match. Unresolved and
-- flagged rows carry neither person nor route — the CHECK makes an
-- implicit guess unrepresentable. Human confirmation of the resolution
-- happens at review (FR-REV-01/05, service level).
--
-- message_mention (FR-ING-02): the mention evidence extracted from source
-- messages as opaque Discord user IDs (NFR-SEC-04/EIR-03: handles only;
-- never resolved roster identity). The same user mentioned twice in one
-- message is two rows — source evidence is preserved (DR-03); mention
-- extraction timing is ingestion/service-level.
--
-- FKs follow DR-01/DR-02/DR-04 (RESTRICT; no person, roster or source
-- deletion detaches stored results). No new vocabularies beyond the SRS
-- tokens verbatim (D1 rule).
--
-- Not schema-enforceable under the project's no-trigger storage pattern:
-- that edited proposals keep their decision history (FR-REV-03, service
-- level), and that REJECTED proposals are never converted into tasks
-- (FR-REV-05 XOR, service level plus 0006/0009).

CREATE TABLE proposal_decision (
    id          INTEGER PRIMARY KEY,
    proposal_id INTEGER NOT NULL UNIQUE,
    decision    TEXT NOT NULL,
    decided_by  INTEGER NOT NULL,
    decided_at  TEXT NOT NULL,
    note        TEXT,
    CHECK (decision IN ('ACCEPTED', 'REJECTED')),
    CHECK (length(trim(decided_at)) > 0),
    CHECK (note IS NULL OR length(trim(note)) > 0),
    FOREIGN KEY (proposal_id) REFERENCES proposal (id) ON DELETE RESTRICT,
    FOREIGN KEY (decided_by) REFERENCES person (id) ON DELETE RESTRICT
);

CREATE TABLE proposal_assignee (
    id            INTEGER PRIMARY KEY,
    proposal_id   INTEGER NOT NULL,
    assignee_text TEXT NOT NULL,
    resolution    TEXT NOT NULL,
    person_id     INTEGER,
    resolved_via  TEXT,
    CHECK (resolution IN ('RESOLVED', 'UNRESOLVED', 'FLAGGED')),
    CHECK (resolved_via IS NULL OR resolved_via IN ('MENTION', 'NAME_MATCH')),
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
    FOREIGN KEY (proposal_id) REFERENCES proposal (id) ON DELETE RESTRICT,
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT
);

CREATE TABLE message_mention (
    id          INTEGER PRIMARY KEY,
    message_id  INTEGER NOT NULL,
    mentioned_discord_user_id TEXT NOT NULL,
    position    INTEGER,
    CHECK (length(trim(mentioned_discord_user_id)) > 0),
    CHECK (position IS NULL OR position >= 0),
    FOREIGN KEY (message_id) REFERENCES source_message (id) ON DELETE RESTRICT
);
