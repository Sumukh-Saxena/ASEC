"""Tests for backend.ingest — normalized-event ingestion (Slice 3.1).

Covers SRS FR-ING-01..10, EIR-01, AC-10, AC-21, DR-03, DR-04, NFR-CON-01,
NFR-REL-04, BR-09 and the §9 storage-authority constraint.

Decision stamps below were owner-approved on 2026-10-20 and are recorded in
docs/PHASE2_PLAN.md (Slice 3.1 entry):
- D-1 A: a channel with no source_group row is auto-registered with
  vertical_id NULL and its messages stored flagged UNMAPPED.
- A1: supplied event_timestamp values are stored verbatim; format
  validation is downstream (bucketing), never at ingestion.
- A2: when the adapter omits received_at, the backend stamps UTC receipt
  time; a supplied value is stored verbatim.
- A3: message_mention.position is stored only as supplied (NULL when absent).
- A4: raw_metadata is a verbatim string, or a mapping serialized
  deterministically with sort_keys=True.
- **P-2 (owner decision of 2026-10-21, partial-success batches):** the
  `ingest_events` batch is not all-or-nothing. One transaction per record (the
  live-path discipline) grounds per-record outcomes CREATED / DUPLICATE /
  REJECTED; a record-specific failure never rolls back independent records;
  a systemic failure aborts the batch and raises with the durable prefix of
  outcomes. Recorded in docs/PHASE2_PLAN.md (§7, §11.2).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import backend.ingest
from backend.bucketing import BucketParams, assign_messages
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.ingest import (
    IngestionBatchError,
    InvalidMessageEventError,
    MentionEvent,
    MessageEvent,
    ReactionEvent,
    RecordStatus,
    StoreResult,
    ingest_events,
    ingest_message,
    ingest_reaction,
)

_PARAMS = BucketParams(10, 5, 30, 3)  # SRS §10.1 test parameter set


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "ingest.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _event(**overrides: Any) -> MessageEvent:
    fields: dict[str, Any] = {
        "source": "discord",
        "source_message_id": "101",
        "discord_channel_id": "DMZ-1",
        "sender_discord_user_id": "u-1",
        "event_timestamp": "2026-05-01T10:00:00+00:00",
        "received_at": "2026-05-01T10:00:01+00:00",
        "content": "Standup at noon",
        "reply_reference": None,
        "raw_metadata": None,
        "mentions": (),
        "reactions": (),
    }
    fields.update(overrides)
    return MessageEvent(**fields)


def _vertical(connection: sqlite3.Connection, name: str = "Events") -> int:
    cursor = connection.execute("INSERT INTO vertical (name) VALUES (?)", (name,))
    return int(cursor.lastrowid or 0)


def _group(
    connection: sqlite3.Connection,
    channel: str = "DMZ-1",
    vertical_id: int | None = None,
) -> int:
    cursor = connection.execute(
        "INSERT INTO source_group (discord_channel_id, vertical_id) VALUES (?, ?)",
        (channel, vertical_id),
    )
    return int(cursor.lastrowid or 0)


def _person(
    connection: sqlite3.Connection,
    name: str = "Asha",
    discord_user_id: str = "u-1",
) -> int:
    # PB-5: phone and roll_number are mandatory unique synthetic columns
    cursor = connection.execute(
        "INSERT INTO person (name, discord_user_id, phone, role, roll_number)"
        " VALUES (?, ?, ?, 'AH', ?)",
        (name, discord_user_id, f"+919800004{discord_user_id}", f"ING-{discord_user_id}"),
    )
    return int(cursor.lastrowid or 0)


def _message_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute("SELECT * FROM source_message ORDER BY id").fetchall()


def _message_count(connection: sqlite3.Connection) -> int:
    return len(_message_rows(connection))


def _message_row(connection: sqlite3.Connection, message_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM source_message WHERE id = ?", (message_id,)
    ).fetchone()
    assert row is not None
    return row


def _mention_rows(connection: sqlite3.Connection, message_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT mentioned_discord_user_id, position FROM message_mention"
        " WHERE message_id = ? ORDER BY id",
        (message_id,),
    ).fetchall()


def _reaction_count(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) FROM message_reaction").fetchone()
    return int(row[0])


def _mention_count(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) FROM message_mention").fetchone()
    return int(row[0])


def _group_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT id, discord_channel_id, vertical_id FROM source_group ORDER BY id"
    ).fetchall()


def _fk_clean(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


# FR-ING-01/02 + EIR-01: the superset event round-trips verbatim.


def test_valid_event_with_supplied_fields_stored_verbatim(conn) -> None:
    vertical_id = _vertical(conn)
    group_id = _group(conn, "DMZ-1", vertical_id)
    person_id = _person(conn, discord_user_id="u-1")

    result = ingest_message(conn, _event())

    assert isinstance(result, StoreResult)
    assert result.created is True
    assert _message_count(conn) == 1
    row = _message_row(conn, result.message_id)
    assert row["source"] == "discord"
    assert row["source_message_id"] == "101"
    assert row["group_id"] == group_id
    assert row["sender_person_id"] == person_id
    assert row["sender_discord_user_id"] == "u-1"
    assert row["event_timestamp"] == "2026-05-01T10:00:00+00:00"
    assert row["received_at"] == "2026-05-01T10:00:01+00:00"
    assert row["content"] == "Standup at noon"
    assert row["reply_reference"] is None
    assert row["raw_metadata"] is None
    assert row["flag_unmapped"] == 0
    assert row["flag_unknown_sender"] == 0
    assert row["flag_late"] == 0
    assert row["flag_unclassified"] == 0
    _fk_clean(conn)


# A4: a mapping is serialized deterministically, a string is kept verbatim.


def test_mapping_raw_metadata_serialized_deterministically(conn) -> None:
    _vertical(conn)
    _group(conn)
    payload = {"b": "two", "a": "one", "nested": {"z": 1, "m": 2}}
    result = ingest_message(conn, _event(source_message_id="102", raw_metadata=payload))
    stored = str(_message_row(conn, result.message_id)["raw_metadata"])
    assert stored == json.dumps(payload, sort_keys=True)


# FR-ING-02/DR-04: absent optional fields are stored as missing, never guessed.


def test_absent_optional_fields_stored_as_missing(conn) -> None:
    vertical_id = _vertical(conn)
    group_id = _group(conn, "DMZ-1", vertical_id)

    result = ingest_message(
        conn,
        _event(
            source_message_id="103",
            sender_discord_user_id=None,
            event_timestamp=None,
            content=None,
            reply_reference=None,
            raw_metadata=None,
        ),
    )

    assert result.created is True
    row = _message_row(conn, result.message_id)
    assert row["group_id"] == group_id
    assert row["sender_person_id"] is None
    assert row["sender_discord_user_id"] is None
    assert row["event_timestamp"] is None
    assert row["content"] is None
    assert row["reply_reference"] is None
    assert row["raw_metadata"] is None
    # Without a Discord sender ID there is nothing to look up (FR-ING-10).
    assert row["flag_unknown_sender"] == 1
    assert row["flag_unmapped"] == 0
    _fk_clean(conn)


# A2: the backend stamps its own UTC receipt time when the adapter omits it.


def test_received_at_stamped_by_backend_when_absent(conn) -> None:
    _vertical(conn)
    _group(conn)
    before = datetime.now(UTC).isoformat()

    result = ingest_message(conn, _event(source_message_id="104", received_at=None))

    after = datetime.now(UTC).isoformat()
    stamped = str(_message_row(conn, result.message_id)["received_at"])
    assert stamped.endswith("+00:00")
    assert datetime.fromisoformat(before) <= datetime.fromisoformat(stamped)
    assert datetime.fromisoformat(stamped) <= datetime.fromisoformat(after)


# A1: event timestamps are stored verbatim; format is bucketing's concern.


def test_event_timestamp_stored_verbatim_without_format_validation(conn) -> None:
    _vertical(conn)
    _group(conn)
    result = ingest_message(
        conn, _event(source_message_id="105", event_timestamp="not-an-instant")
    )
    assert (
        str(_message_row(conn, result.message_id)["event_timestamp"])
        == "not-an-instant"
    )


# FR-ING-10: roster membership decides the flag; the raw ID is always kept.


def test_unknown_sender_keeps_raw_discord_id_and_is_flagged(conn) -> None:
    _vertical(conn)
    _group(conn)
    result = ingest_message(
        conn, _event(source_message_id="106", sender_discord_user_id="u-ghost")
    )
    row = _message_row(conn, result.message_id)
    assert row["sender_person_id"] is None
    assert row["sender_discord_user_id"] == "u-ghost"
    assert row["flag_unknown_sender"] == 1
    _fk_clean(conn)


# FR-ING-09: flag_unmapped follows exactly whether the group has a vertical.


def test_group_without_vertical_sets_unmapped_flag(conn) -> None:
    vertical = _vertical(conn)
    test_group_no = _group(conn, "DMZ-mapped", vertical)
    unmapped_group = _group(conn, "DMZ-loose", None)
    mapped = ingest_message(conn, _event(source_message_id="107", discord_channel_id="DMZ-mapped"))
    unmapped = ingest_message(conn, _event(source_message_id="108", discord_channel_id="DMZ-loose"))
    assert _message_row(conn, mapped.message_id)["group_id"] == test_group_no
    assert _message_row(conn, mapped.message_id)["flag_unmapped"] == 0
    assert _message_row(conn, unmapped.message_id)["group_id"] == unmapped_group
    assert _message_row(conn, unmapped.message_id)["flag_unmapped"] == 1
    _fk_clean(conn)


# D-1 A: unknown channels are auto-registered with vertical NULL and flagged.


def test_unknown_channel_auto_registers_and_stores_unmapped(conn) -> None:
    result = ingest_message(
        conn, _event(source_message_id="109", discord_channel_id="DMZ-fresh")
    )
    groups = _group_rows(conn)
    assert len(groups) == 1
    assert str(groups[0]["discord_channel_id"]) == "DMZ-fresh"
    assert groups[0]["vertical_id"] is None
    row = _message_row(conn, result.message_id)
    assert row["group_id"] == int(groups[0]["id"])
    assert row["flag_unmapped"] == 1
    _fk_clean(conn)


def test_unknown_channel_auto_registration_is_idempotent(conn) -> None:
    first = ingest_message(
        conn, _event(source_message_id="110", discord_channel_id="DMZ-fresh")
    )
    second = ingest_message(
        conn, _event(source_message_id="111", discord_channel_id="DMZ-fresh")
    )
    groups = _group_rows(conn)
    assert len(groups) == 1
    first_group = _message_row(conn, first.message_id)["group_id"]
    assert _message_row(conn, second.message_id)["group_id"] == first_group
    assert _message_count(conn) == 2
    _fk_clean(conn)


# FR-ING-04/NFR-CON-01: duplicate delivery is a no-op keeping one identity.


def test_duplicate_delivery_is_idempotent_noop(conn) -> None:
    _vertical(conn)
    _group(conn)
    first = ingest_message(conn, _event(source_message_id="112"))
    second = ingest_message(conn, _event(source_message_id="112"))
    assert first.created is True
    assert second.created is False
    assert second.message_id == first.message_id
    assert _message_count(conn) == 1
    _fk_clean(conn)


def test_duplicate_delivery_from_second_connection_keeps_one_row(
    conn, tmp_path: Path
) -> None:
    _vertical(conn)
    _group(conn)
    second = connect(tmp_path / "ingest.db")
    apply_migrations(second)
    try:
        first = ingest_message(conn, _event(source_message_id="113"))
        redelivery = ingest_message(second, _event(source_message_id="113"))
        assert first.created is True
        assert redelivery.created is False
        assert redelivery.message_id == first.message_id
        assert _message_count(conn) == 1
    finally:
        second.close()
    _fk_clean(conn)


def test_duplicate_delivery_does_not_duplicate_evidence(conn) -> None:
    _vertical(conn)
    _group(conn)
    event = _event(
        source_message_id="114",
        mentions=(MentionEvent("u-2", 0), MentionEvent("u-3", None)),
        reactions=(ReactionEvent("u-4", "👍", "ADDED", "2026-05-01T10:05:00+00:00"),),
    )
    ingest_message(conn, event)
    redelivery = ingest_message(conn, event)
    assert redelivery.created is False
    assert _message_count(conn) == 1
    assert _mention_count(conn) == 2
    assert _reaction_count(conn) == 1
    _fk_clean(conn)


# FR-ING-02/DR-03: mention evidence preserved as supplied, repeats included.


def test_mentions_stored_as_supplied_with_repeated_users(conn) -> None:
    _vertical(conn)
    _group(conn)
    event = _event(
        source_message_id="115",
        mentions=(
            MentionEvent("u-2", 0),
            MentionEvent("u-3", None),
            MentionEvent("u-2", 5),
        ),
    )
    result = ingest_message(conn, event)
    rows = _mention_rows(conn, result.message_id)
    observed = [(str(r["mentioned_discord_user_id"]), r["position"]) for r in rows]
    assert observed == [("u-2", 0), ("u-3", None), ("u-2", 5)]
    _fk_clean(conn)


# FR-ING-07: embedded reaction evidence stored verbatim, both directions.


def test_embedded_reactions_stored_verbatim(conn) -> None:
    _vertical(conn)
    _group(conn)
    event = _event(
        source_message_id="116",
        reactions=(
            ReactionEvent("u-4", "👍", "ADDED", "2026-05-01T10:05:00+00:00"),
            ReactionEvent("u-5", "🚀", "REMOVED", None),
        ),
    )
    result = ingest_message(conn, event)
    rows = conn.execute(
        "SELECT reactor_discord_user_id, emoji, direction, reacted_at"
        " FROM message_reaction WHERE message_id = ? ORDER BY id",
        (result.message_id,),
    ).fetchall()
    observed = [
        (
            str(r["reactor_discord_user_id"]),
            str(r["emoji"]),
            str(r["direction"]),
            r["reacted_at"],
        )
        for r in rows
    ]
    assert observed == [
        ("u-4", "👍", "ADDED", "2026-05-01T10:05:00+00:00"),
        ("u-5", "🚀", "REMOVED", None),
    ]
    _fk_clean(conn)


# FR-ING-07: later-arriving reactions append as evidence against the message.


def test_later_reaction_is_stored_against_existing_message(conn) -> None:
    _vertical(conn)
    _group(conn)
    result = ingest_message(conn, _event(source_message_id="117"))
    reaction_id = ingest_reaction(
        conn,
        ReactionEvent("u-7", "🎉", "REMOVED", "2026-05-01T11:00:00+00:00"),
        result.message_id,
    )
    assert reaction_id >= 1
    row = conn.execute(
        "SELECT reactor_discord_user_id, emoji, direction, reacted_at"
        " FROM message_reaction WHERE id = ?",
        (reaction_id,),
    ).fetchone()
    assert row is not None
    assert row["reactor_discord_user_id"] == "u-7"
    assert row["emoji"] == "🎉"
    assert row["direction"] == "REMOVED"
    assert row["reacted_at"] == "2026-05-01T11:00:00+00:00"
    _fk_clean(conn)


def test_reaction_for_unknown_message_fails_by_fk_contract(conn) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        ingest_reaction(conn, ReactionEvent("u-7", "🎉", "ADDED"), 9999)
    assert _reaction_count(conn) == 0
    _fk_clean(conn)


# The reaction handle itself is a caller-supplied DB id: fail loud pre-write.


@pytest.mark.parametrize(
    "message_id",
    [0, True, -5],
    ids=["zero", "boolean", "negative"],
)
def test_ingest_reaction_rejects_non_positive_or_non_integer_message_ids(
    conn, message_id: Any
) -> None:
    with pytest.raises(InvalidMessageEventError):
        ingest_reaction(conn, ReactionEvent("u-7", "🎉", "ADDED"), message_id)
    assert _reaction_count(conn) == 0
    _fk_clean(conn)


# Invalid events are rejected before any write (FR-ING-05, NFR-REL-04).

_INVALID_OVERRIDES: list[tuple[str, dict[str, Any]]] = [
    ("blank source", {"source": "  "}),
    ("source with surrounding whitespace", {"source": " discord "}),
    ("blank source_message_id", {"source_message_id": "   "}),
    ("blank discord_channel_id", {"discord_channel_id": " "}),
    ("blank received_at", {"received_at": " "}),
    ("blank event_timestamp", {"event_timestamp": " "}),
    ("empty content", {"content": ""}),
    ("blank reply_reference", {"reply_reference": " "}),
    ("blank raw_metadata string", {"raw_metadata": " "}),
    ("unsupported raw_metadata type", {"raw_metadata": 42}),
    ("unserializable raw_metadata mapping", {"raw_metadata": {"k": {1, 2}}}),
    ("blank mention user id", {"mentions": (MentionEvent(" ", None),)}),
    ("negative mention position", {"mentions": (MentionEvent("u-1", -1),)}),
    ("blank reaction reactor", {"reactions": (ReactionEvent(" ", "👍", "ADDED"),)}),
    ("blank reaction emoji", {"reactions": (ReactionEvent("u-1", " ", "ADDED"),)}),
    ("reaction direction with whitespace", {
        "reactions": (ReactionEvent("u-1", "👍", " ADDED"),)
    }),
    ("blank reaction direction", {"reactions": (ReactionEvent("u-1", "👍", "  "),)}),
]


@pytest.mark.parametrize(
    "overrides",
    [item[1] for item in _INVALID_OVERRIDES],
    ids=[item[0] for item in _INVALID_OVERRIDES],
)
def test_invalid_event_rejected_before_any_write(conn, overrides: dict[str, Any]) -> None:
    _vertical(conn)
    _group(conn)
    with pytest.raises(InvalidMessageEventError):
        ingest_message(conn, _event(**overrides))
    assert _message_count(conn) == 0
    assert _mention_count(conn) == 0
    assert _reaction_count(conn) == 0
    # No side effect leaked either: the roster is untouched by rejects.
    assert len(_group_rows(conn)) == 1
    _fk_clean(conn)


def test_validation_failure_does_not_auto_register_channel(conn) -> None:
    with pytest.raises(InvalidMessageEventError):
        ingest_message(
            conn,
            _event(discord_channel_id="DMZ-unknown", source_message_id=" "),
        )
    assert len(_group_rows(conn)) == 0
    assert _message_count(conn) == 0
    _fk_clean(conn)


def test_invalid_evidence_rejects_the_whole_event(conn) -> None:
    # One valid mention and one invalid reaction: nothing is written at all,
    # so a message and its initial evidence stay atomic (FR-ING-05).
    _vertical(conn)
    _group(conn)
    event = _event(
        source_message_id="118",
        mentions=(MentionEvent("u-2", 0),),
        reactions=(ReactionEvent("u-3", "👍", " "),),
    )
    with pytest.raises(InvalidMessageEventError):
        ingest_message(conn, event)
    assert _message_count(conn) == 0
    assert _mention_count(conn) == 0
    assert _reaction_count(conn) == 0
    _fk_clean(conn)


# AC-21 (first half): raw content stored unchanged; masking is pre-AI only.


def test_content_with_phone_like_text_stored_unchanged(conn) -> None:
    _vertical(conn)
    _group(conn)
    content = "Zoom fallback: call 080-555-1212 before 17:00"
    result = ingest_message(conn, _event(source_message_id="119", content=content))
    assert str(_message_row(conn, result.message_id)["content"]) == content
    _fk_clean(conn)


# The written rows are consumable by the existing bucketing read path as-is.


def test_ingested_messages_are_bucketable_without_code_changes(conn) -> None:
    _vertical(conn)
    _group(conn)
    first = ingest_message(
        conn,
        _event(
            source_message_id="120",
            event_timestamp="2026-05-01T10:00:00+00:00",
        ),
    )
    second = ingest_message(
        conn,
        _event(
            source_message_id="121",
            event_timestamp="2026-05-01T10:04:00+00:00",
        ),
    )
    assignment = assign_messages(conn, group_id=1, params=_PARAMS)
    assigned_ids = {message_id for message_id, _bucket_id in assignment.assigned}
    assert assigned_ids == {first.message_id, second.message_id}
    assert assignment.skipped == ()
    member = conn.execute(
        "SELECT bucket_id FROM bucket_message WHERE message_id = ?",
        (first.message_id,),
    ).fetchone()
    assert member is not None
    _fk_clean(conn)


# A mixed workload keeps the whole relational store referentially intact.


def test_foreign_key_check_clean_after_mixed_workload(conn) -> None:
    _person(conn, name="Bil", discord_user_id="u-2")
    first = ingest_message(
        conn, _event(source_message_id="122", discord_channel_id="DMZ-fresh")
    )
    ingest_message(
        conn,
        _event(
            source_message_id="123",
            discord_channel_id="DMZ-fresh",
            sender_discord_user_id="u-2",
            mentions=(MentionEvent("u-2", 0),),
            reactions=(ReactionEvent("u-2", "✅", "ADDED"),),
        ),
    )
    ingest_reaction(
        conn,
        ReactionEvent("u-2", "✅", "REMOVED", "2026-05-01T12:00:00+00:00"),
        first.message_id,
    )
    ingest_message(
        conn, _event(source_message_id="122", discord_channel_id="DMZ-fresh")
    )  # late duplicate no-op (same channel as the original delivery)
    assert _message_count(conn) == 2
    assert len(_group_rows(conn)) == 1
    assert _mention_count(conn) == 1
    assert _reaction_count(conn) == 2
    _fk_clean(conn)


# Anomalous redelivery: a duplicate delivery must not register side effects.


def test_duplicate_delivery_with_unseen_channel_creates_no_side_effects(
    conn,
) -> None:
    vertical_id = _vertical(conn)
    group_id = _group(conn, "DMZ-1", vertical_id)
    first = ingest_message(conn, _event(source_message_id="130"))
    redelivery = ingest_message(
        conn,
        _event(source_message_id="130", discord_channel_id="DMZ-never-seen"),
    )
    assert redelivery.created is False
    assert redelivery.message_id == first.message_id
    # The stored message keeps its original group; no phantom group row exists.
    assert _message_row(conn, first.message_id)["group_id"] == group_id
    assert len(_group_rows(conn)) == 1
    assert _message_count(conn) == 1
    _fk_clean(conn)


# FR-ING-06: a supplied reply/reference is stored exactly as supplied.


def test_supplied_reply_reference_stored_verbatim(conn) -> None:
    _vertical(conn)
    _group(conn)
    reference = "https://discord.com/channels/789/DMZ-1/109"
    result = ingest_message(
        conn, _event(source_message_id="131", reply_reference=reference)
    )
    assert str(_message_row(conn, result.message_id)["reply_reference"]) == reference
    _fk_clean(conn)


# A4 (text branch): a valid string is stored byte-for-byte, not reserialized.


def test_string_raw_metadata_stored_verbatim(conn) -> None:
    _vertical(conn)
    _group(conn)
    metadata = '{"b":"two",  "a":"one"}'
    result = ingest_message(
        conn, _event(source_message_id="132", raw_metadata=metadata)
    )
    assert str(_message_row(conn, result.message_id)["raw_metadata"]) == metadata
    _fk_clean(conn)


# FR-ING-08: replay through the same path as live messages.


def test_replay_persists_all_records_through_the_live_path(conn) -> None:
    _vertical(conn)
    _group(conn)
    results = ingest_events(
        conn,
        (
            _event(source_message_id="140"),
            _event(source_message_id="141", sender_discord_user_id=None),
            _event(source_message_id="142", event_timestamp=None),
        ),
    )
    assert [r.status for r in results] == [
        RecordStatus.CREATED,
        RecordStatus.CREATED,
        RecordStatus.CREATED,
    ]
    assert len({r.message_id for r in results}) == 3
    row = _message_row(conn, results[2].message_id)
    # An export without second-level timestamps stays missing — indicative
    # only (FR-ING-08); nothing is fabricated.
    assert row["event_timestamp"] is None
    _fk_clean(conn)


def test_repeated_replay_is_idempotent_and_keeps_evidence_unique(conn) -> None:
    _vertical(conn)
    _group(conn)
    batch = (
        _event(
            source_message_id="143",
            mentions=(MentionEvent("u-2", 0), MentionEvent("u-3", None)),
            reactions=(ReactionEvent("u-4", "👍", "ADDED", "2026-05-01T10:05:00+00:00"),),
        ),
        _event(source_message_id="144"),
    )
    first_run = ingest_events(conn, batch)
    duplicate_run = ingest_events(conn, batch)
    assert [r.status for r in first_run] == [
        RecordStatus.CREATED,
        RecordStatus.CREATED,
    ]
    assert [r.status for r in duplicate_run] == [
        RecordStatus.DUPLICATE,
        RecordStatus.DUPLICATE,
    ]
    assert [r.message_id for r in duplicate_run] == [r.message_id for r in first_run]
    assert _message_count(conn) == 2
    assert _mention_count(conn) == 2
    assert _reaction_count(conn) == 1
    _fk_clean(conn)


def test_replay_treats_live_ingested_records_as_duplicates(conn) -> None:
    _vertical(conn)
    _group(conn)
    live = ingest_message(conn, _event(source_message_id="145"))
    results = ingest_events(
        conn,
        (_event(source_message_id="145"), _event(source_message_id="146")),
    )
    assert results[0].status is RecordStatus.DUPLICATE
    assert results[0].message_id == live.message_id
    assert results[1].status is RecordStatus.CREATED
    assert _message_count(conn) == 2
    _fk_clean(conn)


def test_replay_handles_duplicates_inside_one_batch(conn) -> None:
    _vertical(conn)
    _group(conn)
    duplicated = _event(source_message_id="147")
    results = ingest_events(conn, (duplicated, duplicated))
    assert results[0].status is RecordStatus.CREATED
    assert results[1].status is RecordStatus.DUPLICATE
    assert results[1].message_id == results[0].message_id
    assert _message_count(conn) == 1
    _fk_clean(conn)


def test_replay_rejected_record_does_not_abort_neighbors(conn) -> None:
    """P-2: records before and after a rejected record are eligible for
    successful ingestion; the rejected record leaves no trace, not even a
    D-1 channel registration."""
    _group(conn, "DMZ-1")  # pre-existing roster; succeeded records add theirs
    batch = (
        _event(source_message_id="148", discord_channel_id="DMZ-fresh"),
        _event(source_message_id="   "),
        _event(source_message_id="150"),
    )
    results = ingest_events(conn, batch)
    assert [r.status for r in results] == [
        RecordStatus.CREATED,
        RecordStatus.REJECTED,
        RecordStatus.CREATED,
    ]
    assert results[1].message_id is None
    assert results[1].error is not None and "record 1" in results[1].error
    assert _message_count(conn) == 2
    # Only the succeeded records' channel exists; the rejected record's
    # registration (if it had any) never appears.
    channels = {str(g["discord_channel_id"]) for g in _group_rows(conn)}
    assert channels == {"DMZ-1", "DMZ-fresh"}
    _fk_clean(conn)


def test_replay_rejected_record_registers_no_channel(conn) -> None:
    """D-1 consistency: a REJECTED record's unknown channel is never
    registered — auto-registration commits only with its message."""
    _vertical(conn)
    _group(conn)
    results = ingest_events(
        conn,
        (
            _event(source_message_id="166", discord_channel_id="DMZ-good"),
            _event(source_message_id="   ", discord_channel_id="DMZ-bad"),
        ),
    )
    assert [r.status for r in results] == [
        RecordStatus.CREATED,
        RecordStatus.REJECTED,
    ]
    channels = {str(g["discord_channel_id"]) for g in _group_rows(conn)}
    assert channels == {"DMZ-1", "DMZ-good"}
    _fk_clean(conn)


def test_replay_retry_after_partial_success(conn) -> None:
    """Retrying a partially successful batch: stored identities come back as
    DUPLICATE (same id, no evidence writes), rejected records are
    re-evaluated, and afterwards a corrected record can still be created."""
    _vertical(conn)
    _group(conn)
    first = ingest_events(
        conn,
        (
            _event(source_message_id="163"),
            _event(source_message_id="   "),
            _event(source_message_id="165", discord_channel_id="DMZ-fresh"),
        ),
    )
    assert [r.status for r in first] == [
        RecordStatus.CREATED,
        RecordStatus.REJECTED,
        RecordStatus.CREATED,
    ]

    retry = ingest_events(
        conn,
        (
            _event(source_message_id="163"),
            _event(source_message_id="   "),
            _event(source_message_id="165", discord_channel_id="DMZ-fresh"),
        ),
    )

    assert [r.status for r in retry] == [
        RecordStatus.DUPLICATE,
        RecordStatus.REJECTED,
        RecordStatus.DUPLICATE,
    ]
    assert retry[0].message_id == first[0].message_id
    assert retry[2].message_id == first[2].message_id
    assert _message_count(conn) == 2
    assert _mention_count(conn) == 0
    assert _reaction_count(conn) == 0
    corrected = ingest_message(conn, _event(source_message_id="164"))
    assert corrected.created is True
    assert _message_count(conn) == 3
    _fk_clean(conn)


def test_replay_record_scoped_integrity_failure_rolls_back_whole_record(
    conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract boundary: a persistence-time constraint failure binds to that
    record only — its whole record (message, D-1 registration, evidence)
    rolls back via transaction(), the outcome is REJECTED, neighbors are
    untouched. Simulated because the pre-write validation already excludes
    every known CHECK mismatch; this pins the catch-outside-the-transaction
    behavior the design depends on."""
    _vertical(conn)
    _group(conn)
    real_insert = backend.ingest._insert_reaction
    armed = {"count": 1}

    def flaky_insert(
        connection: Any, message_id: int, reaction: Any
    ) -> int:
        if armed["count"] > 0:
            armed["count"] -= 1
            raise sqlite3.IntegrityError("simulated record-scoped constraint failure")
        return real_insert(connection, message_id, reaction)

    monkeypatch.setattr(backend.ingest, "_insert_reaction", flaky_insert)
    results = ingest_events(
        conn,
        (
            _event(source_message_id="160"),  # no reactions: patch not hit
            _event(  # trips the simulated constraint
                source_message_id="161",
                reactions=(ReactionEvent("u-4", "👍", "ADDED"),),
            ),
            _event(  # later record succeeds through the real insert
                source_message_id="162",
                reactions=(ReactionEvent("u-5", "🚀", "ADDED", "2026-05-01T10:03:00+00:00"),),
            ),
        ),
    )
    assert [r.status for r in results] == [
        RecordStatus.CREATED,
        RecordStatus.REJECTED,
        RecordStatus.CREATED,
    ]
    assert results[1].message_id is None
    stored = {
        str(row["source_message_id"]) for row in _message_rows(conn)
    }
    assert stored == {"160", "162"}  # record 161 left no orphan row
    assert _reaction_count(conn) == 1
    _fk_clean(conn)


def test_replay_systemic_failure_aborts_and_reports_durable_prefix(conn) -> None:
    """A non-record-specific failure is never reported as successful: the
    batch raises IngestionBatchError carrying the durable prefix of
    outcomes; nothing further is processed; committed records stay durable."""
    _vertical(conn)
    _group(conn)
    conn.execute("DROP TABLE message_reaction")  # systemic damage
    batch = (
        _event(source_message_id="170"),  # no reactions: storeable
        _event(  # needs the dropped table
            source_message_id="171",
            reactions=(ReactionEvent("u-4", "👍", "ADDED"),),
        ),
        _event(source_message_id="172"),  # never attempted
    )
    with pytest.raises(IngestionBatchError) as caught:
        ingest_events(conn, batch)
    outcomes = caught.value.outcomes
    assert [r.status for r in outcomes] == [RecordStatus.CREATED]
    assert [r.index for r in outcomes] == [0]
    assert "record 1" in str(caught.value)
    # Truth report: the prefix record IS durable in the database.
    assert _message_count(conn) == 1
    stored = {str(row["source_message_id"]) for row in _message_rows(conn)}
    assert stored == {"170"}
    _fk_clean(conn)


def test_replay_outcome_indices_align_with_batch_order(conn) -> None:
    _vertical(conn)
    _group(conn)
    first = _event(source_message_id="167")
    results = ingest_events(
        conn,
        (
            first,
            _event(source_message_id="   "),
            _event(source_message_id="168"),
            first,  # in-batch duplicate
        ),
    )
    assert [r.index for r in results] == [0, 1, 2, 3]
    assert [r.status for r in results] == [
        RecordStatus.CREATED,
        RecordStatus.REJECTED,
        RecordStatus.CREATED,
        RecordStatus.DUPLICATE,
    ]
    assert results[1].message_id is None
    assert results[1].error is not None
    assert results[3].message_id == results[0].message_id
    assert _message_count(conn) == 2
    _fk_clean(conn)


def test_replay_from_second_connection_deduplicates(
    conn, tmp_path: Path
) -> None:
    """NFR-CON-01 under partial success: a second connection replaying the
    same batch sees DUPLICATE identities, never duplicates rows."""
    _vertical(conn)
    _group(conn)
    second = connect(tmp_path / "ingest.db")
    apply_migrations(second)
    try:
        batch = (
            _event(source_message_id="174"),
            _event(source_message_id="175"),
        )
        first_run = ingest_events(conn, batch)
        assert [r.status for r in first_run] == [
            RecordStatus.CREATED,
            RecordStatus.CREATED,
        ]
        redelivery = ingest_events(second, batch)
        assert [r.status for r in redelivery] == [
            RecordStatus.DUPLICATE,
            RecordStatus.DUPLICATE,
        ]
        assert [r.message_id for r in redelivery] == [
            r.message_id for r in first_run
        ]
        assert _message_count(conn) == 2
    finally:
        second.close()
    _fk_clean(conn)


def test_replay_single_record_and_empty_batches(conn) -> None:
    """Contract completeness: empty batch is a no-op; a one-record batch
    returns exactly one outcome, for both outcome classes."""
    _vertical(conn)
    _group(conn)
    assert ingest_events(conn, ()) == ()
    assert _message_count(conn) == 0
    single = ingest_events(conn, (_event(source_message_id="169"),))
    assert [r.status for r in single] == [RecordStatus.CREATED]
    rejected = ingest_events(conn, (_event(source_message_id="   "),))
    assert [r.status for r in rejected] == [RecordStatus.REJECTED]
    assert rejected[0].message_id is None
    assert rejected[0].error is not None and rejected[0].error != ""
    _fk_clean(conn)


def test_replay_rejected_final_record_keeps_earlier_record(conn) -> None:
    """P-2: the earlier valid record of a batch whose last record fails
    validation stays durable and is reported CREATED."""
    _vertical(conn)
    _group(conn)
    ingest_message(conn, _event(source_message_id="151"))  # pre-existing state
    results = ingest_events(
        conn,
        (
            _event(source_message_id="152", sender_discord_user_id=None),
            _event(source_message_id="153", mentions=(MentionEvent(" ", None),)),
        ),
    )
    assert [r.status for r in results] == [
        RecordStatus.CREATED,
        RecordStatus.REJECTED,
    ]
    assert len({r.message_id for r in results if r.message_id is not None}) == 1
    assert _message_count(conn) == 2
    assert _mention_count(conn) == 0
    _fk_clean(conn)
