"""Schema-level tests for the 0004_processing_attempts migration (Slice 2.5).

Verifies: migration application, idempotency and failure isolation for the
processing attempt, relevance decision and relevance correction tables
(§4.1 "Processing attempt", "Relevance decision / correction"). Decision
vocabularies follow FR-REL-02 and §4.1 verbatim. Composite foreign keys
enforce that a decision's (bucket, message) is a real bucket_message row and
that its attempt belongs to the same bucket (FR-REL-01/FR-REL-05). Corrections
are separate records referencing decisions and roster persons (FR-REL-06).

Service-level behavior is out of scope and not claimed: append-only writes,
decision immutability guarantees, recency/effective-correction selection,
retry orchestration, and correction authorization. Stage tokens carry no
vocabulary: the storage layer accepts free-text stage names only.

A note on `test_failed_later_migration_preserves_earlier_migrations`: the
roster seed is recorded via a valid out-of-band 0900_seed migration (a high
fixture version that can never collide with a real migration) so that the data
whose preservation is asserted survives into the failed-run history.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.db.connection import connect, transaction
from backend.db.migrations import MIGRATIONS_DIR, apply_migrations

_ROSTER_TABLES = {"vertical", "source_group", "person", "person_vertical"}
_MESSAGE_TABLES = {"source_message", "message_reaction"}
_BUCKET_TABLES = {"bucket", "bucket_message"}
_PROCESSING_TABLES = {
    "processing_attempt",
    "relevance_decision",
    "relevance_correction",
}
_ATTEMPT_OUTCOMES = ["SUCCEEDED", "FAILED_RETRYABLE", "FAILED_PERMANENT"]

_STARTED_AT = "2026-05-01T10:30:00Z"
_DECIDED_AT = "2026-05-01T10:35:00Z"
_CORRECTED_AT = "2026-05-01T11:00:00Z"


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


def _add_group(connection: sqlite3.Connection, group_id: int | None = None) -> int:
    if group_id is None:
        cursor = connection.execute(
            "INSERT INTO source_group (discord_channel_id) VALUES ('ch-processing')"
        )
    else:
        cursor = connection.execute(
            "INSERT INTO source_group (id, discord_channel_id) VALUES (?, ?)",
            (group_id, f"ch-{group_id}"),
        )
    return int(cursor.lastrowid or 0)


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


def _add_bucket(
    connection: sqlite3.Connection,
    bucket_id: int | None = None,
    group_id: int = 1,
) -> int:
    if bucket_id is None:
        cursor = connection.execute(
            "INSERT INTO bucket (group_id) VALUES (?)", (group_id,)
        )
    else:
        cursor = connection.execute(
            "INSERT INTO bucket (id, group_id) VALUES (?, ?)", (bucket_id, group_id)
        )
    return int(cursor.lastrowid or bucket_id or 0)


def _join_message(
    connection: sqlite3.Connection,
    bucket_id: int = 1,
    message_id: int = 1,
    group_id: int = 1,
) -> int:
    cursor = connection.execute(
        "INSERT INTO bucket_message (bucket_id, message_id, group_id) VALUES (?, ?, ?)",
        (bucket_id, message_id, group_id),
    )
    return int(cursor.lastrowid or 0)


def _add_person(connection: sqlite3.Connection) -> int:
    # PB-5: canonical SRS role plus mandatory unique synthetic phone/roll_number.
    next_id = int(
        connection.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM person").fetchone()[0]
    )
    cursor = connection.execute(
        "INSERT INTO person (id, name, phone, role, roll_number)"
        " VALUES (?, 'reviewer', ?, 'Volunteer', ?)",
        (next_id, f"+919800010{next_id:03d}", f"ATT-{next_id:03d}"),
    )
    return int(cursor.lastrowid or 0)


def _attempt(
    connection: sqlite3.Connection,
    bucket_id: int = 1,
    *,
    stage: str = "relevance_classification",
    outcome: str = "SUCCEEDED",
    started_at: str = _STARTED_AT,
    **fields: Any,
) -> int:
    columns = ["stage", "bucket_id", "started_at", "outcome"]
    values: list[Any] = [stage, bucket_id, started_at, outcome]
    for key, value in fields.items():
        columns.append(key)
        values.append(value)
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO processing_attempt ({', '.join(columns)})"
        f" VALUES ({placeholders})",
        values,
    )
    return int(cursor.lastrowid or 0)


def _decision(
    connection: sqlite3.Connection,
    bucket_id: int = 1,
    message_id: int = 1,
    attempt_id: int = 1,
    *,
    decision: str = "RELEVANT",
    model_id: str = "model-a",
    model_version: str = "1.0.0",
    prompt_version: str = "prompt-1",
    decided_at: str = _DECIDED_AT,
    **fields: Any,
) -> int:
    columns = [
        "bucket_id",
        "message_id",
        "attempt_id",
        "decision",
        "model_id",
        "model_version",
        "prompt_version",
        "decided_at",
    ]
    values: list[Any] = [
        bucket_id,
        message_id,
        attempt_id,
        decision,
        model_id,
        model_version,
        prompt_version,
        decided_at,
    ]
    for key, value in fields.items():
        columns.append(key)
        values.append(value)
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO relevance_decision ({', '.join(columns)})"
        f" VALUES ({placeholders})",
        values,
    )
    return int(cursor.lastrowid or 0)


def _correction(
    connection: sqlite3.Connection,
    decision_id: int = 1,
    person_id: int = 1,
    *,
    corrected_decision: str = "IRRELEVANT",
    corrected_at: str = _CORRECTED_AT,
    **fields: Any,
) -> int:
    columns = ["decision_id", "person_id", "corrected_decision", "corrected_at"]
    values: list[Any] = [decision_id, person_id, corrected_decision, corrected_at]
    for key, value in fields.items():
        columns.append(key)
        values.append(value)
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO relevance_correction ({', '.join(columns)})"
        f" VALUES ({placeholders})",
        values,
    )
    return int(cursor.lastrowid or 0)


def _seed_bucket_with_messages(
    connection: sqlite3.Connection, *, message_count: int = 1
) -> tuple[int, list[int]]:
    _add_group(connection)
    bucket_id = _add_bucket(connection)
    message_ids = [
        _add_message(connection, message_id=index) for index in range(1, message_count + 1)
    ]
    for message_id in message_ids:
        _join_message(connection, bucket_id=bucket_id, message_id=message_id)
    return bucket_id, message_ids


def test_default_migrations_create_processing_tables(conn: sqlite3.Connection) -> None:
    assert _PROCESSING_TABLES <= _table_names(conn)
    assert _ROSTER_TABLES <= _table_names(conn)
    assert _MESSAGE_TABLES <= _table_names(conn)
    assert _BUCKET_TABLES <= _table_names(conn)
    assert {1, 2, 3, 4} <= set(_versions(conn))


def test_reapplying_the_migrations_is_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "test.db")
    try:
        first = apply_migrations(connection)
        assert {1, 2, 3, 4} <= set(first)
        at_4 = _applied_at(connection, 4)

        second = apply_migrations(connection)
        assert second == []
        assert _versions(connection) == sorted(set(_versions(connection)))
        assert _applied_at(connection, 4) == at_4
    finally:
        connection.close()


def test_migrations_recording_versions_are_ordered_and_include_processing_version(
    conn: sqlite3.Connection,
) -> None:
    versions = _versions(conn)
    assert versions == sorted(set(versions))
    assert {1, 2, 3, 4} <= set(versions)


def test_failed_later_migration_preserves_earlier_migrations(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        (directory / path.name).write_bytes(path.read_bytes())
    (directory / "0900_seed.sql").write_text(
        "INSERT INTO source_group (discord_channel_id) VALUES ('ch-seed');\n"
        "INSERT INTO person (name, phone, role, roll_number)"
        " VALUES ('Seed Human', 'ph-seed', 'Admin', 'roll-seed');\n",
        encoding="utf-8",
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
        tables = _table_names(connection)
        versions = _versions(connection)
        assert versions == sorted(set(versions))
        assert {1, 2, 3, 4, 5} <= set(versions)
        assert 900 in versions and 901 not in versions
        assert _PROCESSING_TABLES <= tables
        assert "ROLLBACK_CHECK" not in tables
        assert connection.execute("SELECT COUNT(*) FROM source_group").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM person").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM processing_attempt").fetchone()[0] == 0
    finally:
        connection.close()


def test_attempt_minimal_fields_and_null_profile(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    attempt_id = _attempt(conn)
    row = conn.execute(
        "SELECT * FROM processing_attempt WHERE id = ?", (attempt_id,)
    ).fetchone()
    assert row is not None
    assert row["stage"] == "relevance_classification"
    assert row["bucket_id"] == 1
    assert row["outcome"] == "SUCCEEDED"
    assert row["started_at"] == _STARTED_AT
    assert row["model_id"] is None
    assert row["model_version"] is None
    assert row["config_version"] is None
    assert row["finished_at"] is None
    assert row["error"] is None


@pytest.mark.parametrize("outcome", _ATTEMPT_OUTCOMES)
def test_attempt_outcome_tokens_accepted(conn: sqlite3.Connection, outcome: str) -> None:
    assert _ATTEMPT_OUTCOMES == ["SUCCEEDED", "FAILED_RETRYABLE", "FAILED_PERMANENT"]
    _add_group(conn)
    _add_bucket(conn)
    attempt_id = _attempt(conn, outcome=outcome)
    row = conn.execute(
        "SELECT outcome FROM processing_attempt WHERE id = ?", (attempt_id,)
    ).fetchone()
    assert row is not None
    assert row["outcome"] == outcome


def test_attempt_outcome_unknown_is_rejected(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(conn, outcome="DONE")


def test_attempt_outcome_null_is_rejected(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(conn, outcome=None)


def test_attempt_outcome_with_whitespace_is_rejected(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(conn, outcome=" FAILED_RETRYABLE ")


def test_attempt_stage_is_free_text(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    first = _attempt(conn, stage="relevance_classification")
    second = _attempt(conn, stage="proposal_generation")
    rows = conn.execute("SELECT id, stage FROM processing_attempt").fetchall()
    by_id = {row["id"]: row["stage"] for row in rows}
    assert first != second
    assert by_id[first] == "relevance_classification"
    assert by_id[second] == "proposal_generation"


def test_attempt_blank_stage_is_rejected(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(conn, stage="")
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(conn, stage="   ")


def test_attempt_error_domain(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    failed = _attempt(conn, outcome="FAILED_RETRYABLE", error="model timeout after 5000 ms")
    row = conn.execute(
        "SELECT outcome, error FROM processing_attempt WHERE id = ?", (failed,)
    ).fetchone()
    assert row is not None
    assert row["outcome"] == "FAILED_RETRYABLE"
    assert row["error"] == "model timeout after 5000 ms"
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(conn, error="")
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(conn, error="   ")


def test_attempt_timestamps_stored_verbatim_and_open_ended(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    open_ended = _attempt(conn)
    finished = _attempt(conn, finished_at="2026-05-01T10:34:59Z")
    first = conn.execute(
        "SELECT started_at, finished_at FROM processing_attempt WHERE id = ?", (open_ended,)
    ).fetchone()
    second = conn.execute(
        "SELECT started_at, finished_at FROM processing_attempt WHERE id = ?", (finished,)
    ).fetchone()
    assert first is not None and second is not None
    assert first["started_at"] == _STARTED_AT
    assert first["finished_at"] is None
    assert second["started_at"] == _STARTED_AT
    assert second["finished_at"] == "2026-05-01T10:34:59Z"


def test_attempt_model_fields_optional_but_nonblank(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    unattributed = _attempt(conn)
    attributed = _attempt(
        conn,
        model_id="backed-up-model",
        model_version="2.0.1",
        config_version="cfg-7",
    )
    bare = conn.execute(
        "SELECT model_id, model_version, config_version FROM processing_attempt WHERE id = ?",
        (unattributed,),
    ).fetchone()
    full = conn.execute(
        "SELECT model_id, model_version, config_version FROM processing_attempt WHERE id = ?",
        (attributed,),
    ).fetchone()
    assert bare is not None and full is not None
    assert (
        bare["model_id"],
        bare["model_version"],
        bare["config_version"],
    ) == (None, None, None)
    assert (
        full["model_id"],
        full["model_version"],
        full["config_version"],
    ) == ("backed-up-model", "2.0.1", "cfg-7")
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(conn, model_id="")
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(conn, model_version=" ")
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(conn, config_version="  ")


def test_attempt_without_bucket_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(conn, bucket_id=None)


def test_attempt_with_unknown_bucket_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(conn, bucket_id=999)


def test_attempt_bucket_deletion_is_restricted(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    _attempt(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM bucket WHERE id = 1")


def test_decision_relevant_minimal_stores_verbatim_provenance(
    conn: sqlite3.Connection,
) -> None:
    _seed_bucket_with_messages(conn)
    attempt_id = _attempt(conn)
    decision_id = _decision(conn, attempt_id=attempt_id)
    row = conn.execute(
        "SELECT bucket_id, message_id, attempt_id, decision, model_id,"
        " model_version, prompt_version, decided_at FROM relevance_decision"
        " WHERE id = ?",
        (decision_id,),
    ).fetchone()
    assert row is not None
    assert row["bucket_id"] == 1
    assert row["message_id"] == 1
    assert row["attempt_id"] == attempt_id
    assert row["decision"] == "RELEVANT"
    assert row["model_id"] == "model-a"
    assert row["model_version"] == "1.0.0"
    assert row["prompt_version"] == "prompt-1"
    assert row["decided_at"] == _DECIDED_AT


def test_decision_irrelevant_accepted(conn: sqlite3.Connection) -> None:
    _seed_bucket_with_messages(conn)
    attempt_id = _attempt(conn)
    decision_id = _decision(conn, attempt_id=attempt_id, decision="IRRELEVANT")
    row = conn.execute(
        "SELECT decision FROM relevance_decision WHERE id = ?", (decision_id,)
    ).fetchone()
    assert row is not None
    assert row["decision"] == "IRRELEVANT"


def test_decision_invalid_values_are_rejected(conn: sqlite3.Connection) -> None:
    _seed_bucket_with_messages(conn)
    attempt_id = _attempt(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, attempt_id=attempt_id, decision="RELEVANT ")
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, attempt_id=attempt_id, decision=None)
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, attempt_id=attempt_id, decision="UNCERTAIN")


def test_decision_provenance_nonblank_when_present(conn: sqlite3.Connection) -> None:
    _seed_bucket_with_messages(conn)
    attempt_id = _attempt(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, attempt_id=attempt_id, model_id="")
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, attempt_id=attempt_id, model_version="  ")
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, attempt_id=attempt_id, prompt_version="")
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, attempt_id=attempt_id, decided_at="   ")


def test_decision_duplicate_attempt_message_is_rejected(conn: sqlite3.Connection) -> None:
    _seed_bucket_with_messages(conn)
    attempt_id = _attempt(conn)
    _decision(conn, attempt_id=attempt_id)
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, attempt_id=attempt_id)


def test_decision_retry_with_new_attempt_is_accepted(conn: sqlite3.Connection) -> None:
    _seed_bucket_with_messages(conn)
    first_attempt = _attempt(conn)
    second_attempt = _attempt(conn)
    _decision(conn, attempt_id=first_attempt)
    retried = _decision(
        conn,
        attempt_id=second_attempt,
        decided_at="2026-05-01T10:40:00Z",
    )
    rows = conn.execute("SELECT COUNT(*) FROM relevance_decision").fetchone()
    assert rows[0] == 2
    row = conn.execute(
        "SELECT attempt_id FROM relevance_decision WHERE id = ?", (retried,)
    ).fetchone()
    assert row is not None
    assert row["attempt_id"] == second_attempt


def test_duplicating_decisions_across_connections_keeps_one_row(
    conn: sqlite3.Connection,
) -> None:
    _seed_bucket_with_messages(conn)
    attempt_id = _attempt(conn)
    _decision(conn, attempt_id=attempt_id)

    database_file = conn.execute("PRAGMA database_list").fetchone()[-1]
    second = connect(database_file)
    try:
        assert {1, 2, 3, 4} <= set(_versions(second))
        assert _versions(second) == _versions(conn)
        with transaction(second):
            with pytest.raises(sqlite3.IntegrityError):
                _decision(second, attempt_id=attempt_id)
        assert conn.execute("SELECT COUNT(*) FROM relevance_decision").fetchone()[0] == 1
    finally:
        second.close()


def test_decision_multiple_messages_same_attempt_accepted(conn: sqlite3.Connection) -> None:
    _seed_bucket_with_messages(conn, message_count=2)
    attempt_id = _attempt(conn)
    _decision(conn, attempt_id=attempt_id, message_id=1)
    _decision(
        conn,
        attempt_id=attempt_id,
        message_id=2,
        decision="IRRELEVANT",
        decided_at="2026-05-01T10:36:00Z",
    )
    rows = conn.execute(
        "SELECT message_id, decision FROM relevance_decision ORDER BY message_id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["message_id"] == 1 and rows[0]["decision"] == "RELEVANT"
    assert rows[1]["message_id"] == 2 and rows[1]["decision"] == "IRRELEVANT"


def test_decision_membership_composite_fk_rejects_mismatch(
    conn: sqlite3.Connection,
) -> None:
    _seed_bucket_with_messages(conn)
    attempt_id = _attempt(conn)
    _add_message(conn, message_id=9, group_id=1)
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, attempt_id=attempt_id, message_id=9)
    _add_group(conn, group_id=2)
    _add_message(conn, message_id=2, group_id=2)
    other_bucket_id = _add_bucket(conn, group_id=2)
    _join_message(conn, bucket_id=other_bucket_id, message_id=2, group_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, attempt_id=attempt_id, bucket_id=1, message_id=2)


def test_decision_membership_composite_fk_accepts_valid(
    conn: sqlite3.Connection,
) -> None:
    _seed_bucket_with_messages(conn)
    attempt_id = _attempt(conn)
    decision_id = _decision(
        conn, attempt_id=attempt_id, message_id=1, decision="IRRELEVANT"
    )
    row = conn.execute(
        "SELECT bucket_id, message_id FROM relevance_decision WHERE id = ?",
        (decision_id,),
    ).fetchone()
    assert row is not None
    assert (row["bucket_id"], row["message_id"]) == (1, 1)


def test_decision_attempt_bucket_pairing_rejects_mismatch(
    conn: sqlite3.Connection,
) -> None:
    _seed_bucket_with_messages(conn)
    _add_group(conn, group_id=2)
    _add_bucket(conn)
    rows = conn.execute("SELECT id FROM bucket ORDER BY id").fetchall()
    assert rows[0]["id"] == 1 and rows[1]["id"] == 2
    foreign_attempt = _attempt(conn, bucket_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, attempt_id=foreign_attempt)


def test_decision_attempt_bucket_pairing_accepts_valid(
    conn: sqlite3.Connection,
) -> None:
    _seed_bucket_with_messages(conn)
    attempt_id = _attempt(conn, bucket_id=1)
    decision_id = _decision(conn, attempt_id=attempt_id)
    row = conn.execute(
        "SELECT attempt_id, bucket_id FROM relevance_decision WHERE id = ?",
        (decision_id,),
    ).fetchone()
    assert row is not None
    assert (row["attempt_id"], row["bucket_id"]) == (attempt_id, 1)


def test_decision_with_unknown_attempt_is_rejected(conn: sqlite3.Connection) -> None:
    _seed_bucket_with_messages(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _decision(conn, attempt_id=999)


def test_decision_referenced_rows_deletion_is_restricted(conn: sqlite3.Connection) -> None:
    _seed_bucket_with_messages(conn)
    attempt_id = _attempt(conn)
    _decision(conn, attempt_id=attempt_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM processing_attempt WHERE id = ?", (attempt_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM source_message WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM bucket WHERE id = 1")


def test_failed_retryable_attempt_has_zero_decisions_and_message_untouched(
    conn: sqlite3.Connection,
) -> None:
    _seed_bucket_with_messages(conn)
    _attempt(conn, outcome="FAILED_RETRYABLE", error="classifier unreachable")
    assert conn.execute("SELECT COUNT(*) FROM relevance_decision").fetchone()[0] == 0
    message_row = conn.execute(
        "SELECT id, content, flag_unclassified FROM source_message WHERE id = 1"
    ).fetchone()
    assert message_row is not None
    assert message_row["id"] == 1
    assert message_row["content"] is None
    assert message_row["flag_unclassified"] == 0


def test_foreign_key_check_clean_after_mixed_workload(conn: sqlite3.Connection) -> None:
    _seed_bucket_with_messages(conn, message_count=2)
    attempt_id = _attempt(conn)
    _decision(conn, attempt_id=attempt_id, message_id=1, decision="RELEVANT")
    _decision(
        conn,
        attempt_id=attempt_id,
        message_id=2,
        decision="IRRELEVANT",
        decided_at="2026-05-01T10:36:00Z",
    )
    person_id = _add_person(conn)
    correction_id = _correction(conn, decision_id=1, person_id=person_id)
    row = conn.execute(
        "SELECT decision_id, person_id, corrected_decision, corrected_at"
        " FROM relevance_correction WHERE id = ?",
        (correction_id,),
    ).fetchone()
    assert row is not None
    assert (
        row["decision_id"],
        row["person_id"],
        row["corrected_decision"],
        row["corrected_at"],
    ) == (1, person_id, "IRRELEVANT", _CORRECTED_AT)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_correction_minimal_stored(conn: sqlite3.Connection) -> None:
    _seed_bucket_with_messages(conn)
    attempt_id = _attempt(conn)
    decision_id = _decision(conn, attempt_id=attempt_id)
    person_id = _add_person(conn)
    correction_id = _correction(conn, decision_id=decision_id, person_id=person_id)
    row = conn.execute(
        "SELECT decision_id, person_id, corrected_decision, corrected_at"
        " FROM relevance_correction WHERE id = ?",
        (correction_id,),
    ).fetchone()
    assert row is not None
    assert (
        row["decision_id"],
        row["person_id"],
        row["corrected_decision"],
        row["corrected_at"],
    ) == (
        decision_id,
        person_id,
        "IRRELEVANT",
        _CORRECTED_AT,
    )


def test_correction_both_tokens_accepted(conn: sqlite3.Connection) -> None:
    _seed_bucket_with_messages(conn)
    first_attempt = _attempt(conn)
    second_attempt = _attempt(conn)
    relevant = _decision(conn, attempt_id=first_attempt, decision="RELEVANT")
    irrelevant = _decision(
        conn,
        attempt_id=second_attempt,
        decision="IRRELEVANT",
        decided_at="2026-05-01T10:36:00Z",
    )
    person_id = _add_person(conn)
    first = _correction(
        conn, decision_id=relevant, person_id=person_id, corrected_decision="IRRELEVANT"
    )
    second = _correction(
        conn,
        decision_id=irrelevant,
        person_id=person_id,
        corrected_decision="RELEVANT",
        corrected_at="2026-05-01T11:05:00Z",
    )
    first_row = conn.execute(
        "SELECT corrected_decision FROM relevance_correction WHERE id = ?", (first,)
    ).fetchone()
    second_row = conn.execute(
        "SELECT corrected_decision FROM relevance_correction WHERE id = ?", (second,)
    ).fetchone()
    assert first_row is not None and second_row is not None
    assert first_row["corrected_decision"] == "IRRELEVANT"
    assert second_row["corrected_decision"] == "RELEVANT"


def test_correction_invalid_values_are_rejected(conn: sqlite3.Connection) -> None:
    _seed_bucket_with_messages(conn)
    attempt_id = _attempt(conn)
    decision_id = _decision(conn, attempt_id=attempt_id)
    person_id = _add_person(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _correction(conn, decision_id=decision_id, person_id=person_id, corrected_decision="DONE")
    with pytest.raises(sqlite3.IntegrityError):
        _correction(conn, decision_id=decision_id, person_id=person_id, corrected_decision=None)
    with pytest.raises(sqlite3.IntegrityError):
        _correction(
            conn,
            decision_id=decision_id,
            person_id=person_id,
            corrected_decision=" RELEVANT ",
        )


def test_correction_unknown_decision_or_person_is_rejected(conn: sqlite3.Connection) -> None:
    _seed_bucket_with_messages(conn)
    person_id = _add_person(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _correction(conn, decision_id=999, person_id=person_id)
    attempt_id = _attempt(conn)
    decision_id = _decision(conn, attempt_id=attempt_id)
    with pytest.raises(sqlite3.IntegrityError):
        _correction(conn, decision_id=decision_id, person_id=999)


def test_decision_row_unchanged_after_correction_inserted(conn: sqlite3.Connection) -> None:
    _seed_bucket_with_messages(conn)
    attempt_id = _attempt(conn)
    decision_id = _decision(conn, attempt_id=attempt_id)
    person_id = _add_person(conn)
    before = conn.execute(
        "SELECT * FROM relevance_decision WHERE id = ?", (decision_id,)
    ).fetchone()
    _correction(conn, decision_id=decision_id, person_id=person_id)
    after = conn.execute(
        "SELECT * FROM relevance_decision WHERE id = ?", (decision_id,)
    ).fetchone()
    assert before is not None and after is not None
    assert tuple(after) == tuple(before)


def test_correction_multiple_per_decision_allowed_and_delete_restricts(
    conn: sqlite3.Connection,
) -> None:
    _seed_bucket_with_messages(conn)
    attempt_id = _attempt(conn)
    decision_id = _decision(conn, attempt_id=attempt_id)
    first_person = _add_person(conn)
    second_person = _add_person(conn)
    _correction(
        conn,
        decision_id=decision_id,
        person_id=first_person,
        corrected_decision="IRRELEVANT",
    )
    _correction(
        conn,
        decision_id=decision_id,
        person_id=second_person,
        corrected_decision="RELEVANT",
        corrected_at="2026-05-01T12:00:00Z",
    )
    rows = conn.execute(
        "SELECT COUNT(*) FROM relevance_correction WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    assert rows[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM relevance_decision WHERE id = ?", (decision_id,))
    for person_id in (first_person, second_person):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM person WHERE id = ?", (person_id,))
