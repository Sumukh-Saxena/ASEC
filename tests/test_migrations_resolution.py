"""Schema-level tests for the 0012_resolution_and_mentions migration (Slice A4).

Verifies: migration application/idempotency; review-decision storage per
FR-REV-05/FR-REV-07 (one decision per proposal, sender decided, AC-04);
assignee-resolution recording per FR-PRP-10 (assignee as written, resolution
state, resolved person and resolution route); and mention evidence per
FR-ING-02 (message mentions with opaque Discord user IDs). AC-02 (several
proposals for the same primary message in one run) is restated explicitly.

Storage-only and deliberately not enforced here: who may accept or reject
(authorization, service layer); how assignees are resolved (AI + backend,
service layer); mention extraction timing (ingestion/service layer).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations

_RESOLUTION_TABLES = {
    "proposal_decision",
    "proposal_assignee",
    "message_mention",
}

_GENERATED_AT = "2026-05-01T11:05:00Z"
_DECIDED_AT = "2026-05-01T11:30:00Z"


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def _add_vertical(connection: sqlite3.Connection, vertical_id: int = 1) -> int:
    connection.execute(
        "INSERT INTO vertical (id, name) VALUES (?, ?)", (vertical_id, f"vertical-{vertical_id}")
    )
    return vertical_id


def _add_group(connection: sqlite3.Connection, group_id: int = 1, vertical_id: int = 1) -> int:
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (?, ?, ?)",
        (group_id, f"ch-{group_id}", vertical_id),
    )
    return group_id


def _add_message(
    connection: sqlite3.Connection,
    message_id: int = 1,
    group_id: int = 1,
    content: str | None = None,
) -> int:
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " received_at, content) VALUES (?, 'discord', ?, ?, '2026-05-01T10:00:00Z', ?)",
        (message_id, str(message_id), group_id, content),
    )
    return message_id


def _add_bucket(connection: sqlite3.Connection, bucket_id: int = 1, group_id: int = 1) -> int:
    connection.execute(
        "INSERT INTO bucket (id, group_id) VALUES (?, ?)", (bucket_id, group_id)
    )
    return bucket_id


def _join_message(
    connection: sqlite3.Connection,
    bucket_id: int = 1,
    message_id: int = 1,
    group_id: int = 1,
) -> int:
    connection.execute(
        "INSERT INTO bucket_message (bucket_id, message_id, group_id) VALUES (?, ?, ?)",
        (bucket_id, message_id, group_id),
    )
    return message_id


def _add_person(
    connection: sqlite3.Connection,
    name: str = "reviewer",
    role: str = "AH",
    discord_user_id: str | None = None,
) -> int:
    # PB-5: mandatory unique synthetic phone/roll_number derived from the next id.
    next_id = int(
        connection.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM person").fetchone()[0]
    )
    cursor = connection.execute(
        "INSERT INTO person (id, name, phone, role, discord_user_id, roll_number)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            next_id,
            name,
            f"+919800007{next_id:03d}",
            role,
            discord_user_id,
            f"RES-{next_id:03d}",
        ),
    )
    return int(cursor.lastrowid or 0)


def _attempt(
    connection: sqlite3.Connection,
    bucket_id: int = 1,
    *,
    stage: str = "proposal_generation",
    outcome: str = "SUCCEEDED",
    started_at: str = "2026-05-01T11:00:00Z",
) -> int:
    cursor = connection.execute(
        "INSERT INTO processing_attempt (stage, bucket_id, started_at, outcome)"
        " VALUES (?, ?, ?, ?)",
        (stage, bucket_id, started_at, outcome),
    )
    return int(cursor.lastrowid or 0)


def _proposal(
    connection: sqlite3.Connection,
    attempt_id: int = 1,
    bucket_id: int = 1,
    primary_message_id: int = 1,
    **fields: object,
) -> int:
    record: dict[str, object] = {
        "attempt_id": attempt_id,
        "bucket_id": bucket_id,
        "primary_message_id": primary_message_id,
        "source_vertical_id": 1,
        "ptype": "TASK",
        "assignment_mode": "EXPLICIT",
        "task_type": "INTERNAL",
        "structurally_valid": 1,
        "generated_at": _GENERATED_AT,
        "model_id": "model-p",
        "model_version": "1.0.0",
        "prompt_version": "prompt-1",
    }
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO proposal ({', '.join(record)}) VALUES ({placeholders})", values
    )
    return int(cursor.lastrowid or 0)


def _seed_generated_bucket(connection: sqlite3.Connection) -> dict[str, int]:
    _add_vertical(connection)
    _add_group(connection)
    _add_bucket(connection)
    for message_id in range(1, 6):
        _add_message(connection, message_id=message_id, group_id=1)
        _join_message(connection, bucket_id=1, message_id=message_id)
    attempt_id = _attempt(connection)
    return {"attempt_id": attempt_id, "bucket_id": 1, "message_id": 1}


# --- migration application ------------------------------------------------------


def test_default_migrations_create_resolution_tables(conn: sqlite3.Connection) -> None:
    assert _RESOLUTION_TABLES <= _table_names(conn)


def test_reapplying_the_migrations_is_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "test.db")
    try:
        apply_migrations(connection)
        apply_migrations(connection)
        rows = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        # Phase 7 added the 0016-0018 schema window and Phase 9 added 0019-0021;
        # the shared count expectation bumps 15 -> 21 with them (PB-3/PB-5/PB-6).
        assert [row["version"] for row in rows] == list(range(1, 22))
    finally:
        connection.close()


# --- proposal_decision (FR-REV-05, FR-REV-07, AC-04) -----------------------------


@pytest.mark.parametrize("decision", ["ACCEPTED", "REJECTED"])
def test_decision_tokens_recordable(conn: sqlite3.Connection, decision: str) -> None:
    _seed_generated_bucket(conn)
    person_id = _add_person(conn)
    proposal_id = _proposal(conn)
    cursor = conn.execute(
        "INSERT INTO proposal_decision (proposal_id, decision, decided_by, decided_at)"
        " VALUES (?, ?, ?, ?)",
        (proposal_id, decision, person_id, _DECIDED_AT),
    )
    assert cursor.lastrowid is not None


def test_decision_invalid_token_rejected(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    person_id = _add_person(conn)
    _proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_decision (proposal_id, decision, decided_by, decided_at)"
            " VALUES (?, 'MAYBE', ?, ?)",
            (1, person_id, _DECIDED_AT),
        )


def test_one_decision_per_proposal(conn: sqlite3.Connection) -> None:
    """The schema keeps exactly one decision row per proposal. The owner
    decision (2026-10-20) stores only the latest review decision: a later
    permitted decision overwrites the existing row rather than being added
    as a second row, and no decision history is kept (FR-REV-05/FR-REV-07,
    AC-04 recording shape). A second INSERT must therefore be rejected."""
    _seed_generated_bucket(conn)
    person_id = _add_person(conn)
    _proposal(conn)
    conn.execute(
        "INSERT INTO proposal_decision (proposal_id, decision, decided_by, decided_at)"
        " VALUES (?, 'ACCEPTED', ?, ?)",
        (1, person_id, _DECIDED_AT),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_decision (proposal_id, decision, decided_by, decided_at)"
            " VALUES (?, 'REJECTED', ?, ?)",
            (1, person_id, _DECIDED_AT),
        )


def test_decision_decider_must_be_a_known_person(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_decision (proposal_id, decision, decided_by, decided_at)"
            " VALUES (?, 'ACCEPTED', ?, ?)",
            (1, 9999, _DECIDED_AT),
        )


def test_decision_deletion_restricted_in_both_directions(
    conn: sqlite3.Connection,
) -> None:
    _seed_generated_bucket(conn)
    person_id = _add_person(conn)
    proposal_id = _proposal(conn)
    conn.execute(
        "INSERT INTO proposal_decision (proposal_id, decision, decided_by, decided_at)"
        " VALUES (?, 'REJECTED', ?, ?)",
        (proposal_id, person_id, _DECIDED_AT),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM proposal WHERE id = ?", (proposal_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM person WHERE id = ?", (person_id,))


def test_decision_note_is_optional_but_not_blank(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    person_id = _add_person(conn)
    first = _proposal(conn)
    second = _proposal(conn, ptype="ANNOUNCEMENT", assignment_mode="NONE")
    conn.execute(
        "INSERT INTO proposal_decision (proposal_id, decision, decided_by, decided_at, note)"
        " VALUES (?, 'ACCEPTED', ?, ?, 'edited before accepting')",
        (first, person_id, _DECIDED_AT),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_decision (proposal_id, decision, decided_by, decided_at, note)"
            " VALUES (?, 'ACCEPTED', ?, ?, '   ')",
            (second, person_id, _DECIDED_AT),
        )


# --- proposal_assignee (FR-PRP-10) -----------------------------------------------


def test_assignee_resolution_states_recordable(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    resolver_id = _add_person(conn, name="resolved", discord_user_id="111")
    rows = [
        ("one mention", "RESOLVED", resolver_id, "MENTION"),
        ("same name twice", "UNRESOLVED", None, None),
        ("nobody", "FLAGGED", None, None),
    ]
    for text, resolution, person_id, via in rows:
        conn.execute(
            "INSERT INTO proposal_assignee (proposal_id, assignee_text, resolution,"
            " person_id, resolved_via) VALUES (?, ?, ?, ?, ?)",
            (proposal_id, text, resolution, person_id, via),
        )
        if resolution == "RESOLVED":
            assert via in ("MENTION", "NAME_MATCH")


def test_assignee_resolved_requires_person_and_via(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_assignee (proposal_id, assignee_text, resolution)"
            " VALUES (?, 'someone', 'RESOLVED')",
            (1,),
        )


def test_assignee_unresolved_or_flagged_cannot_carry_person(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    resolver_id = _add_person(conn)
    _proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_assignee (proposal_id, assignee_text, resolution,"
            " person_id, resolved_via) VALUES (?, 'someone', 'FLAGGED', ?, 'MENTION')",
            (1, resolver_id),
        )


def test_assignee_invalid_via_token_rejected(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    resolver_id = _add_person(conn)
    _proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_assignee (proposal_id, assignee_text, resolution,"
            " person_id, resolved_via) VALUES (?, 'someone', 'RESOLVED', ?, 'GUESS')",
            (1, resolver_id),
        )


def test_assignee_unknown_person_rejected(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_assignee (proposal_id, assignee_text, resolution,"
            " person_id, resolved_via) VALUES (?, 'someone', 'RESOLVED', 9999, 'MENTION')",
            (1,),
        )


# --- message_mention (FR-ING-02) --------------------------------------------------


def test_mentions_stored_with_position(conn: sqlite3.Connection) -> None:
    _add_vertical(conn)
    _add_group(conn)
    _add_message(conn, message_id=1, content="hey @111 and @222 look")
    conn.execute(
        "INSERT INTO message_mention (message_id, mentioned_discord_user_id, position)"
        " VALUES (?, ?, ?)",
        (1, "111", 4),
    )
    conn.execute(
        "INSERT INTO message_mention (message_id, mentioned_discord_user_id, position)"
        " VALUES (?, ?, ?)",
        (1, "222", 13),
    )
    rows = conn.execute(
        "SELECT mentioned_discord_user_id, position FROM message_mention"
        " WHERE message_id = 1 ORDER BY position"
    ).fetchall()
    assert [(row["mentioned_discord_user_id"], row["position"]) for row in rows] == [
        ("111", 4),
        ("222", 13),
    ]


def test_duplicate_mentions_preserved_as_evidence(conn: sqlite3.Connection) -> None:
    """DR-03: source evidence is preserved; repeats are additional rows."""
    _add_vertical(conn)
    _add_group(conn)
    _add_message(conn, message_id=1, content="@111 @111 again")
    for _ in range(2):
        conn.execute(
            "INSERT INTO message_mention (message_id, mentioned_discord_user_id)"
            " VALUES (?, ?)",
            (1, "111"),
        )
    count = conn.execute("SELECT COUNT(*) FROM message_mention").fetchone()[0]
    assert count == 2


def test_mention_unknown_message_rejected(conn: sqlite3.Connection) -> None:
    _add_vertical(conn)
    _add_group(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO message_mention (message_id, mentioned_discord_user_id)"
            " VALUES (?, ?)",
            (9999, "111"),
        )


def test_mention_blank_user_id_rejected(conn: sqlite3.Connection) -> None:
    _add_vertical(conn)
    _add_group(conn)
    _add_message(conn, message_id=1)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO message_mention (message_id, mentioned_discord_user_id)"
            " VALUES (?, '    ')",
            (1,),
        )


# --- AC-02: several proposals for one primary message in one run -----------------


def test_ac02_two_proposals_from_same_primary_message_same_run(
    conn: sqlite3.Connection,
) -> None:
    """AC-02: a task and an announcement for the same primary message in the
    same generating run are both stored as independent proposals."""
    _seed_generated_bucket(conn)
    task_id = _proposal(conn, primary_message_id=1, ptype="TASK")
    announcement_id = _proposal(
        conn, primary_message_id=1, ptype="ANNOUNCEMENT", assignment_mode="NONE"
    )
    rows = conn.execute(
        "SELECT id, attempt_id, bucket_id, primary_message_id, ptype FROM proposal"
        " ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["id"] == task_id and rows[0]["ptype"] == "TASK"
    assert rows[1]["id"] == announcement_id and rows[1]["ptype"] == "ANNOUNCEMENT"
    assert all(row["attempt_id"] == 1 for row in rows)
    assert all(row["primary_message_id"] == 1 for row in rows)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
