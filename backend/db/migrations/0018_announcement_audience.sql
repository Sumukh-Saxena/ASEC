-- 0018_announcement_audience.sql: Rebuild announcement table with structured audience (PB-6)

PRAGMA defer_foreign_keys = ON;

CREATE TABLE announcement_new (
    id INTEGER PRIMARY KEY,
    proposal_id INTEGER NOT NULL,
    accepted_by INTEGER NOT NULL,
    accepted_at TEXT NOT NULL,
    audience_type TEXT NOT NULL,
    audience_vertical_id INTEGER NOT NULL,
    audience_ah_person_id INTEGER NULL,
    CHECK (audience_type IN ('VERTICAL', 'AH_VOLUNTEERS')),
    CHECK (audience_type != 'AH_VOLUNTEERS' OR audience_ah_person_id IS NOT NULL),
    CHECK (length(trim(accepted_at)) > 0),
    UNIQUE (proposal_id),
    FOREIGN KEY (proposal_id) REFERENCES proposal (id) ON DELETE RESTRICT,
    FOREIGN KEY (accepted_by) REFERENCES person (id) ON DELETE RESTRICT,
    FOREIGN KEY (audience_vertical_id) REFERENCES vertical (id) ON DELETE RESTRICT,
    FOREIGN KEY (audience_ah_person_id) REFERENCES person (id) ON DELETE RESTRICT
);

CREATE TRIGGER announcement_new_guards
BEFORE INSERT ON announcement_new
FOR EACH ROW
BEGIN
    SELECT CASE
        WHEN NEW.audience_type NOT IN ('VERTICAL', 'AH_VOLUNTEERS')
            THEN RAISE(ABORT, 'announcement_audience_type_known')
        WHEN NEW.audience_vertical_id IS NULL
            THEN RAISE(ABORT, 'announcement_audience_vertical_required')
        WHEN NEW.audience_type = 'AH_VOLUNTEERS' AND NEW.audience_ah_person_id IS NULL
            THEN RAISE(ABORT, 'announcement_ah_sender_required')
    END;
END;

INSERT INTO announcement_new (id, proposal_id, accepted_by, accepted_at,
                              audience_type, audience_vertical_id,
                              audience_ah_person_id)
SELECT a.id, a.proposal_id, a.accepted_by, a.accepted_at, a.audience_scope,
       a.audience_vertical_id,
       CASE WHEN a.audience_scope = 'AH_VOLUNTEERS'
            THEN m.sender_person_id ELSE NULL END
FROM announcement AS a
JOIN proposal AS p ON p.id = a.proposal_id
JOIN source_message AS m ON m.id = p.primary_message_id;

DROP TRIGGER announcement_new_guards;
DROP TABLE announcement;
ALTER TABLE announcement_new RENAME TO announcement;
