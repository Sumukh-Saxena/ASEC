-- OTP challenges store only a salted hash; the code is never stored.
CREATE TABLE otp_challenge (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL,
    code_salt   TEXT NOT NULL,
    code_hash   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    consumed_at TEXT,
    CHECK (length(trim(code_salt)) > 0),
    CHECK (length(trim(code_hash)) > 0),
    CHECK (length(trim(created_at)) > 0),
    CHECK (length(trim(expires_at)) > 0),
    CHECK (attempts >= 0),
    CHECK (consumed_at IS NULL OR length(trim(consumed_at)) > 0),
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT
);

CREATE INDEX ix_otp_challenge_person ON otp_challenge (person_id, created_at);

-- Session tokens are stored as SHA-256 hashes; the plaintext exists only in the
-- response to a successful verify.
CREATE TABLE auth_session (
    id         INTEGER PRIMARY KEY,
    person_id  INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    issued_at  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    CHECK (length(trim(token_hash)) > 0),
    CHECK (length(trim(issued_at)) > 0),
    CHECK (length(trim(expires_at)) > 0),
    CHECK (revoked_at IS NULL OR length(trim(revoked_at)) > 0),
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT
);

CREATE INDEX ix_auth_session_person ON auth_session (person_id, expires_at);

-- Rate-limit and audit evidence. The roll number itself is not stored; its hash
-- is enough to count attempts and cannot be used to sign in.
CREATE TABLE auth_attempt (
    id               INTEGER PRIMARY KEY,
    roll_number_hash TEXT NOT NULL,
    kind             TEXT NOT NULL,
    attempted_at     TEXT NOT NULL,
    succeeded        INTEGER NOT NULL,
    reason           TEXT,
    CHECK (length(trim(roll_number_hash)) > 0),
    CHECK (length(trim(kind)) > 0),
    CHECK (length(trim(attempted_at)) > 0),
    CHECK (succeeded IN (0, 1)),
    CHECK (reason IS NULL OR length(trim(reason)) > 0)
);

CREATE INDEX ix_auth_attempt_roll ON auth_attempt (roll_number_hash, attempted_at);
