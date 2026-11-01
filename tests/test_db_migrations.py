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
