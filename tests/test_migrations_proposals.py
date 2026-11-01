"""Schema-level tests for the 0005_proposals migration (Slice 2.6).

Verifies: migration application/idempotency/failure isolation; proposal
candidate storage per FR-PRP-01..13 (§3.4) with SRS-verbatim vocabulary
CHECKs (FR-PRP-06/08/09) and the FR-REV-01 review-state vocabulary;
provenance pinning via composite foreign keys (FR-PRP-12, NFR-AUD-01/02);
context/attribute/affected-vertical links (FR-PRP-03/07/12); per-edit
revisions with edit provenance (FR-REV-03/09).

Storage-only and deliberately not enforced here: proposal-model execution;
bucket state (an OPEN bucket is not rejected by the schema — closed-bucket
selection is service behavior per FR-PRP-01/FR-BKT-12); review/accept
endpoints or authorization; exactly-one-task-or-announcement per accepted
proposal (FR-REV-05 cross-table XOR lives on the 2.7/2.10 tables plus the
service layer); assignee resolution (FR-PRP-10 Phase 3); notifications.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.db.connection import connect, transaction
from backend.db.migrations import MIGRATIONS_DIR, apply_migrations

_PROPOSAL_TABLES = {
    "proposal",
    "proposal_attribute",
    "proposal_affected_vertical",
    "proposal_context_message",
    "proposal_revision",
    "revision_attribute",
    "revision_affected_vertical",
}

_GENERATED_AT = "2026-05-01T11:05:00Z"
_DECISION_RUN_AT = "2026-05-01T11:00:00Z"


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def _versions(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [row["version"] for row in rows]


def _applied_at(connection: sqlite3.Connection, version: int) -> str:
    row = connection.execute(
        "SELECT applied_at FROM schema_migrations WHERE version = ?", (version,)
    ).fetchone()
    assert row is not None
    return str(row["applied_at"])


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
) -> int:
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id, received_at)"
        " VALUES (?, 'discord', ?, ?, '2026-05-01T10:00:00Z')",
        (message_id, str(message_id), group_id),
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


def _add_person(connection: sqlite3.Connection) -> int:
    # PB-5: mandatory unique synthetic phone/roll_number for the roster row.
    cursor = connection.execute(
        "INSERT INTO person (name, phone, role, roll_number)"
        " VALUES ('reviewer', 'ph-reviewer', 'AH', 'roll-reviewer')"
    )
    return int(cursor.lastrowid or 0)


def _attempt(
    connection: sqlite3.Connection,
    bucket_id: int = 1,
    *,
    stage: str = "proposal_generation",
    outcome: str = "SUCCEEDED",
    started_at: str = _DECISION_RUN_AT,
    **fields: Any,
) -> int:
    columns = ["stage", "bucket_id", "started_at", "outcome"]
    values: list[Any] = [stage, bucket_id, started_at, outcome]
    for key, value in fields.items():
        columns.append(key)
        values.append(value)
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO processing_attempt ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    return int(cursor.lastrowid or 0)


def _proposal(
    connection: sqlite3.Connection,
    attempt_id: int = 1,
    bucket_id: int = 1,
    primary_message_id: int = 1,
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
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


def _row(connection: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row:
    row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    assert row is not None
    return row


def _revision(
    connection: sqlite3.Connection,
    proposal_id: int = 1,
    person_id: int = 1,
    *,
    revision_number: int = 1,
    changed_at: str = "2026-05-01T12:00:00Z",
    **fields: Any,
) -> int:
    record: dict[str, Any] = {
        "proposal_id": proposal_id,
        "revision_number": revision_number,
        "person_id": person_id,
        "changed_fields": '["description"]',
        "changed_at": changed_at,
        "assignment_mode": "EXPLICIT",
        "task_type": "INTERNAL",
    }
    record.update(fields)
    values = [record[column] for column in record]
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO proposal_revision ({', '.join(record)}) VALUES ({placeholders})", values
    )
    return int(cursor.lastrowid or 0)


def _seed_generated_bucket(connection: sqlite3.Connection) -> dict[str, int]:
    """Roster vertical + group + bucket holding messages 1-5 + a SUCCEEDED run attempt.

    Joining messages 2-5 up front keeps every later INSERT a valid foreign-key
    row shape, so vocabulary/CHECK violations surface as CHECK failures rather
    than being masked by foreign-key mismatches.
    """
    _add_vertical(connection)
    _add_group(connection)
    _add_bucket(connection)
    for message_id in range(1, 6):
        _add_message(connection, message_id=message_id, group_id=1)
        _join_message(connection, bucket_id=1, message_id=message_id)
    attempt_id = _attempt(connection)
    return {"attempt_id": attempt_id, "bucket_id": 1, "message_id": 1}


def test_default_migrations_create_proposal_tables(conn: sqlite3.Connection) -> None:
    assert _PROPOSAL_TABLES <= _table_names(conn)
    expected = {
        "processing_attempt",
        "relevance_decision",
        "relevance_correction",
        "bucket",
        "bucket_message",
    }
    assert expected <= _table_names(conn)
    assert {1, 2, 3, 4, 5} <= set(_versions(conn))


def test_reapplying_the_migrations_is_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "test.db")
    try:
        first = apply_migrations(connection)
        assert {1, 2, 3, 4, 5} <= set(first)
        at_5 = _applied_at(connection, 5)
        second = apply_migrations(connection)
        assert second == []
        assert _versions(connection) == sorted(set(_versions(connection)))
        assert _applied_at(connection, 5) == at_5
    finally:
        connection.close()


def test_migrations_recording_versions_are_ordered_and_include_0005(
    conn: sqlite3.Connection,
) -> None:
    versions = _versions(conn)
    assert versions == sorted(set(versions))
    assert {1, 2, 3, 4, 5} <= set(versions)


def test_failed_later_migration_preserves_earlier_migrations(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        (directory / path.name).write_bytes(path.read_bytes())
    (directory / "0900_seed.sql").write_text(
        "INSERT INTO vertical (id, name) VALUES (1, 'seed-vertical');\n", encoding="utf-8"
    )
    (directory / "0901_broken.sql").write_text(
        "CREATE TABLE ROLLBACK_CHECK (should_never_exist INTEGER PRIMARY KEY);\n"
        "INSERT INTO nonexistent_table (id) VALUES (1);\n",
        encoding="utf-8",
    )
    connection = connect(tmp_path / "test.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection, directory=directory)
        versions = _versions(connection)
        assert versions == sorted(set(versions))
        assert {1, 2, 3, 4, 5} <= set(versions)
        assert 900 in versions and 901 not in versions
        assert _PROPOSAL_TABLES <= _table_names(connection)
        assert "ROLLBACK_CHECK" not in _table_names(connection)
        assert connection.execute("SELECT COUNT(*) FROM vertical").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM proposal").fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.parametrize("ptype", ["TASK", "ANNOUNCEMENT"])
def test_proposal_type_tokens_recordable(conn: sqlite3.Connection, ptype: str) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn, ptype=ptype)
    assert _row(conn, "proposal", proposal_id)["ptype"] == ptype


def test_proposal_type_invalid_rejected(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    for bad in ("TODO", " TASK ", None):
        with pytest.raises(sqlite3.IntegrityError):
            _proposal(conn, ptype=bad)


@pytest.mark.parametrize(
    ("mode", "ptype"),
    [("EXPLICIT", "TASK"), ("OPEN", "TASK"), ("OPEN", "ANNOUNCEMENT"), ("NONE", "ANNOUNCEMENT")],
)
def test_proposal_assignment_mode_tokens_recordable(
    conn: sqlite3.Connection, mode: str, ptype: str
) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn, ptype=ptype, assignment_mode=mode)
    assert _row(conn, "proposal", proposal_id)["assignment_mode"] == mode


def test_proposal_assignment_mode_invalid_rejected(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    for bad in ("MAYBE", " EXPLICIT ", None):
        with pytest.raises(sqlite3.IntegrityError):
            _proposal(conn, assignment_mode=bad)


def test_proposal_none_mode_is_announcement_only(conn: sqlite3.Connection) -> None:
    # FR-PRP-08: "NONE (announcements only)" — a NONE assignment mode on a TASK
    # proposal is not storable. The SRS does not state the converse, so an
    # ANNOUNCEMENT carrying EXPLICIT/OPEN remains storable here.
    _seed_generated_bucket(conn)
    announcement = _proposal(conn, ptype="ANNOUNCEMENT", assignment_mode="NONE")
    assert _row(conn, "proposal", announcement)["assignment_mode"] == "NONE"
    with pytest.raises(sqlite3.IntegrityError):
        _proposal(conn, ptype="TASK", assignment_mode="NONE")
    open_announcement = _proposal(
        conn, primary_message_id=2, ptype="ANNOUNCEMENT", assignment_mode="OPEN"
    )
    assert _row(conn, "proposal", open_announcement)["assignment_mode"] == "OPEN"


@pytest.mark.parametrize("task_type", ["INTERNAL", "CROSS_VERTICAL"])
def test_proposal_task_type_tokens_recordable(conn: sqlite3.Connection, task_type: str) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn, task_type=task_type)
    assert _row(conn, "proposal", proposal_id)["task_type"] == task_type


def test_proposal_task_type_invalid_rejected(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    for bad in ("SHARED", " INTERNAL ", None):
        with pytest.raises(sqlite3.IntegrityError):
            _proposal(conn, task_type=bad)


def test_proposal_review_state_default_pending_and_tokens_recordable(
    conn: sqlite3.Connection,
) -> None:
    _seed_generated_bucket(conn)
    first = _proposal(conn)
    row = _row(conn, "proposal", first)
    assert row["review_state"] == "PENDING"
    for message_id, state in [(2, "EDITED"), (3, "ACCEPTED"), (4, "REJECTED")]:
        tracked = _proposal(conn, primary_message_id=message_id)
        conn.execute(
            "UPDATE proposal SET review_state = ? WHERE id = ?", (state, tracked)
        )
        assert _row(conn, "proposal", tracked)["review_state"] == state


def test_proposal_review_state_invalid_rejected(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _proposal(conn)
    for bad in ("DONE", " EDITED ", None):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE proposal SET review_state = ? WHERE id = 1", (bad,))


def test_proposal_description_and_assignee_text_domain(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(
        conn, description="Clean the gate", proposed_assignees_text="Arjun"
    )
    row = _row(conn, "proposal", proposal_id)
    assert row["description"] == "Clean the gate"
    assert row["proposed_assignees_text"] == "Arjun"
    bare = _proposal(conn, primary_message_id=2, description=None, proposed_assignees_text=None)
    assert _row(conn, "proposal", bare)["proposed_assignees_text"] is None
    for blank in ("", "  "):
        with pytest.raises(sqlite3.IntegrityError):
            _proposal(conn, primary_message_id=3, description=blank)
        with pytest.raises(sqlite3.IntegrityError):
            _proposal(conn, primary_message_id=4, proposed_assignees_text=blank)


def test_proposal_people_needed_domain(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    numbered = _proposal(conn, people_needed=5)
    assert _row(conn, "proposal", numbered)["people_needed"] == 5
    unlimited = _proposal(conn, primary_message_id=2, people_needed=None, people_needed_unlimited=1)
    assert _row(conn, "proposal", unlimited)["people_needed"] is None
    unfilled = _proposal(conn, primary_message_id=3)
    assert _row(conn, "proposal", unfilled)["people_needed_unlimited"] == 0
    for bad in (0, -2):
        with pytest.raises(sqlite3.IntegrityError):
            _proposal(conn, primary_message_id=4, people_needed=bad, people_needed_unlimited=0)
    with pytest.raises(sqlite3.IntegrityError):
        _proposal(conn, primary_message_id=5, people_needed=3, people_needed_unlimited=1)


def test_proposal_structurally_valid_boolean_domain(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    valid = _proposal(conn, structurally_valid=1)
    invalid = _proposal(conn, primary_message_id=2, structurally_valid=0)
    assert _row(conn, "proposal", valid)["structurally_valid"] == 1
    assert _row(conn, "proposal", invalid)["structurally_valid"] == 0
    for bad in (2, None):
        with pytest.raises(sqlite3.IntegrityError):
            _proposal(conn, primary_message_id=3, structurally_valid=bad)


def test_proposal_source_vertical_required(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn)
    assert _row(conn, "proposal", proposal_id)["source_vertical_id"] == 1
    with pytest.raises(sqlite3.IntegrityError):
        _proposal(conn, primary_message_id=2, source_vertical_id=999)
    with pytest.raises(sqlite3.IntegrityError):
        _proposal(conn, primary_message_id=3, source_vertical_id=None)


def test_proposal_suggested_responsible_vertical_optional(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    none_candidate = _proposal(conn)
    assert _row(conn, "proposal", none_candidate)["suggested_responsible_vertical_id"] is None
    linked = _proposal(conn, primary_message_id=2, suggested_responsible_vertical_id=1)
    assert _row(conn, "proposal", linked)["suggested_responsible_vertical_id"] == 1
    with pytest.raises(sqlite3.IntegrityError):
        _proposal(conn, primary_message_id=3, suggested_responsible_vertical_id=999)


def test_proposal_provenance_required_and_verbatim(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(
        conn,
        generated_at="2026-05-01T09:07:11Z",
        model_id="proposal-1",
        model_version="2.1",
        prompt_version="cfg-x",
    )
    row = _row(conn, "proposal", proposal_id)
    assert row["generated_at"] == "2026-05-01T09:07:11Z"
    assert row["model_id"] == "proposal-1"
    assert row["model_version"] == "2.1"
    assert row["prompt_version"] == "cfg-x"
    for fields in (
        {"generated_at": ""},
        {"generated_at": " "},
        {"model_id": ""},
        {"model_version": " "},
        {"prompt_version": "  "},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            _proposal(conn, primary_message_id=2, **fields)


def test_proposal_attempt_bucket_pairing_accepts_valid(conn: sqlite3.Connection) -> None:
    context = _seed_generated_bucket(conn)
    proposal_id = _proposal(conn, attempt_id=context["attempt_id"], bucket_id=1)
    row = _row(conn, "proposal", proposal_id)
    assert (row["attempt_id"], row["bucket_id"]) == (context["attempt_id"], 1)


def test_proposal_attempt_bucket_pairing_rejects_mismatch(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _add_group(conn, group_id=2)
    _add_bucket(conn, bucket_id=2, group_id=2)
    foreign_attempt = _attempt(conn, bucket_id=2, stage="proposal_generation")
    with pytest.raises(sqlite3.IntegrityError):
        _proposal(conn, attempt_id=foreign_attempt)


def test_proposal_primary_message_must_be_bucket_member_accepts(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    proposal_id = _proposal(conn, primary_message_id=2)
    row = _row(conn, "proposal", proposal_id)
    assert row["primary_message_id"] == 2


def test_proposal_primary_message_membership_rejects_mismatches(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _add_message(conn, message_id=9, group_id=1)
    with pytest.raises(sqlite3.IntegrityError):
        _proposal(conn, primary_message_id=9)
    _add_vertical(conn, vertical_id=2)
    _add_group(conn, group_id=2, vertical_id=2)
    _add_message(conn, message_id=7, group_id=2)
    _add_bucket(conn, bucket_id=2, group_id=2)
    _join_message(conn, bucket_id=2, message_id=7, group_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        _proposal(conn, primary_message_id=7)
    with pytest.raises(sqlite3.IntegrityError):
        _proposal(conn, primary_message_id=999)


def test_proposals_from_one_run_are_independent(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    good = _proposal(conn, primary_message_id=1, structurally_valid=1)
    broken = _proposal(conn, primary_message_id=1, structurally_valid=0)
    rows = conn.execute("SELECT id FROM proposal ORDER BY id").fetchall()
    assert [row["id"] for row in rows] == [good, broken]


def test_zero_proposal_run_is_valid(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    assert conn.execute("SELECT COUNT(*) FROM proposal").fetchone()[0] == 0


def test_retry_attempt_may_generate_again(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    first = _proposal(conn)
    second_attempt = _attempt(conn)
    second = _proposal(conn, attempt_id=second_attempt)
    rows = conn.execute("SELECT id, attempt_id FROM proposal ORDER BY id").fetchall()
    assert rows[0]["id"] == first and rows[0]["attempt_id"] == 1
    assert rows[1]["id"] == second and rows[1]["attempt_id"] == second_attempt


def _add_context(
    connection: sqlite3.Connection,
    proposal_id: int = 1,
    bucket_id: int = 1,
    message_id: int = 2,
) -> None:
    connection.execute(
        "INSERT INTO proposal_context_message (proposal_id, bucket_id, message_id)"
        " VALUES (?, ?, ?)",
        (proposal_id, bucket_id, message_id),
    )


def test_context_links_accept_members(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _proposal(conn)
    _add_context(conn, message_id=2)
    _add_context(conn, message_id=3)
    assert conn.execute("SELECT COUNT(*) FROM proposal_context_message").fetchone()[0] == 2


def test_context_membership_rejects_non_members(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _add_message(conn, message_id=9, group_id=1)
    _proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_context(conn, message_id=9)
    _add_vertical(conn, vertical_id=2)
    _add_group(conn, group_id=2, vertical_id=2)
    _add_message(conn, message_id=7, group_id=2)
    _add_bucket(conn, bucket_id=2, group_id=2)
    _join_message(conn, bucket_id=2, message_id=7, group_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        _add_context(conn, message_id=7)


def test_context_unknown_references_rejected(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_context(conn, message_id=999)
    with pytest.raises(sqlite3.IntegrityError):
        _add_context(conn, proposal_id=999)


def test_context_message_unique_per_proposal(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    first = _proposal(conn)
    second = _proposal(conn)
    _add_context(conn, proposal_id=first, message_id=2)
    _add_context(conn, proposal_id=second, message_id=2)
    assert conn.execute("SELECT COUNT(*) FROM proposal_context_message").fetchone()[0] == 2


def test_proposal_affected_vertical_links(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    conn.execute("INSERT INTO vertical (id, name) VALUES (2, 'another-vertical')")
    _proposal(conn)
    conn.execute(
        "INSERT INTO proposal_affected_vertical (proposal_id, vertical_id) VALUES (1, 2)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_affected_vertical (proposal_id, vertical_id) VALUES (1, 2)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_affected_vertical (proposal_id, vertical_id) VALUES (1, 999)"
        )
    conn.execute(
        "INSERT INTO proposal_affected_vertical (proposal_id, vertical_id) VALUES (1, 1)"
    )
    rows = conn.execute("SELECT vertical_id FROM proposal_affected_vertical").fetchall()
    assert sorted(row["vertical_id"] for row in rows) == [1, 2]


def test_proposal_attribute_domain(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _proposal(conn)
    conn.execute(
        "INSERT INTO proposal_attribute (proposal_id, attr_key, attr_value)"
        " VALUES (1, 'venue', 'Gate 3')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_attribute (proposal_id, attr_key, attr_value)"
            " VALUES (1, 'venue', 'Gate 3')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_attribute (proposal_id, attr_key, attr_value)"
            " VALUES (1, ' ', 'x')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_attribute (proposal_id, attr_key, attr_value)"
            " VALUES (999, 'key', 'val')"
        )
    conn.execute(
        "INSERT INTO proposal_attribute (proposal_id, attr_key, attr_value)"
        " VALUES (1, 'time', NULL)"
    )
    rows = conn.execute("SELECT attr_key, attr_value FROM proposal_attribute").fetchall()
    assert {row["attr_key"] for row in rows} == {"venue", "time"}


def test_revision_minimal_snapshot_and_sequence(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _proposal(conn)
    person_id = _add_person(conn)
    revision_id = _revision(conn, person_id=person_id, revision_number=1)
    row = _row(conn, "proposal_revision", revision_id)
    assert row["proposal_id"] == 1
    assert row["person_id"] == person_id
    assert row["changed_fields"] == '["description"]'
    assert row["changed_at"] == "2026-05-01T12:00:00Z"
    second = _revision(
        conn, person_id=person_id, revision_number=2, changed_at="2026-05-01T13:00:00Z"
    )
    assert second != revision_id
    with pytest.raises(sqlite3.IntegrityError):
        _revision(conn, person_id=person_id, revision_number=2)


def test_revision_references_rejected(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _proposal(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _revision(conn, person_id=999)
    with pytest.raises(sqlite3.IntegrityError):
        _revision(conn, proposal_id=999)


def test_revision_changed_fields_required(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _add_person(conn)
    _proposal(conn)
    for blank in ("", "  ", None):
        with pytest.raises(sqlite3.IntegrityError):
            _revision(conn, changed_fields=blank)


def test_revision_snapshot_independent_original_unchanged(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    person_id = _add_person(conn)
    _proposal(conn, description="Generated", proposed_assignees_text=None)
    before = _row(conn, "proposal", 1)
    revision_id = _revision(
        conn,
        person_id=person_id,
        description="Edited description",
        proposed_assignees_text="Arjun",
        people_needed=4,
    )
    after = _row(conn, "proposal", 1)
    assert tuple(after) == tuple(before)
    row = _row(conn, "proposal_revision", revision_id)
    assert row["description"] == "Edited description"
    assert row["people_needed"] == 4


def test_revision_multiple_edits_per_proposal(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _add_person(conn)
    _proposal(conn)
    _revision(conn, revision_number=1, changed_at="2026-05-01T12:00:00Z")
    _revision(conn, revision_number=2, changed_at="2026-05-01T13:00:00Z")
    rows = conn.execute("SELECT COUNT(*) FROM proposal_revision").fetchone()
    assert rows[0] == 2


def test_revision_snapshot_children(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _add_person(conn)
    conn.execute("INSERT INTO vertical (id, name) VALUES (2, 'another-vertical')")
    _proposal(conn)
    revision_id = _revision(conn)
    conn.execute(
        "INSERT INTO revision_attribute (revision_id, attr_key, attr_value)"
        " VALUES (?, 'time', '18:30')",
        (revision_id,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO revision_attribute (revision_id, attr_key, attr_value)"
            " VALUES (?, 'time', '18:30')",
            (revision_id,),
        )
    conn.execute(
        "INSERT INTO revision_attribute (revision_id, attr_key, attr_value)"
        " VALUES (?, 'venue', NULL)",
        (revision_id,),
    )
    conn.execute(
        "INSERT INTO revision_affected_vertical (revision_id, vertical_id)" " VALUES (?, 2)",
        (revision_id,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO revision_affected_vertical (revision_id, vertical_id) VALUES (?, 999)",
            (revision_id,),
        )
    attrs = conn.execute("SELECT attr_key FROM revision_attribute").fetchall()
    assert {row["attr_key"] for row in attrs} == {"time", "venue"}


def test_deletion_restricted_in_every_direction(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    person_id = _add_person(conn)
    _proposal(conn)
    _revision(conn, person_id=person_id)
    conn.execute(
        "INSERT INTO revision_attribute (revision_id, attr_key, attr_value)"
        " VALUES (1, 'venue', NULL)"
    )
    for statement, parameters in (
        ("DELETE FROM processing_attempt WHERE id = ?", (1,)),
        ("DELETE FROM bucket WHERE id = ?", (1,)),
        ("DELETE FROM source_message WHERE id = ?", (1,)),
        ("DELETE FROM proposal WHERE id = ?", (1,)),
        ("DELETE FROM proposal_revision WHERE id = ?", (1,)),
        ("DELETE FROM person WHERE id = ?", (person_id,)),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement, parameters)


def test_foreign_key_check_clean_after_mixed_workload(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _add_person(conn)
    _proposal(conn, description="Clean the gate", people_needed=3)
    conn.execute(
        "INSERT INTO proposal_affected_vertical (proposal_id, vertical_id) VALUES (1, 1)"
    )
    _add_context(conn, message_id=2)
    conn.execute(
        "INSERT INTO proposal_attribute (proposal_id, attr_key, attr_value)"
        " VALUES (1, 'venue', 'Gate 3')"
    )
    _revision(conn, revision_number=1, description="Clean the gate", people_needed=5)
    conn.execute(
        "INSERT INTO revision_affected_vertical (revision_id, vertical_id) VALUES (1, 1)"
    )
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_duplicating_revisions_across_connections_keeps_one_row(conn: sqlite3.Connection) -> None:
    _seed_generated_bucket(conn)
    _add_person(conn)
    _proposal(conn)
    _revision(conn)
    revision_number = 1

    database_file = conn.execute("PRAGMA database_list").fetchone()[-1]
    second = connect(database_file)
    try:
        assert {1, 2, 3, 4, 5} <= set(_versions(second))
        assert _versions(second) == _versions(conn)
        with transaction(second):
            with pytest.raises(sqlite3.IntegrityError):
                _revision(second, revision_number=revision_number)
        assert conn.execute("SELECT COUNT(*) FROM proposal_revision").fetchone()[0] == 1
    finally:
        second.close()
