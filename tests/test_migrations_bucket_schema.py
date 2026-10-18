"""Schema-level tests for the 0003_buckets migration (Slice 2.4).

Verifies: default-directory migration application, idempotency, and failure
isolation; the §4.1 Bucket entity fields; FR-BKT-08 predecessor links; the
FR-BKT-12 state vocabulary as recording only (no transition logic here);
FR-BKT-01 (at most one bucket per message) and FR-BKT-02 (same-group
membership enforced by composite foreign keys with one duplicated group_id
per membership row, approved correction decision).

Not implemented and not claimed: bucket formation (FR-BKT-03/04/05),
arrival-order and concurrency behaviors (FR-BKT-06/07), membership
assignment logic, processing-state transitions beyond recording, tasks or
proposals (FR-BKT-09/10), and every NFR. FR-BKT-04 and FR-BKT-05 are blank
in the SRS and remain an open requirements issue; nothing here infers
their semantics.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.db.connection import connect, transaction
from backend.db.migrations import apply_migrations

_BUCKET_TABLES = {"bucket", "bucket_message"}
_ROSTER_TABLES = {"vertical", "source_group", "person", "person_vertical"}
_MESSAGE_TABLES = {"source_message", "message_reaction"}
_BUCKET_STATES = ["OPEN", "CLOSED", "PROCESSED", "FAILED", "AWAITING_RECOVERY"]


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


def _bucket_count(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) FROM bucket").fetchone()
    return int(row[0])


def _add_group(connection: sqlite3.Connection, group_id: int | None = None) -> int:
    if group_id is None:
        cursor = connection.execute(
            "INSERT INTO source_group (discord_channel_id) VALUES ('ch-bucket')"
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
        "INSERT INTO source_message (source, source_message_id, group_id, received_at)"
        " VALUES ('discord', ?, ?, '2026-05-01T10:00:00Z')",
        (str(message_id), group_id),
    )
    return message_id


def _add_bucket(
    connection: sqlite3.Connection,
    bucket_id: int | None = None,
    group_id: int = 1,
    predecessor_bucket_id: int | None = None,
    **fields: Any,
) -> int:
    columns = ["group_id"]
    values: list[Any] = [group_id]
    if predecessor_bucket_id is not None:
        columns.append("predecessor_bucket_id")
        values.append(predecessor_bucket_id)
    for key, value in fields.items():
        columns.append(key)
        values.append(value)
    if bucket_id is not None:
        columns.append("id")
        values.append(bucket_id)
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO bucket ({', '.join(columns)}) VALUES ({placeholders})", values
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


def test_default_migrations_apply_and_create_bucket_tables(conn: sqlite3.Connection) -> None:
    assert _BUCKET_TABLES <= _table_names(conn)
    assert _ROSTER_TABLES <= _table_names(conn)
    assert _MESSAGE_TABLES <= _table_names(conn)


def test_reapplying_the_migrations_is_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "test.db")
    try:
        first = apply_migrations(connection)
        assert {1, 2, 3} <= set(first)
        assert {1, 2, 3} <= set(_versions(connection))
        at_3 = _applied_at(connection, 3)

        second = apply_migrations(connection)
        assert second == []
        assert _versions(connection) == sorted(set(_versions(connection)))
        assert _applied_at(connection, 3) == at_3
    finally:
        connection.close()


def test_migrations_recording_versions_are_ordered_and_include_the_bucket_version(
    conn: sqlite3.Connection,
) -> None:
    versions = _versions(conn)
    assert versions == sorted(set(versions))
    assert {1, 2, 3} <= set(versions)


def test_newly_created_bucket_is_open_with_no_window_data(
    conn: sqlite3.Connection,
) -> None:
    _add_group(conn)
    bucket_id = _add_bucket(conn)
    row = conn.execute("SELECT * FROM bucket WHERE id = ?", (bucket_id,)).fetchone()
    assert row is not None
    assert row["processing_state"] == "OPEN"
    assert row["start_event_timestamp"] is None
    assert row["end_event_timestamp"] is None
    assert row["parameters_used"] is None
    assert row["predecessor_bucket_id"] is None


def test_each_documented_state_token_is_recordable(conn: sqlite3.Connection) -> None:
    assert _BUCKET_STATES == [
        "OPEN",
        "CLOSED",
        "PROCESSED",
        "FAILED",
        "AWAITING_RECOVERY",
    ]
    _add_group(conn)
    bucket_id = _add_bucket(conn)
    for state in _BUCKET_STATES:
        conn.execute(
            "UPDATE bucket SET processing_state = ? WHERE id = ?", (state, bucket_id)
        )
        row = conn.execute(
            "SELECT processing_state FROM bucket WHERE id = ?", (bucket_id,)
        ).fetchone()
        assert row is not None
        assert row["processing_state"] == state


def test_unknown_bucket_state_is_rejected(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE bucket SET processing_state = 'DONE' WHERE id = 1"
        )


def test_null_bucket_state_is_rejected(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE bucket SET processing_state = NULL WHERE id = 1")


def test_bucket_with_whitespace_state_is_rejected(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE bucket SET processing_state = ' OPEN ' WHERE id = 1")


def test_bucket_without_group_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO bucket (group_id) VALUES (NULL)")


def test_bucket_with_unknown_group_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO bucket (group_id) VALUES (999)")


def test_bucket_predecessor_must_exist_but_may_be_absent(
    conn: sqlite3.Connection,
) -> None:
    _add_group(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO bucket (group_id, predecessor_bucket_id) VALUES (1, 999)")
    earlier_id = _add_bucket(conn)
    later_id = _add_bucket(conn, predecessor_bucket_id=earlier_id)
    row = conn.execute(
        "SELECT predecessor_bucket_id FROM bucket WHERE id = ?", (later_id,)
    ).fetchone()
    assert row is not None
    assert row["predecessor_bucket_id"] == earlier_id


def test_a_closed_bucket_may_link_several_new_buckets(
    conn: sqlite3.Connection,
) -> None:
    _add_group(conn)
    earlier_id = _add_bucket(conn)
    first = _add_bucket(conn, predecessor_bucket_id=earlier_id)
    second = _add_bucket(conn, predecessor_bucket_id=earlier_id)
    assert first != second
    assert _bucket_count(conn) == 3


def test_a_message_joins_exactly_one_bucket(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_message(conn)
    bucket_a = _add_bucket(conn)
    bucket_b = _add_bucket(conn)
    _join_message(conn, bucket_id=bucket_a)
    with pytest.raises(sqlite3.IntegrityError):
        _join_message(conn, bucket_id=bucket_b)


def test_duplicate_membership_pair_is_rejected(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_message(conn)
    _add_bucket(conn)
    _join_message(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _join_message(conn)


def test_membership_rejects_unknown_bucket(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_message(conn)
    _add_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _join_message(conn, bucket_id=999)


def test_membership_rejects_unknown_message(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _join_message(conn, message_id=999)


def test_valid_same_group_membership_is_recorded_with_verbatim_columns(
    conn: sqlite3.Connection,
) -> None:
    _add_group(conn)
    _add_group(conn, group_id=2)
    _add_message(conn, message_id=1, group_id=2)
    bucket_in_group_two = _add_bucket(conn, group_id=2)
    _join_message(conn, bucket_id=bucket_in_group_two, message_id=1, group_id=2)
    row = conn.execute(
        "SELECT bucket_id, message_id, group_id FROM bucket_message"
    ).fetchone()
    assert row is not None
    assert tuple(row) == (bucket_in_group_two, 1, 2)


def test_cross_group_membership_is_rejected(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_group(conn, group_id=2)
    _add_message(conn, message_id=1, group_id=2)
    bucket_in_group_one = _add_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _join_message(conn, bucket_id=bucket_in_group_one, message_id=1, group_id=2)


def test_membership_matching_the_bucket_but_not_the_message_is_rejected(
    conn: sqlite3.Connection,
) -> None:
    _add_group(conn)
    _add_group(conn, group_id=2)
    bucket_in_group_two = _add_bucket(conn, group_id=2)
    _add_message(conn, message_id=1)
    with pytest.raises(sqlite3.IntegrityError):
        _join_message(conn, bucket_id=bucket_in_group_two, message_id=1, group_id=2)


def test_membership_group_not_matching_either_parent_is_rejected(
    conn: sqlite3.Connection,
) -> None:
    _add_group(conn)
    _add_message(conn)
    _add_bucket(conn)
    _add_group(conn, group_id=3)
    with pytest.raises(sqlite3.IntegrityError):
        _join_message(conn, group_id=3)


def test_membership_without_group_is_rejected(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_message(conn)
    _add_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO bucket_message (bucket_id, message_id, group_id)"
            " VALUES (1, 1, NULL)"
        )


def test_foreign_key_check_reports_no_violations(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_message(conn)
    _add_bucket(conn)
    _join_message(conn)
    _add_group(conn, group_id=2)
    _add_message(conn, message_id=2, group_id=2)
    bucket_in_group_two = _add_bucket(conn, group_id=2)
    _join_message(conn, bucket_id=bucket_in_group_two, message_id=2, group_id=2)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_composite_parent_indexes_exist(conn: sqlite3.Connection) -> None:
    indexes = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert {"ux_source_message_id_group", "ux_bucket_id_group"} <= indexes


def test_referenced_group_deletion_is_restricted(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM source_group WHERE id = 1")


def test_bucket_referenced_only_by_membership_cannot_be_deleted(
    conn: sqlite3.Connection,
) -> None:
    _add_group(conn)
    _add_message(conn)
    bucket_id = _add_bucket(conn)
    _join_message(conn, bucket_id=bucket_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM bucket WHERE id = ?", (bucket_id,))


def test_membership_message_deletion_is_restricted(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    _add_message(conn)
    _add_bucket(conn)
    _join_message(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM source_message WHERE id = 1")


def test_predecessor_bucket_deletion_is_restricted(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    earlier_id = _add_bucket(conn)
    _add_bucket(conn, predecessor_bucket_id=earlier_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM bucket WHERE id = ?", (earlier_id,))


def test_bucket_survives_failed_membership_transaction(
    conn: sqlite3.Connection,
) -> None:
    _add_group(conn)
    _add_message(conn)
    bucket_id = _add_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            _join_message(conn, bucket_id=bucket_id, message_id=1)
            _join_message(conn, bucket_id=999, message_id=1)
    row = conn.execute(
        "SELECT processing_state FROM bucket WHERE id = ?", (bucket_id,)
    ).fetchone()
    assert row is not None
    assert row["processing_state"] == "OPEN"
    assert conn.execute("SELECT COUNT(*) FROM bucket_message").fetchone()[0] == 0


def test_duplicating_membership_retries_across_connections_keeps_one_row(
    conn: sqlite3.Connection,
) -> None:
    _add_group(conn)
    _add_message(conn)
    bucket_id = _add_bucket(conn)
    _join_message(conn, bucket_id=bucket_id)

    database_file = conn.execute("PRAGMA database_list").fetchone()[-1]
    second = connect(database_file)
    try:
        assert {1, 2, 3} <= set(_versions(second))
        assert _versions(second) == _versions(conn)
        with transaction(second):
            with pytest.raises(sqlite3.IntegrityError):
                second.execute(
                    "INSERT INTO bucket_message (bucket_id, message_id, group_id)"
                    " VALUES (?, ?, 1)",
                    (bucket_id, 1),
                )
        assert conn.execute("SELECT COUNT(*) FROM bucket_message").fetchone()[0] == 1
    finally:
        second.close()


def test_buckets_may_exist_for_the_same_group(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    first = _add_bucket(conn)
    second = _add_bucket(conn)
    assert first != second


def test_window_bounds_may_be_missing_and_stored_verbatim(
    conn: sqlite3.Connection,
) -> None:
    _add_group(conn)
    bucket_id = _add_bucket(
        conn,
        start_event_timestamp="2026-05-01T10:00:00Z",
        end_event_timestamp="2026-05-01T10:00:30Z",
    )
    empty_bucket = _add_bucket(conn)
    row = conn.execute("SELECT * FROM bucket WHERE id = ?", (bucket_id,)).fetchone()
    assert row is not None
    assert row["start_event_timestamp"] == "2026-05-01T10:00:00Z"
    assert row["end_event_timestamp"] == "2026-05-01T10:00:30Z"
    absent = conn.execute(
        "SELECT start_event_timestamp, end_event_timestamp FROM bucket WHERE id = ?",
        (empty_bucket,),
    ).fetchone()
    assert absent is not None
    assert absent["start_event_timestamp"] is None
    assert absent["end_event_timestamp"] is None


def test_empty_window_bounds_are_rejected(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_bucket(conn, start_event_timestamp="")
    with pytest.raises(sqlite3.IntegrityError):
        _add_bucket(conn, end_event_timestamp="   ")


def test_parameters_used_is_optional_and_stored_verbatim(
    conn: sqlite3.Connection,
) -> None:
    _add_group(conn)
    configured = _add_bucket(conn, parameters_used='{"W": 15, "G": 5}')
    empty = _add_bucket(conn)
    row = conn.execute(
        "SELECT parameters_used FROM bucket WHERE id = ?", (configured,)
    ).fetchone()
    assert row is not None
    assert row["parameters_used"] == '{"W": 15, "G": 5}'
    absent = conn.execute(
        "SELECT parameters_used FROM bucket WHERE id = ?", (empty,)
    ).fetchone()
    assert absent is not None
    assert absent["parameters_used"] is None


def test_empty_parameters_used_is_rejected(conn: sqlite3.Connection) -> None:
    _add_group(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_bucket(conn, parameters_used="")
    with pytest.raises(sqlite3.IntegrityError):
        _add_bucket(conn, parameters_used="  ")


def test_failed_later_migration_preserves_earlier_migrations(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_ok.sql").write_text(
        "CREATE TABLE alpha (id INTEGER PRIMARY KEY);\nINSERT INTO alpha (id) VALUES (1);\n",
        encoding="utf-8",
    )
    (directory / "0002_ok.sql").write_text(
        "CREATE TABLE beta (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )
    (directory / "0003_ok.sql").write_text(
        "CREATE TABLE bucket ("
        "id INTEGER PRIMARY KEY, "
        "group_id INTEGER NOT NULL REFERENCES beta(id));\n",
        encoding="utf-8",
    )
    (directory / "0004_broken.sql").write_text(
        "CREATE TABLE ROLLBACK_CHECK (should_never_exist INTEGER PRIMARY KEY);\n"
        "INSERT INTO nonexistent_table (id) VALUES (1);\n",
        encoding="utf-8",
    )
    connection = connect(tmp_path / "test.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection, directory=directory)
        assert _versions(connection) == [1, 2, 3]
        assert _applied_at(connection, 3)
        assert "ROLLBACK_CHECK" not in _table_names(connection)
        assert connection.execute("SELECT COUNT(*) FROM alpha").fetchone()[0] == 1
    finally:
        connection.close()
