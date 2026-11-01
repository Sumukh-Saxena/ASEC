-- 0016_roster_integrity.sql: Rebuild person table with strict roles, mandatory phone/roll, and FK reports_to (PB-5)

PRAGMA defer_foreign_keys = ON;

CREATE TABLE person_new (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    discord_user_id TEXT NULL,
    phone TEXT NOT NULL,
    role TEXT NOT NULL,
    reports_to INTEGER NULL REFERENCES person_new (id) ON DELETE RESTRICT,
    active INTEGER NOT NULL DEFAULT 1,
    roll_number TEXT NOT NULL,
    CHECK (length(trim(name)) > 0),
    CHECK (role IN ('Admin', 'Head', 'AH', 'Volunteer')),
    CHECK (discord_user_id IS NULL OR length(trim(discord_user_id)) > 0),
    CHECK (length(trim(phone)) > 0),
    CHECK (length(trim(roll_number)) > 0),
    CHECK (active IN (0, 1)),
    UNIQUE (discord_user_id),
    UNIQUE (phone),
    UNIQUE (roll_number)
);

CREATE TRIGGER person_new_guards
BEFORE INSERT ON person_new
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.phone IS NULL OR length(trim(NEW.phone)) = 0
            THEN RAISE(ABORT, 'person_phone_required')
        WHEN NEW.roll_number IS NULL OR length(trim(NEW.roll_number)) = 0
            THEN RAISE(ABORT, 'person_roll_number_required')
        WHEN NEW.role NOT IN ('Admin', 'Head', 'AH', 'Volunteer')
            THEN RAISE(ABORT, 'person_role_known')
        WHEN (SELECT reports_to FROM person WHERE id = NEW.id) = CAST(NEW.id AS TEXT)
            THEN RAISE(ABORT, 'person_cannot_report_to_self')
        WHEN (SELECT reports_to FROM person WHERE id = NEW.id) IS NOT NULL
             AND (SELECT reports_to FROM person WHERE id = NEW.id) != ''
             AND NOT EXISTS (
                 SELECT 1 FROM person p2
                 WHERE CAST(p2.id AS TEXT) = (SELECT reports_to FROM person WHERE id = NEW.id)
             )
            THEN RAISE(ABORT, 'person_reports_to_must_name_a_person')
    END;
END;

INSERT INTO person_new (id, name, discord_user_id, phone, role, reports_to, active, roll_number)
SELECT id, name, discord_user_id, phone, role,
       CASE WHEN reports_to IS NULL OR reports_to = '' THEN NULL
            WHEN EXISTS (SELECT 1 FROM person p2 WHERE CAST(p2.id AS TEXT) = person.reports_to)
            THEN CAST(reports_to AS INTEGER) ELSE NULL END,
       active, roll_number
FROM person;

DROP TRIGGER person_new_guards;
DROP TABLE person;
ALTER TABLE person_new RENAME TO person;
