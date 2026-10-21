-- 0013_relevance_attempt_input.sql (Phase 4, approved F3/F4 corrective batch)
--
-- Durable canonical masked-input snapshot per committed classification
-- attempt (NFR-AUD-02 §6.2): what the classifier actually saw, captured at
-- write time as a self-contained artifact, so a later masking-ruleset or
-- policy edit can change what a NEW attempt sends but can never alter what
-- an OLD attempt stored.
--
-- masked_request is the canonical JSON serialization of the exact
-- RelevanceRequest passed to the stateless adapter (EIR-03): the whole
-- bucket context, the doubt policy text (FR-REL-02/03/04) and every message
-- with its NFR-SEC-04-masked content. Unmasked source content never appears
-- here — the original messages stay verbatim in source_message (DR-03) and
-- this artifact only records data the backend already put into the request.
-- Per the NFR-SEC-04 field list, sender display names and Discord user ids
-- are sent and therefore stored as the adapter received them (plain text);
-- only phone/email/government-ID-like numbers (and roll numbers, as a
-- project safeguard) are masked before capture.
--
-- version columns carry the masking-ruleset and classification-policy
-- identities at capture time (durable lineage markers; the artifact itself
-- is what makes reconstruction independent of live code). request_sha256
-- pins the integrity of the stored artifact. One row per attempt: UNIQUE
-- attempt_id (F3 invariant — snapshot iff a commit-accepted attempt row
-- exists, which the service enforces inside the same transaction; the
-- schema enforces the cardinality side).
--
-- Additive table; no changes to existing tables. Stage tokens are free
-- text and stored on the referenced attempt row, not duplicated here. The
-- Post-MVP review surfaces may read this table; capture/write remains the
-- relevance service's job (AI layer never writes the database, EIR-03).
--
-- Pre-0013 attempts (none exist in production; dev databases only) carry
-- no artifact — recorded as a compatibility note in docs/PHASE2_PLAN.md.

CREATE TABLE relevance_attempt_input (
    id                     INTEGER PRIMARY KEY,
    attempt_id             INTEGER NOT NULL UNIQUE,

    -- Durable version identity of the request contract at capture time.
    masking_ruleset_version TEXT NOT NULL,
    policy_version          TEXT NOT NULL,

    -- The artifact: canonical JSON of the masked whole-bucket request.
    masked_request          TEXT NOT NULL,
    request_sha256          TEXT NOT NULL,

    -- Backend-stamped capture time (same instant family as the attempt).
    captured_at             TEXT NOT NULL,

    CHECK (length(trim(masking_ruleset_version)) > 0),
    CHECK (length(trim(policy_version)) > 0),
    CHECK (length(trim(masked_request)) > 0),
    CHECK (length(trim(request_sha256)) > 0),
    CHECK (length(trim(captured_at)) > 0),

    -- DR-01/DR-02: the audit chain never breaks by deletion.
    FOREIGN KEY (attempt_id)
        REFERENCES processing_attempt (id) ON DELETE RESTRICT
);
