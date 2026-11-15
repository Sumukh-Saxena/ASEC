-- Owner decision A2: idempotency keys for mutating API operations.
-- One stored response per (person, route, key); the unique key includes the
-- person so one caller can never replay another caller's response. Only 2xx
-- responses are stored (the store function enforces that), and an expired row
-- is treated as absent. No secret or request body is stored here: response_body
-- is the exact JSON the same person already received.
CREATE TABLE idempotency_key (
    id              INTEGER PRIMARY KEY,
    person_id       INTEGER NOT NULL,
    route_key       TEXT NOT NULL,
    key             TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    response_status INTEGER NOT NULL,
    response_body   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    UNIQUE (person_id, route_key, key),
    CHECK (length(trim(route_key)) > 0),
    CHECK (length(key) BETWEEN 16 AND 64),
    CHECK (key NOT GLOB '*[^A-Za-z0-9._:-]*'),
    CHECK (length(request_hash) = 64),
    CHECK (response_status BETWEEN 100 AND 599),
    CHECK (length(response_body) > 0),
    CHECK (length(trim(created_at)) > 0),
    CHECK (length(trim(expires_at)) > 0),
    FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE RESTRICT
);

-- The cleanup function deletes expired rows in bounded batches.
CREATE INDEX ix_idempotency_key_expiry ON idempotency_key (expires_at, id);
