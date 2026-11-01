"""Shared AI-request context helper tests (Phase 7 — S1).

Requires EIR-03 (docs/SRS.md §5), NFR-SEC-04, DR-03 and AC-21, plus the
Phase 7 plan §4.1 rules pinned here:

1. ``mask_pii`` runs on the request copy only; the stored ``content`` is never
   written, and masking runs *before* markup rewriting.
2. Real Discord markup (``<@ID>``, ``<@!ID>``, ``<@&ID>``, ``<#ID>``,
   ``<:name:ID>``, ``<a:name:ID>``) is replaced; user forms become ``@mN`` and
   no raw Discord ID survives.
3. Handles are assigned in first-appearance order scanning the bucket by
   ascending message id: stored mentions first (position, then row id), then
   content mentions — exactly Phase 5's algorithm, now shared.
4. Reply references resolve to internal message ids of the same bucket, or
   None (DC-19).

The shared helper is what both the relevance and proposal request builders
consume, so these tests are the single contract for handle order and markup.
"""

from __future__ import annotations

import sqlite3

import pytest

from backend.ai_context import (
    MentionHandles,
    bucket_message_entries,
    masked_request_content,
    reply_to_message_id,
    rewrite_markup,
)
from backend.db.connection import connect
from backend.db.migrations import apply_migrations


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    apply_migrations(connection)
    yield connection
    connection.close()


# --- helpers -------------------------------------------------------------


def _group(conn: sqlite3.Connection, group_id: int) -> None:
    conn.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (?, ?, NULL)",
        (group_id, f"chan-{group_id}"),
    )


def _message(
    conn: sqlite3.Connection,
    group_id: int,
    index: int,
    *,
    source_message_id: str | None = None,
    sender_person_id: int | None = None,
    content: str | None = None,
    reply_reference: str | None = None,
    event_timestamp: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO source_message"
        " (source, source_message_id, group_id, sender_person_id,"
        "  event_timestamp, received_at, content, reply_reference)"
        " VALUES ('discord', ?, ?, ?, ?,"
        "         '2026-01-01T10:00:00+00:00', ?, ?)",
        (
            source_message_id
            if source_message_id is not None
            else f"mid-{group_id}-{index}",
            group_id,
            sender_person_id,
            event_timestamp
            if event_timestamp is not None
            else "2026-01-01T10:00:00+00:00",
            content if content is not None else "kuch karo yaar",
            reply_reference,
        ),
    )
    return int(cursor.lastrowid or 0)


def _bucket(conn: sqlite3.Connection, group_id: int) -> int:
    cursor = conn.execute(
        "INSERT INTO bucket (group_id, start_event_timestamp, end_event_timestamp,"
        " parameters_used, processing_state)"
        " VALUES (?, '2026-01-01T10:00:00+00:00', '2026-01-01T10:00:10+00:00',"
        "         '{\"window_w\": 10, \"burst_gap_g\": 5, \"max_duration_m\": 30,"
        "          \"close_grace_c\": 3}', 'CLOSED')",
        (group_id,),
    )
    return int(cursor.lastrowid or 0)


def _membership(
    conn: sqlite3.Connection, bucket_id: int, message_id: int, group_id: int
) -> None:
    conn.execute(
        "INSERT INTO bucket_message (bucket_id, message_id, group_id) VALUES (?, ?, ?)",
        (bucket_id, message_id, group_id),
    )


def _mention(
    conn: sqlite3.Connection, message_id: int, discord_user_id: str, position: int
) -> None:
    conn.execute(
        "INSERT INTO message_mention (message_id, mentioned_discord_user_id, position)"
        " VALUES (?, ?, ?)",
        (message_id, discord_user_id, position),
    )


# --- MentionHandles — first-appearance order (plan §4.1 rule 3) -----------


def test_handle_assignment_follows_first_appearance() -> None:
    handles = MentionHandles()
    assert handles.handle("2003") == "@m1"
    assert handles.handle("2002") == "@m2"
    # A repeated id keeps its original handle (never renumbered).
    assert handles.handle("2003") == "@m1"
    assert handles.handle("5544") == "@m3"


def test_mapping_pairs_handle_with_discord_id_in_order() -> None:
    handles = MentionHandles()
    handles.handle("2003")
    handles.handle("2002")
    handles.handle("2003")
    assert handles.mapping == (("@m1", "2003"), ("@m2", "2002"))


# --- rewrite_markup — every Discord markup form (plan §4.1 rule 2) --------


def test_rewrite_markup_rewrites_every_discord_markup_form() -> None:
    handles = MentionHandles()
    content = (
        "hey <@2002> and <@!2003>, check <#9988776655> and <@&8877665544>"
        " <:custom:7766554433> and <a:anim:6655443322>"
    )

    rewritten = rewrite_markup(content, handles)

    assert rewritten == (
        "hey @m1 and @m2, check #channel and @role :custom: and :anim:"
    )
    assert handles.mapping == (("@m1", "2002"), ("@m2", "2003"))


def test_rewrite_markup_leaves_no_raw_discord_id() -> None:
    handles = MentionHandles()
    rewritten = rewrite_markup(
        "<@2002> <@!2003> <@&8877665544> <#9988776655>"
        " <:custom:7766554433> <a:anim:6655443322>",
        handles,
    )
    assert "<@" not in rewritten
    assert "<#" not in rewritten
    for raw in (
        "2002",
        "2003",
        "8877665544",
        "9988776655",
        "7766554433",
        "6655443322",
    ):
        assert raw not in rewritten


def test_rewrite_markup_without_markup_is_unchanged() -> None:
    handles = MentionHandles()
    assert rewrite_markup("plain text only", handles) == "plain text only"
    assert handles.mapping == ()


# --- masked_request_content — mask first, rewrite second (rule 1) --------


def test_masked_request_content_masks_then_rewrites() -> None:
    handles = MentionHandles()
    content = "call 98765 43210 and mail a.b@site.co <@2002>"
    masked = masked_request_content(content, handles)
    assert masked == "call [PHONE] and mail [EMAIL] @m1"
    assert "[PHONE]" in masked
    assert "[EMAIL]" in masked
    assert "98765 43210" not in masked
    assert "a.b@site.co" not in masked
    assert handles.mapping == (("@m1", "2002"),)


def test_masked_request_content_none_stays_none() -> None:
    handles = MentionHandles()
    assert masked_request_content(None, handles) is None
    assert handles.mapping == ()


def test_masking_runs_before_rewriting_a_phone_shaped_mention() -> None:
    """Rule 1: masking happens first, so a maskable digit run is never
    rewritten into a handle and never survives as a raw id."""
    handles = MentionHandles()
    masked = masked_request_content("<@9876543210>", handles)
    assert masked == "<@[PHONE]>"
    assert "9876543210" not in str(masked)
    assert handles.mapping == ()


# --- reply_to_message_id — same-bucket internal ids or None (DC-19) ------


def test_reply_resolves_to_internal_id_only_within_bucket(
    conn: sqlite3.Connection,
) -> None:
    _group(conn, 1)
    first = _message(conn, 1, 1, source_message_id="snowflake-1")
    second = _message(
        conn, 1, 2, source_message_id="snowflake-2", reply_reference="snowflake-1"
    )
    outside = _message(conn, 1, 3, source_message_id="snowflake-3")
    bucket_ids = frozenset({first, second})

    assert (
        reply_to_message_id(
            conn, "snowflake-1", group_id=1, bucket_message_ids=bucket_ids
        )
        == first
    )
    # A resolved group message that is not in this bucket stays None.
    assert (
        reply_to_message_id(
            conn, "snowflake-3", group_id=1, bucket_message_ids=bucket_ids
        )
        is None
    )
    # An external / unknown reference stays None, never guessed.
    assert (
        reply_to_message_id(
            conn, "external-999", group_id=1, bucket_message_ids=bucket_ids
        )
        is None
    )
    assert outside not in bucket_ids


def test_reply_none_stays_none(conn: sqlite3.Connection) -> None:
    assert (
        reply_to_message_id(
            conn, None, group_id=1, bucket_message_ids=frozenset()
        )
        is None
    )


# --- bucket_message_entries — shared bucket scan (rule 3) ----------------


def test_bucket_entries_assign_stored_mentions_then_content_mentions(
    conn: sqlite3.Connection,
) -> None:
    _group(conn, 1)
    bucket_id = _bucket(conn, 1)
    first = _message(conn, 1, 1, content="hi <@5544>")
    second = _message(conn, 1, 2, content="plain")
    _membership(conn, bucket_id, first, 1)
    _membership(conn, bucket_id, second, 1)
    _mention(conn, first, "2002", 1)
    _mention(conn, first, "2003", 0)
    _mention(conn, second, "2002", 0)

    entries, mapping = bucket_message_entries(conn, bucket_id)

    assert [entry.message_id for entry in entries] == [first, second]
    # Stored mentions bucket-wide first (position order), then content.
    assert mapping == (("@m1", "2003"), ("@m2", "2002"), ("@m3", "5544"))
    assert entries[0].mention_handles == ("@m1", "@m2", "@m3")
    assert entries[0].masked_content == "hi @m3"
    assert entries[1].mention_handles == ("@m2",)
    assert entries[1].masked_content == "plain"


def test_bucket_entries_resolve_replies_and_keep_sender_fields(
    conn: sqlite3.Connection,
) -> None:
    _group(conn, 1)
    # PB-5: phone and roll_number mandatory unique synthetic columns
    conn.execute(
        "INSERT INTO person (id, name, discord_user_id, phone, role, active, roll_number)"
        " VALUES (7, 'Neha', '2002', '+919800003007', 'AH', 1, 'AI-007')"
    )
    bucket_id = _bucket(conn, 1)
    first = _message(
        conn, 1, 1, source_message_id="snowflake-1", sender_person_id=7
    )
    second = _message(
        conn,
        1,
        2,
        source_message_id="snowflake-2",
        sender_person_id=7,
        reply_reference="snowflake-1",
        event_timestamp="2026-01-01T10:00:05+00:00",
    )
    _membership(conn, bucket_id, first, 1)
    _membership(conn, bucket_id, second, 1)

    entries, _mapping = bucket_message_entries(conn, bucket_id)

    assert entries[0].reply_to_message_id is None
    assert entries[1].reply_to_message_id == first
    assert entries[1].sender_person_id == 7
    assert entries[1].sender_name == "Neha"
    assert entries[1].event_timestamp == "2026-01-01T10:00:05+00:00"


def test_bucket_entries_never_write_back_masked_content(
    conn: sqlite3.Connection,
) -> None:
    """AC-21/DR-03: the masking and rewriting happen on the request copy."""
    _group(conn, 1)
    bucket_id = _bucket(conn, 1)
    original = "call 98765 43210 <@2002>"
    mid = _message(conn, 1, 1, content=original)
    _membership(conn, bucket_id, mid, 1)

    bucket_message_entries(conn, bucket_id)

    stored = conn.execute(
        "SELECT content FROM source_message WHERE id = ?", (mid,)
    ).fetchone()
    assert stored["content"] == original


def test_bucket_entries_of_empty_bucket_are_empty(
    conn: sqlite3.Connection,
) -> None:
    _group(conn, 1)
    bucket_id = _bucket(conn, 1)
    entries, mapping = bucket_message_entries(conn, bucket_id)
    assert entries == ()
    assert mapping == ()
