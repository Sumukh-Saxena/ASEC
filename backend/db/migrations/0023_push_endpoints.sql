-- One row per active device token. Tokens are secrets: never logged, never in
-- an error string. Legacy/duplicate tokens are deactivated, not deleted, so a
-- delivery attempt can still explain itself (FR-AUD/NFR-AUD-03).
CREATE TABLE push_endpoint (
    id           INTEGER PRIMARY KEY,
    person_id    INTEGER NOT NULL,
    provider     TEXT NOT NULL,
    token        TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT,
    CHECK (provider IN ('FCM')),
    CHECK (length(trim(token)) > 0),
    CHECK (active IN (0, 1)),
    CHECK (length(trim(created_at)) > 0),
    CHECK (last_seen_at IS NULL OR length(trim(last_seen_at)) > 0),
    UNIQUE (token),
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT
);

CREATE INDEX ix_push_endpoint_active
    ON push_endpoint (person_id)
    WHERE active = 1;

-- In-app read marker. Absence means unread; the row is never updated, so a
-- read receipt is immutable evidence (NFR-AUD-03).
CREATE TABLE notification_read (
    notification_id INTEGER PRIMARY KEY,
    read_at         TEXT NOT NULL,
    CHECK (length(trim(read_at)) > 0),
    FOREIGN KEY (notification_id)
        REFERENCES notification (id) ON DELETE RESTRICT
);
