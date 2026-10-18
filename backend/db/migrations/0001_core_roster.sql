CREATE TABLE vertical (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    CHECK (name = trim(name) AND length(name) > 0),
    UNIQUE (name)
);

CREATE TABLE source_group (
    id                 INTEGER PRIMARY KEY,
    discord_channel_id TEXT NOT NULL,
    vertical_id        INTEGER NULL REFERENCES vertical(id) ON DELETE RESTRICT,
    CHECK (length(trim(discord_channel_id)) > 0),
    UNIQUE (discord_channel_id)
);

CREATE TABLE person (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    discord_user_id TEXT NULL,
    phone           TEXT NULL,
    role            TEXT NOT NULL,
    reports_to      TEXT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    roll_number     TEXT NULL,
    CHECK (length(trim(name)) > 0),
    CHECK (role = trim(role) AND length(role) > 0),
    CHECK (discord_user_id IS NULL OR length(trim(discord_user_id)) > 0),
    CHECK (phone          IS NULL OR length(trim(phone))          > 0),
    CHECK (reports_to     IS NULL OR length(trim(reports_to))     > 0),
    CHECK (roll_number    IS NULL OR length(trim(roll_number))    > 0),
    CHECK (active IN (0, 1)),
    UNIQUE (discord_user_id),
    UNIQUE (phone),
    UNIQUE (roll_number)
);

CREATE TABLE person_vertical (
    person_id   INTEGER NOT NULL REFERENCES person(id)   ON DELETE RESTRICT,
    vertical_id INTEGER NOT NULL REFERENCES vertical(id) ON DELETE RESTRICT,
    PRIMARY KEY (person_id, vertical_id)
);
