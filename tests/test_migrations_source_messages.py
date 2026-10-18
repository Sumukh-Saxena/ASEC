"""Tests for the 0002_source_messages migration (Slice 2.3 messages, idempotency).

Covers SRS FR-ING-02..07, FR-ING-09, FR-ING-10, FR-ING-05, FR-REL-07 (flag
semantics only), AC-10, AC-21, DR-01, DR-02, DR-03, DR-04, DR-05, NFR-CON-01,
NFR-REL-04 and the §9 relational-store constraint.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.db.connection import connect, transaction
from backend.db.migrations import apply_migrations

_MESSAGE_TABLES = {"source_message", "message_reaction"}
_ROSTER_TABLES = {"vertical", "source_group", "person", "person_vertical"}


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


def _message_count(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) FROM source_message").fetchone()
    return int(row[0])


def _add_group(
    connection: sqlite3.Connection,
    channel: str,
    vertical_id: int | None,
    group_id: int | None = None,
) -> int:
    if group_id is None:
        cursor = connection.execute(
            "INSERT INTO source_group (discord_channel_id, vertical_id) VALUES (?, ?)",
            (channel, vertical_id),
        )
    else:
        cursor = connection.execute(
            "INSERT INTO source_group (id, discord_channel_id, vertical_id) VALUES (?, ?, ?)",
            (group_id, channel, vertical_id),
        )
    return int(cursor.lastrowid or group_id or 0)


def _add_person(connection: sqlite3.Connection, person_id: int, name: str = "Asha") -> int:
    connection.execute(
        "INSERT INTO person (id, name, role) VALUES (?, ?, 'AH')", (person_id, name)
    )
    return person_id


def _add_message(
    connection: sqlite3.Connection,
    source_message_id: str = "111",
    group_id: int = 1,
    source: str = "discord",
    received_at: str = "2026-05-01T10:00:00Z",
    **fields: Any,
) -> int:
    columns = ["source", "source_message_id", "group_id", "received_at"]
    values: list[Any] = [source, source_message_id, group_id, received_at]
    for key, value in fields.items():
        columns.append(key)
        values.append(value)
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO source_message ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    return int(cursor.lastrowid or 0)


def _add_reaction(
    connection: sqlite3.Connection,
    message_id: int,
    reactor: str = "999",
    emoji: str = "👍",
    direction: str = "ADDED",
    reacted_at: str | None = None,
    reaction_id: int | None = None,
) -> int:
    columns = ["message_id", "reactor_discord_user_id", "emoji", "direction"]
    values: list[Any] = [message_id, reactor, emoji, direction]
    if reaction_id is not None:
        columns.insert(0, "id")
        values.insert(0, reaction_id)
    if reacted_at is not None:
        columns.append("reacted_at")
        values.append(reacted_at)
    placeholders = ", ".join("?" for _ in values)
    cursor = connection.execute(
        f"INSERT INTO message_reaction ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    return int(cursor.lastrowid or reaction_id or 0)


def test_default_migrations_apply_and_create_message_tables(conn: sqlite3.Connection) -> None:
    assert _versions(conn) == [1, 2]
    assert _ROSTER_TABLES <= _table_names(conn)
    assert _MESSAGE_TABLES <= _table_names(conn)


def test_reapplying_the_migrations_is_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "test.db")
    try:
        first = apply_migrations(connection)
        assert first == [1, 2]
        at_1 = _applied_at(connection, 1)
        at_2 = _applied_at(connection, 2)

        second = apply_migrations(connection)
        assert second == []
        assert _versions(connection) == [1, 2]
        assert _applied_at(connection, 1) == at_1
        assert _applied_at(connection, 2) == at_2
    finally:
        connection.close()


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
    (directory / "0003_broken.sql").write_text(
        "CREATE TABLE gamma (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE gamma (id INTEGER);\n"
        "INSERT INTO alpha (id) VALUES (2);\n",
        encoding="utf-8",
    )

    connection = connect(tmp_path / "test.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(connection, directory)

        names = _table_names(connection)
        assert {"alpha", "beta"} <= names
        assert "gamma" not in names
        assert _versions(connection) == [1, 2]
        assert _applied_at(connection, 1)
        assert _applied_at(connection, 2)
        assert connection.execute("SELECT COUNT(*) FROM alpha").fetchone()[0] == 1
    finally:
        connection.close()


def test_minimal_valid_message_is_stored_with_defaults(conn: sqlite3.Connection) -> None:
    group_id = _add_group(conn, "chan-1", None, 1)
    message_id = _add_message(conn, source_message_id="111", group_id=group_id)

    row = conn.execute("SELECT * FROM source_message WHERE id = ?", (message_id,)).fetchone()
    assert row is not None
    assert row["source"] == "discord"
    assert row["source_message_id"] == "111"
    assert row["group_id"] == group_id
    assert row["sender_person_id"] is None
    assert row["sender_discord_user_id"] is None
    assert row["event_timestamp"] is None
    assert row["content"] is None
    assert row["reply_reference"] is None
    assert row["raw_metadata"] is None


def test_newly_stored_message_has_zero_flags(conn: sqlite3.Connection) -> None:
    _add_group(conn, "chan-1", None, 1)
    message_id = _add_message(conn)

    row = conn.execute(
        "SELECT flag_late, flag_unmapped, flag_unknown_sender, flag_unclassified "
        "FROM source_message WHERE id = ?",
        (message_id,),
    ).fetchone()
    assert row is not None
    assert row["flag_late"] == 0
    assert row["flag_unmapped"] == 0
    assert row["flag_unknown_sender"] == 0
    assert row["flag_unclassified"] == 0


def test_duplicate_delivery_produces_one_record(conn: sqlite3.Connection) -> None:
    _add_group(conn, "chan-1", None, 1)
    _add_message(conn, source="discord", source_message_id="111")

    with pytest.raises(sqlite3.IntegrityError):
        _add_message(conn, source="discord", source_message_id="111")

    assert _message_count(conn) == 1


def test_same_source_message_id_from_different_sources_is_stored_twice(
    conn: sqlite3.Connection,
) -> None:
    _add_group(conn, "chan-1", None, 1)
    first = _add_message(conn, source="discord", source_message_id="111")
    second = _add_message(conn, source="export", source_message_id="111")

    assert first != second
    assert _message_count(conn) == 2
    rows = conn.execute(
        "SELECT source FROM source_message WHERE source_message_id = '111' ORDER BY source"
    ).fetchall()
    assert [row[0] for row in rows] == ["discord", "export"]


def test_message_with_unknown_group_reference_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _add_message(conn, group_id=999)


def test_message_requires_a_group(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        _add_message(conn, group_id=None)


def test_message_with_unknown_sender_person_reference_is_rejected(
    conn: sqlite3.Connection,
) -> None:
    _add_group(conn, "chan-1", None, 1)
    with pytest.raises(sqlite3.IntegrityError):
        _add_message(conn, sender_person_id=999)


def test_unknown_sender_is_stored_with_flag_and_raw_id(conn: sqlite3.Connection) -> None:
    _add_group(conn, "chan-1", None, 1)
    message_id = _add_message(
        conn, sender_person_id=None, flag_unknown_sender=1, sender_discord_user_id="424242"
    )

    row = conn.execute("SELECT * FROM source_message WHERE id = ?", (message_id,)).fetchone()
    assert row is not None
    assert row["sender_person_id"] is None
    assert row["flag_unknown_sender"] == 1
    assert row["sender_discord_user_id"] == "424242"


def test_message_with_known_sender_is_stored(conn: sqlite3.Connection) -> None:
    group_id = _add_group(conn, "chan-1", None, 1)
    person_id = _add_person(conn, person_id=1, name="Asha")
    message_id = _add_message(conn, group_id=group_id, sender_person_id=person_id)

    row = conn.execute(
        "SELECT sender_person_id FROM source_message WHERE id = ?", (message_id,)
    ).fetchone()
    assert row is not None
    assert row["sender_person_id"] == person_id


def test_original_content_is_stored_unchanged(conn: sqlite3.Connection) -> None:
    _add_group(conn, "chan-1", None, 1)
    content = "call me on 98765 43210 or email a@b.com who is in the PR vertical"
    message_id = _add_message(conn, content=content)

    row = conn.execute("SELECT content FROM source_message WHERE id = ?", (message_id,)).fetchone()
    assert row is not None
    assert row["content"] == content


def test_required_message_fields_are_not_null(conn: sqlite3.Connection) -> None:
    _add_group(conn, "chan-1", None, 1)
    with pytest.raises(sqlite3.IntegrityError):
        _add_message(conn, source="")
    with pytest.raises(sqlite3.IntegrityError):
        _add_message(conn, source_message_id="")
    with pytest.raises(sqlite3.IntegrityError):
        _add_message(conn, received_at=None)


def test_optional_fields_may_be_null(conn: sqlite3.Connection) -> None:
    _add_group(conn, "chan-1", None, 1)
    message_id = _add_message(
        conn,
        sender_person_id=None,
        sender_discord_user_id=None,
        event_timestamp=None,
        content=None,
        reply_reference=None,
        raw_metadata=None,
    )
    row = conn.execute(
        "SELECT event_timestamp, sender_discord_user_id, content, reply_reference, raw_metadata "
        "FROM source_message WHERE id = ?",
        (message_id,),
    ).fetchone()
    assert row is not None
    assert row["event_timestamp"] is None
    assert row["sender_discord_user_id"] is None
    assert row["content"] is None
    assert row["reply_reference"] is None
    assert row["raw_metadata"] is None


def test_empty_text_values_are_rejected(conn: sqlite3.Connection) -> None:
    _add_group(conn, "chan-1", None, 1)
    _add_message(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _add_message(conn, content="")
    with pytest.raises(sqlite3.IntegrityError):
        _add_message(conn, reply_reference=" ")
    with pytest.raises(sqlite3.IntegrityError):
        _add_message(conn, raw_metadata=" ")


def test_message_flags_must_be_zero_or_one(conn: sqlite3.Connection) -> None:
    _add_group(conn, "chan-1", None, 1)
    with pytest.raises(sqlite3.IntegrityError):
        _add_message(conn, flag_unmapped=2)
    with pytest.raises(sqlite3.IntegrityError):
        _add_message(conn, flag_late=None)
    with pytest.raises(sqlite3.IntegrityError):
        _add_message(conn, flag_unclassified=None)


def test_deleting_referenced_rows_is_restricted(conn: sqlite3.Connection) -> None:
    group_id = _add_group(conn, "chan-1", None, 1)
    person_id = _add_person(conn, person_id=1)
    message_id = _add_message(conn, group_id=group_id, sender_person_id=person_id)
    _add_reaction(conn, message_id, reaction_id=1)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM source_group WHERE id = ?", (group_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM person WHERE id = ?", (person_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM source_message WHERE id = ?", (message_id,))

    assert _message_count(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM message_reaction").fetchone()[0] == 1


def test_reaction_constraint_behavior(conn: sqlite3.Connection) -> None:
    _add_group(conn, "chan-1", None, 1)
    message_id = _add_message(conn)

    reaction_id = _add_reaction(conn, message_id, reaction_id=1)
    row = conn.execute(
        "SELECT reacted_at, emoji FROM message_reaction WHERE id = ?", (reaction_id,)
    ).fetchone()
    assert row is not None
    assert row["reacted_at"] is None
    assert row["emoji"] == "👍"

    with pytest.raises(sqlite3.IntegrityError):
        _add_reaction(conn, message_id, direction=" ", reaction_id=2)
    with pytest.raises(sqlite3.IntegrityError):
        _add_reaction(conn, message_id, reactor="", emoji="🎉", reaction_id=3)
    with pytest.raises(sqlite3.IntegrityError):
        _add_reaction(conn, 999, reaction_id=4)


def test_message_survives_failed_reaction_transaction(
    conn: sqlite3.Connection,
) -> None:
    _add_group(conn, "chan-1", None, 1)
    message_id = _add_message(conn)

    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn):
            _add_reaction(conn, 999, reaction_id=1)

    row = conn.execute("SELECT id FROM source_message WHERE id = ?", (message_id,)).fetchone()
    assert row is not None
    assert _message_count(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM message_reaction").fetchone()[0] == 0


def test_duplicate_delivery_retry_across_connections(tmp_path: Path) -> None:
    first = connect(tmp_path / "test.db")
    apply_migrations(first)
    try:
        _add_group(first, "chan-1", None, 1)
        with transaction(first):
            message_id = _add_message(first)

        second = connect(tmp_path / "test.db")
        try:
            row = second.execute(
                "SELECT id FROM source_message WHERE id = ?", (message_id,)
            ).fetchone()
            assert row is not None
            with pytest.raises(sqlite3.IntegrityError):
                with transaction(second):
                    _add_message(second)

            assert _message_count(second) == 1
        finally:
            second.close()
    finally:
        first.close()


def test_multiple_null_values_are_allowed(tmp_path: Path) -> None:
    connection = connect(tmp_path / "test.db")
    try:
        apply_migrations(connection)
        group_id = _add_group(connection, "chan-1", None, 1)
        for i in range(1, 4):
            _add_message(
                connection,
                source_message_id=str(i),
                group_id=group_id,
                content=None,
                event_timestamp=None,
            )

        rows = connection.execute(
            "SELECT content, event_timestamp FROM source_message ORDER BY id"
        ).fetchall()
        assert len(rows) == 3
        assert all(row["content"] is None for row in rows)
        assert all(row["event_timestamp"] is None for row in rows)
    finally:
        connection.close()
