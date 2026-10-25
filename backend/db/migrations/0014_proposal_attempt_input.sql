-- 0014_proposal_attempt_input.sql (Phase 5, approved one-batch implementation)
--
-- Durable canonical masked-input artifact per committed proposal-generation
-- attempt (NFR-AUD-02 §6.2, mirroring 0013), extended by the owner's Phase 5
-- corrections C1/C2 so the proposal-stage input is reconstructable and the
-- model's output is retained:
--
-- masked_request is the ARTIFACT form of the canonical JSON request: the
-- dispatch serialization (what the adapter/model sees — per-request opaque
-- mention handles `@m1…`, no real Discord identifiers, sender display names
-- only — correction C1) plus one extra top-level `mention_handle_map` member
-- (handle → real Discord user ID) the backend needs to reconstruct the
-- resolution input (NFR-AUD-02) and to execute the MENTION route
-- (FR-PRP-10). Real IDs are backend-only audit data, exactly like the
-- unmasked-context precedent in 0013; request_sha256 pins the DISPATCH form
-- (the exact string handed to the adapter), not the artifact form.
--
-- raw_response retains the adapter's returned output after the same
-- mask_pii + size-cap treatment as attempt errors (correction C2), so
-- Tier-2-rejected output survives audit (FR-PRP-13) without introducing
-- PII. raw_response is NULL only when the adapter raised before returning.
-- response_sha256 is the sha256 of the exact stored text; the pair is
-- either both NULL or both set (CHECK below).
--
-- rejected_items records Tier-2 item rejections as JSON evidence with
-- reason codes ({"item_index", "reason_code", ...}); NULL when there were
-- none or the failure was at envelope level (the attempt's own error text
-- describes envelope failures). Reason codes are internal annotation, not
-- SRS state vocabulary.
--
-- version columns carry the masking-ruleset and proposal-policy identities
-- at capture time. One row per attempt: UNIQUE attempt_id (F3 invariant —
-- artifact iff a commit-accepted attempt exists, which the service enforces
-- inside the same transaction; the schema enforces the cardinality side).
--
-- Additive table; no changes to existing tables. Stage tokens are free
-- text and stored on the referenced attempt row, not duplicated here.
-- Capture/write remains the proposal service's job (AI layer never writes
-- the database, EIR-03).

CREATE TABLE proposal_attempt_input (
    id                      INTEGER PRIMARY KEY,
    attempt_id              INTEGER NOT NULL UNIQUE,

    -- Durable version identity of the request contract at capture time.
    masking_ruleset_version TEXT NOT NULL,
    policy_version          TEXT NOT NULL,

    -- Artifact form of the canonical masked request (incl. handle map).
    masked_request          TEXT NOT NULL,
    -- Integrity of the DISPATCH form (the string handed to the adapter).
    request_sha256          TEXT NOT NULL,

    -- C2 retention: adapter output after mask_pii + size cap; NULL iff the
    -- adapter raised before returning.
    raw_response            TEXT,
    -- sha256 over the full masked raw_response text before truncation (NULL iff above NULL).
    response_sha256         TEXT,
    -- C2/DC-20: 1 if raw_response exceeded MAX_RAW_RESPONSE_BYTES (65536) and was truncated, 0 otherwise.
    raw_response_truncated  INTEGER NOT NULL DEFAULT 0,

    -- C2 evidence: Tier-2 rejections with reason codes (JSON; NULL when none).
    rejected_items          TEXT,

    -- Backend-stamped capture time (same instant family as the attempt).
    captured_at             TEXT NOT NULL,

    CHECK (length(trim(masking_ruleset_version)) > 0),
    CHECK (length(trim(policy_version)) > 0),
    CHECK (length(trim(masked_request)) > 0),
    CHECK (length(trim(request_sha256)) > 0),
    CHECK (length(trim(captured_at)) > 0),
    CHECK (raw_response_truncated IN (0, 1)),
    CHECK (raw_response IS NOT NULL OR raw_response_truncated = 0),
    CHECK (
        (raw_response IS NULL AND response_sha256 IS NULL)
        OR (raw_response IS NOT NULL AND response_sha256 IS NOT NULL)
    ),

    -- DR-01/DR-02: the audit chain never breaks by deletion.
    FOREIGN KEY (attempt_id)
        REFERENCES processing_attempt (id) ON DELETE RESTRICT
);
