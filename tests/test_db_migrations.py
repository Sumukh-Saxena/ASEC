"""Tests for backend.db.migrations (Slice 2.1 migration runner)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import MigrationError, apply_migrations

_ALPHA = "CREATE TABLE alpha (id INTEGER PRIMARY KEY);"
_BETA = "CREATE TABLE beta (id INTEGER PRIMARY KEY);"


def _migrations_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir()
    return directory


def _write(directory: Path, name: str, sql: str) -> Path:
    path = directory / name
    path.write_text(sql, encoding="utf-8")
    return path


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def _versions(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return [row["version"] for row in rows]


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


def test_empty_directory_bootstraps_tracking_table(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    assert apply_migrations(conn, _migrations_dir(tmp_path)) == []
    assert "schema_migrations" in _table_names(conn)


def test_migrations_apply_in_order(conn: sqlite3.Connection, tmp_path: Path) -> None:
    directory = _migrations_dir(tmp_path)
    _write(directory, "0001_create_alpha.sql", _ALPHA)
    _write(directory, "0002_create_beta.sql", _BETA)

    assert apply_migrations(conn, directory) == [1, 2]
    assert {"alpha", "beta"} <= _table_names(conn)
    assert _versions(conn) == [1, 2]
    row = conn.execute("SELECT applied_at FROM schema_migrations WHERE version = 1").fetchone()
    assert row is not None
    assert row["applied_at"]


def test_migrations_apply_by_version_not_filename_order(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    directory = _migrations_dir(tmp_path)
    _write(directory, "0010_create_beta.sql", _BETA)
    _write(directory, "0002_create_alpha.sql", _ALPHA)

    assert apply_migrations(conn, directory) == [2, 10]
    assert _versions(conn) == [2, 10]


def test_second_run_does_not_reapply(conn: sqlite3.Connection, tmp_path: Path) -> None:
    directory = _migrations_dir(tmp_path)
    _write(directory, "0001_create_alpha.sql", _ALPHA)

    assert apply_migrations(conn, directory) == [1]
    first = conn.execute("SELECT applied_at FROM schema_migrations WHERE version = 1").fetchone()
    assert first is not None

    assert apply_migrations(conn, directory) == []
    second = conn.execute("SELECT applied_at FROM schema_migrations WHERE version = 1").fetchone()
    assert second is not None
    assert first["applied_at"] == second["applied_at"]


def test_checksum_change_is_detected(conn: sqlite3.Connection, tmp_path: Path) -> None:
    directory = _migrations_dir(tmp_path)
    path = _write(directory, "0001_create_alpha.sql", _ALPHA)
    apply_migrations(conn, directory)

    path.write_text("CREATE TABLE alpha (id INTEGER PRIMARY KEY, extra TEXT);", encoding="utf-8")
    with pytest.raises(MigrationError, match="checksum"):
        apply_migrations(conn, directory)


def test_missing_applied_migration_is_detected(conn: sqlite3.Connection, tmp_path: Path) -> None:
    directory = _migrations_dir(tmp_path)
    path = _write(directory, "0001_create_alpha.sql", _ALPHA)
    apply_migrations(conn, directory)

    path.unlink()
    with pytest.raises(MigrationError, match="missing"):
        apply_migrations(conn, directory)


def test_failing_migration_rolls_back_and_is_not_recorded(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    directory = _migrations_dir(tmp_path)
    _write(directory, "0001_create_alpha.sql", _ALPHA)
    _write(
        directory,
        "0002_broken.sql",
        "CREATE TABLE gamma (id INTEGER PRIMARY KEY);\nCREATE TABLE gamma (id INTEGER);",
    )

    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(conn, directory)

    assert "alpha" in _table_names(conn)
    assert "gamma" not in _table_names(conn)
    assert _versions(conn) == [1]
    alpha = conn.execute("SELECT applied_at FROM schema_migrations WHERE version = 1").fetchone()
    assert alpha is not None
    assert alpha["applied_at"]


def test_data_changes_in_failing_migration_are_rolled_back(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    directory = _migrations_dir(tmp_path)
    _write(directory, "0001_create_alpha.sql", _ALPHA)
    _write(directory, "0002_insert_one.sql", "INSERT INTO alpha (id) VALUES (1);")
    assert apply_migrations(conn, directory) == [1, 2]
    assert conn.execute("SELECT COUNT(*) FROM alpha").fetchone()[0] == 1

    _write(
        directory,
        "0003_insert_then_fail.sql",
        "INSERT INTO alpha (id) VALUES (2);\nINSERT INTO alpha (id) VALUES (2);",
    )
    with pytest.raises(sqlite3.IntegrityError):
        apply_migrations(conn, directory)

    # The duplicate row from the failed migration must not survive.
    assert conn.execute("SELECT COUNT(*) FROM alpha").fetchone()[0] == 1
    assert _versions(conn) == [1, 2]


def test_statements_with_quoted_semicolons_and_comments_are_handled(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    directory = _migrations_dir(tmp_path)
    _write(
        directory,
        "0001_quoted.sql",
        "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL);\n"
        "-- a comment containing a ; semicolon\n"
        "INSERT INTO notes (id, body) VALUES (1, 'a;b;c');\n",
    )

    assert apply_migrations(conn, directory) == [1]
    row = conn.execute("SELECT body FROM notes WHERE id = 1").fetchone()
    assert row is not None
    assert row["body"] == "a;b;c"
    assert _versions(conn) == [1]


def test_incomplete_migration_sql_fails_and_rolls_back(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    directory = _migrations_dir(tmp_path)
    _write(directory, "0001_incomplete.sql", "CREATE TABLE broken (id INTEGER PRIMARY KEY, ")

    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(conn, directory)

    assert "broken" not in _table_names(conn)
    assert _versions(conn) == []


def test_invalid_utf8_migration_is_reported(conn: sqlite3.Connection, tmp_path: Path) -> None:
    directory = _migrations_dir(tmp_path)
    (directory / "0001_bad.sql").write_bytes(b"\xff\xfeCREATE TABLE bad (id INTEGER);")

    with pytest.raises(MigrationError, match="UTF-8"):
        apply_migrations(conn, directory)


def test_duplicate_version_is_rejected(conn: sqlite3.Connection, tmp_path: Path) -> None:
    directory = _migrations_dir(tmp_path)
    _write(directory, "0001_create_alpha.sql", _ALPHA)
    _write(directory, "0001_create_beta.sql", _BETA)

    with pytest.raises(MigrationError, match="[Dd]uplicate"):
        apply_migrations(conn, directory)


def test_invalid_filename_is_rejected(conn: sqlite3.Connection, tmp_path: Path) -> None:
    directory = _migrations_dir(tmp_path)
    _write(directory, "create_alpha.sql", _ALPHA)

    with pytest.raises(MigrationError, match="filename"):
        apply_migrations(conn, directory)


def test_missing_directory_is_reported(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="directory"):
        apply_migrations(conn, tmp_path / "nope")


def _fk_pragma(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA foreign_keys").fetchone()[0])


_PARENT = "CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
_CHILD = (
    "CREATE TABLE child (id INTEGER PRIMARY KEY,"
    " parent_id INTEGER NOT NULL REFERENCES parent (id) ON DELETE RESTRICT);"
)


def test_migration_may_rebuild_a_parent_table_with_restrict_children(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Migration 0016/0017 pattern: drop and rename a referenced table.

    ``connect`` always sets ``foreign_keys = ON`` (DR-01), which rejects
    ``DROP TABLE parent`` while RESTRICT children reference rows in it. The
    runner therefore applies a file with enforcement disabled and proves the
    result with ``PRAGMA foreign_key_check`` before it commits.
    """
    directory = _migrations_dir(tmp_path)
    _write(directory, "0001_schema.sql", f"{_PARENT}\n{_CHILD}")
    _write(
        directory,
        "0002_rows.sql",
        "INSERT INTO parent (id, name) VALUES (1, 'p');\n"
        "INSERT INTO child (id, parent_id) VALUES (1, 1);",
    )
    _write(
        directory,
        "0003_rebuild.sql",
        "CREATE TABLE parent_new (id INTEGER PRIMARY KEY, name TEXT NOT NULL);\n"
        "INSERT INTO parent_new (id, name) SELECT id, name FROM parent;\n"
        "DROP TABLE parent;\n"
        "ALTER TABLE parent_new RENAME TO parent;",
    )

    assert apply_migrations(conn, directory) == [1, 2, 3]
    assert conn.execute("SELECT name FROM parent").fetchone()["name"] == "p"
    assert conn.execute("SELECT count(*) FROM child").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    child_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'child'"
    ).fetchone()[0]
    assert "REFERENCES parent" in child_sql
    conn.execute("INSERT INTO parent (id, name) VALUES (2, 'q')")
    conn.execute("INSERT INTO child (id, parent_id) VALUES (2, 2)")


def test_migration_ending_with_a_dangling_reference_is_reported(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The post-file ``foreign_key_check`` replaces enforcement inside the file."""
    directory = _migrations_dir(tmp_path)
    _write(directory, "0001_schema.sql", f"{_PARENT}\n{_CHILD}")
    _write(
        directory,
        "0002_dangling.sql",
        "INSERT INTO parent (id, name) VALUES (1, 'p');\n"
        "INSERT INTO child (id, parent_id) VALUES (1, 99);",
    )

    with pytest.raises(MigrationError, match="foreign_key_check"):
        apply_migrations(conn, directory)

    assert _versions(conn) == [1]
    assert conn.execute("SELECT count(*) FROM parent").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM child").fetchone()[0] == 0


def test_foreign_keys_pragma_is_restored_after_success_and_failure(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Enforcement is a connection setting: the runner must always restore it."""
    directory = _migrations_dir(tmp_path)
    _write(directory, "0001_schema.sql", f"{_PARENT}\n{_CHILD}")
    assert _fk_pragma(conn) == 1

    assert apply_migrations(conn, directory) == [1]
    assert _fk_pragma(conn) == 1

    _write(
        directory,
        "0002_dangling.sql",
        "INSERT INTO child (id, parent_id) VALUES (1, 99);",
    )
    with pytest.raises(MigrationError):
        apply_migrations(conn, directory)
    assert _fk_pragma(conn) == 1


def test_independent_databases_do_not_leak_state(tmp_path: Path) -> None:
    directory = _migrations_dir(tmp_path)
    _write(directory, "0001_create_alpha.sql", _ALPHA)

    first = connect(tmp_path / "first.db")
    second = connect(tmp_path / "second.db")
    try:
        apply_migrations(first, directory)
        assert "alpha" in _table_names(first)
        assert "alpha" not in _table_names(second)
    finally:
        first.close()
        second.close()


def test_real_migrations_reapply_cleanly_after_a_populated_database(
    tmp_path: Path,
) -> None:
    """Plan §9: a real temp-file DB applies once, re-applies as ``[]``.

    The database is populated with one row of every Phase 7-rebuilt shape
    (person, task, announcement) plus their chains, so the second apply runs
    against real data and ``PRAGMA foreign_key_check`` proves the rebuild kept
    the references intact.
    """
    connection = connect(tmp_path / "phase7-fresh.db")
    try:
        assert apply_migrations(connection) == list(range(1, 19))

        connection.execute("INSERT INTO vertical (id, name) VALUES (1, 'Tech')")
        connection.execute(
            "INSERT INTO person (id, name, phone, role, active, roll_number)"
            " VALUES (1, 'Aarav', '+919844440001', 'AH', 1, 'ROLL-401')"
        )
        connection.execute(
            "INSERT INTO person (id, name, phone, role, active, roll_number)"
            " VALUES (2, 'Neha', '+919844440002', 'Volunteer', 1, 'ROLL-402')"
        )
        connection.execute(
            "INSERT INTO person_vertical (person_id, vertical_id) VALUES (1, 1)"
        )
        connection.execute(
            "INSERT INTO person_vertical (person_id, vertical_id) VALUES (2, 1)"
        )
        connection.execute(
            "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
            " VALUES (1, 'chan-401', 1)"
        )
        connection.execute(
            "INSERT INTO source_message (id, source, source_message_id, group_id,"
            " sender_person_id, event_timestamp, received_at, content)"
            " VALUES (1, 'discord', 'msg-401', 1, 1,"
            " '2026-11-02T09:00:00+00:00', '2026-11-02T09:00:00+00:00',"
            " 'set up the stage')"
        )
        connection.execute(
            "INSERT INTO bucket (id, group_id, start_event_timestamp,"
            " end_event_timestamp) VALUES (1, 1, '2026-11-02T09:00:00+00:00',"
            " '2026-11-02T09:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO bucket_message (bucket_id, message_id, group_id)"
            " VALUES (1, 1, 1)"
        )
        connection.execute(
            "INSERT INTO processing_attempt (id, stage, bucket_id, started_at,"
            " outcome) VALUES (1, 'proposal_generation', 1,"
            " '2026-11-02T09:00:30+00:00', 'SUCCEEDED')"
        )
        connection.execute(
            "INSERT INTO proposal (id, attempt_id, bucket_id, primary_message_id,"
            " source_vertical_id, ptype, assignment_mode, task_type,"
            " structurally_valid, generated_at, model_id, model_version,"
            " prompt_version) VALUES (1, 1, 1, 1, 1, 'TASK', 'EXPLICIT',"
            " 'INTERNAL', 1, '2026-11-02T09:00:30+00:00', 'model-p', '1.0.0',"
            " 'prompt-1')"
        )
        connection.execute(
            "INSERT INTO task (id, proposal_id, task_type, source_vertical_id,"
            " responsible_vertical_id, creator_id, description, assignment_mode,"
            " people_needed, people_needed_unlimited, created_at)"
            " VALUES (1, 1, 'INTERNAL', 1, 1, 1, 'set up the stage', 'EXPLICIT',"
            " NULL, 0, '2026-11-02T09:01:00+00:00')"
        )
        connection.execute(
            "INSERT INTO task_assignee (task_id, person_id, agreement,"
            " work_status, cancelled, added_at)"
            " VALUES (1, 2, 'AWAITING', 'NOT_STARTED', 0,"
            " '2026-11-02T09:01:00+00:00')"
        )
        connection.execute(
            "INSERT INTO task_history (task_id, field_name, old_value, new_value,"
            " changed_at, actor_person_id, actor_role, actor_vertical_id, reason)"
            " VALUES (1, 'assignee', NULL, '2', '2026-11-02T09:01:00+00:00', 1,"
            " 'AH', 1, 'EDIT')"
        )
        connection.execute(
            "INSERT INTO task_attribute (task_id, attr_key, attr_value)"
            " VALUES (1, 'venue', 'Hall A')"
        )
        connection.execute(
            "INSERT INTO announcement (proposal_id, accepted_by, accepted_at,"
            " audience_type, audience_vertical_id, audience_ah_person_id)"
            " VALUES (1, 1, '2026-11-02T09:02:00+00:00', 'AH_VOLUNTEERS', 1, 1)"
        )
        connection.execute(
            "INSERT INTO notification (event_type, recipient_person_id,"
            " dedupe_key, task_id, delivery_state, created_at)"
            " VALUES ('TASK_ASSIGNED', 2, 'TASK_ASSIGNED:1:2', 1, 'PENDING',"
            " '2026-11-02T09:01:00+00:00')"
        )

        assert apply_migrations(connection) == []
        assert _versions(connection) == list(range(1, 19))
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
