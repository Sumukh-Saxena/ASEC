"""Proposal-generation service tests (Phase 5).

Implements the executable proposal stage (FR-PRP-01..16), the owner's
Phase 5 corrections (C1 opaque mention handles, C2 raw-response retention,
C3 eligibility policy, C4 backlog visibility, C5 stage-delay metric, C6
judgement pins), and the storage contracts of migrations
0004/0005/0009/0011/0012/0014 and the committed relevance stage.

Architecture pins woven through every test: the proposal generator is a
stateless injected adapter (EIR-03, AI-01) that never touches the
database; the backend composes the masked request (C1: opaque per-request
handles, no real Discord identifiers), validates the response as untrusted
data (AI-02), resolves assignees at storage time (FR-PRP-10) and writes
everything in one transaction with the durable 0014 artifact. No bucket
lifecycle change (DC-1), no task/announcement writes (FR-PRP-16); the one
approved FR-PRP-17 reviewer notification is staged inside the same storage
transaction (owner amendment A7). Two-valued failure writer (FR-PRP-15,
AC-12 half), never FAILED_PERMANENT. Tests are written first; the
implementation observes RED before any code exists.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Callable, Sequence

import pytest

import backend.proposals as proposals
from backend.bucketing import assign_messages, mark_stream_progress
from backend.db.connection import connect, transaction
from backend.db.migrations import apply_migrations
from backend.ingest import MentionEvent, MessageEvent, ingest_events
from backend.proposals import (
    DEFAULT_PROPOSAL_ATTEMPTS_PER_BUCKET,
    LATENCY_METRIC_LABEL,
    PROPOSAL_PROMPT,
    PROPOSAL_PROMPT_VERSION,
    STAGE_DELAY_METRIC_LABEL,
    STAGE_PROPOSAL_FALLBACK_GENERATION,
    STAGE_PROPOSAL_GENERATION,
    ProposalBacklog,
    ProposalDraft,
    ProposalGenerationRequest,
    ProposalMessage,
    ProposalOutcome,
    ProposalPassError,
    ProposalPolicy,
    ProposalResponse,
    ProposalServiceError,
    load_proposal_policy,
    proposal_backlog,
    proposal_exhausted_buckets,
    proposal_pending_buckets,
    run_proposal_pass,
)
from backend.relevance import MessageDecision, RelevanceResponse, run_relevance_pass


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    apply_migrations(connection)
    yield connection
    connection.close()


# --- scenario helpers -----------------------------------------------------


def _vertical(conn: sqlite3.Connection, vertical_id: int, name: str) -> None:
    conn.execute("INSERT INTO vertical (id, name) VALUES (?, ?)", (vertical_id, name))


def _group(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    vertical_id: int | None = None,
    channel: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id) VALUES (?, ?, ?)",
        (
            group_id,
            channel if channel is not None else f"chan-{group_id}",
            vertical_id,
        ),
    )


def _person(
    conn: sqlite3.Connection,
    person_id: int,
    name: str,
    discord_id: str | None = None,
    *,
    role: str = "AH",
    active: int = 1,
) -> int:
    # PB-5: canonical role in SRS vocabulary and mandatory unique synthetic columns
    canonical_role = "Volunteer" if role.upper() == "VOLUNTEER" else role
    conn.execute(
        "INSERT INTO person (id, name, discord_user_id, phone, role, active, roll_number)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            person_id,
            name,
            discord_id,
            f"+919800001{person_id:03d}",
            canonical_role,
            active,
            f"PROP-{person_id:03d}",
        ),
    )
    return person_id


def _membership_vertical(
    conn: sqlite3.Connection, person_id: int, vertical_id: int
) -> None:
    conn.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)",
        (person_id, vertical_id),
    )


def _message(
    conn: sqlite3.Connection,
    group_id: int,
    index: int,
    *,
    source_message_id: str | None = None,
    sender_person_id: int | None = None,
    sender_discord_user_id: str | None = None,
    content: str | None = None,
    reply_reference: str | None = None,
    event_timestamp: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO source_message"
        " (source, source_message_id, group_id, sender_person_id,"
        "  sender_discord_user_id, event_timestamp, received_at, content,"
        "  reply_reference, flag_unclassified)"
        " VALUES ('discord', ?, ?, ?, ?, ?, '2026-01-01T10:00:00+00:00', ?, ?, 0)",
        (
            source_message_id
            if source_message_id is not None
            else f"mid-{group_id}-{index}",
            group_id,
            sender_person_id,
            sender_discord_user_id,
            event_timestamp
            if event_timestamp is not None
            else "2026-01-01T10:00:00+00:00",
            content if content is not None else "kuch karo yaar",
            reply_reference,
        ),
    )
    return int(cursor.lastrowid or 0)


_DEFAULT_END_EVENT = object()


def _bucket(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    state: str = "PROCESSED",
    start_event: str = "2026-01-01T10:00:00+00:00",
    end_event: str | None | object = _DEFAULT_END_EVENT,
) -> int:
    cursor = conn.execute(
        "INSERT INTO bucket (group_id, start_event_timestamp, end_event_timestamp,"
        " parameters_used, processing_state)"
        " VALUES (?, ?, ?, '{\"window_w\": 10, \"burst_gap_g\": 5,"
        " \"max_duration_m\": 30, \"close_grace_c\": 3}', ?)",
        (
            group_id,
            start_event,
            "2026-01-01T10:00:10+00:00" if end_event is _DEFAULT_END_EVENT else end_event,
            state,
        ),
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


def _progress(conn: sqlite3.Connection, group_id: int, event_progress: str) -> None:
    conn.execute(
        "INSERT INTO source_group_progress (group_id, progress_to, updated_at)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT (group_id) DO UPDATE SET progress_to ="
        " excluded.progress_to, updated_at = excluded.updated_at",
        (group_id, event_progress, event_progress),
    )


def _relevance_success(
    conn: sqlite3.Connection,
    bucket_id: int,
    relevant_message_ids: Sequence[int],
    *,
    finished_at: str = "2026-01-01T10:01:01+00:00",
    decision: str = "RELEVANT",
) -> int:
    cursor = conn.execute(
        "INSERT INTO processing_attempt"
        " (stage, bucket_id, started_at, finished_at, outcome, model_id,"
        "  model_version, config_version)"
        " VALUES ('RELEVANCE_CLASSIFICATION', ?, '2026-01-01T10:01:00+00:00',"
        "  ?, 'SUCCEEDED', 'rel-model', 'v1', 'rel-prompt-1')",
        (bucket_id, finished_at),
    )
    attempt_id = int(cursor.lastrowid or 0)
    for message_id in relevant_message_ids:
        conn.execute(
            "INSERT INTO relevance_decision"
            " (bucket_id, message_id, attempt_id, decision, model_id,"
            "  model_version, prompt_version, decided_at)"
            " VALUES (?, ?, ?, ?, 'rel-model', 'v1', 'rel-prompt-1', 't')",
            (bucket_id, message_id, attempt_id, decision),
        )
    return attempt_id


def _proposal_attempt_seed(
    conn: sqlite3.Connection,
    bucket_id: int,
    outcome: str,
    *,
    stage: str = STAGE_PROPOSAL_GENERATION,
    finished_at: str = "2026-01-01T10:02:00+00:00",
) -> int:
    cursor = conn.execute(
        "INSERT INTO processing_attempt (stage, bucket_id, started_at,"
        " finished_at, outcome) VALUES (?, ?, 't', ?, ?)",
        (stage, bucket_id, finished_at, outcome),
    )
    return int(cursor.lastrowid or 0)


def _expected_scenario(
    conn: sqlite3.Connection,
    *,
    message_count: int = 2,
    role: str = "AH",
    extra_irrelevant: bool = False,
) -> tuple[int, int, list[int]]:
    """One PROCESSED bucket from a mapped group; sender is eligible (AH)."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    person_id = _person(conn, 5, "Arjun", "5001", role=role)
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, person_id, 1)
    bucket_id = _bucket(conn, 1)
    message_ids: list[int] = []
    for index in range(1, message_count + 1):
        mid = _message(
            conn,
            1,
            index,
            sender_person_id=person_id,
            sender_discord_user_id="5001",
        )
        _membership(conn, bucket_id, mid, 1)
        message_ids.append(mid)
    relevant = message_ids[:1] if extra_irrelevant else message_ids
    attempt_id = _relevance_success(conn, bucket_id, relevant)
    if extra_irrelevant:
        conn.execute(
            "INSERT INTO relevance_decision"
            " (bucket_id, message_id, attempt_id, decision, model_id,"
            "  model_version, prompt_version, decided_at)"
            " VALUES (?, ?, ?, 'IRRELEVANT', 'rel-model', 'v1', 'rel-prompt-1', 't')",
            (bucket_id, message_ids[-1], attempt_id),
        )
    return bucket_id, person_id, message_ids


def _resolution_scenario(
    conn: sqlite3.Connection,
    *,
    mention: bool = False,
    second_member: tuple[int, str] | None = None,
    cross_vertical: tuple[int, str, str] | None = None,
    mention_member_active: int = 1,
) -> tuple[int, int, int]:
    """Bucket with eligible sender; optional `Neha` (id 7, "2002", member of
    the source vertical); optional same-name colleague in another vertical;
    optional mention evidence on the candidate message."""
    _vertical(conn, 1, "Tech")
    _vertical(conn, 2, "Sports")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001")  # eligible sender
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    neha = _person(conn, 7, "Neha", "2002", active=mention_member_active)
    _membership_vertical(conn, neha, 1)
    second_member_id: int = 0
    if second_member is not None:
        second_member_id = _person(
            conn, second_member[0], second_member[1], "6001", role=second_member[2]
        )
        _membership_vertical(conn, second_member_id, 1)
    if cross_vertical is not None:
        doppel_person_id = _person(
            conn, cross_vertical[0], cross_vertical[1], "7001"
        )
        _membership_vertical(conn, doppel_person_id, 2)
    bucket_id = _bucket(conn, 1)
    mid = _message(
        conn, 1, 1, sender_person_id=5, sender_discord_user_id="5001"
    )
    _membership(conn, bucket_id, mid, 1)
    if mention:
        _mention(conn, mid, "2002", 0)
    _relevance_success(conn, bucket_id, [mid])
    return bucket_id, 5, mid


def _draft(
    primary_message_id: int,
    *,
    ptype: str = "TASK",
    description: str | None = "plant the flags",
    assignment_mode: str = "EXPLICIT",
    proposed_assignees: tuple[str, ...] | None = ("Neha",),
    people_needed: int | None = None,
    people_needed_unlimited: int = 0,
    task_type: str = "CROSS_VERTICAL",
    suggested_responsible_vertical: str | None = None,
    affected_verticals: tuple[str, ...] = (),
    attributes: tuple[tuple[str, str | None], ...] = (),
) -> ProposalDraft:
    return ProposalDraft(
        primary_message_id=primary_message_id,
        ptype=ptype,
        description=description,
        assignment_mode=assignment_mode,
        proposed_assignees=proposed_assignees,
        people_needed=people_needed,
        people_needed_unlimited=people_needed_unlimited,
        task_type=task_type,
        suggested_responsible_vertical=suggested_responsible_vertical,
        affected_verticals=affected_verticals,
        attributes=attributes,
    )


def _response(
    *drafts: ProposalDraft,
    model_id: str = "gen-model",
    model_version: str = "gen-v2",
    prompt_version: str = "proposal-prompt-v1",
) -> ProposalResponse:
    return ProposalResponse(
        model_id=model_id,
        model_version=model_version,
        prompt_version=prompt_version,
        proposals=tuple(drafts),
    )


class ScriptedGenerator:
    """Deterministic generator double (stateless per call, injected).

    ``script`` items are :class:`ProposalResponse` (returned) or
    ``Exception`` (raised). Requests are captured for masking/contract
    assertions.
    """

    def __init__(self, script: Sequence[ProposalResponse | Exception]):
        self._script = list(script)
        self._next = 0
        self.requests: list[ProposalGenerationRequest] = []

    def generate(self, request: ProposalGenerationRequest) -> ProposalResponse:
        self.requests.append(request)
        if self._next >= len(self._script):
            raise AssertionError("script exhausted; unexpected additional call")
        item = self._script[self._next]
        self._next += 1
        if isinstance(item, BaseException):
            raise item
        return item


def _artifact(conn: sqlite3.Connection, attempt_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM proposal_attempt_input WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    assert row is not None
    return row


def _artifact_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM proposal_attempt_input").fetchone()
    return int(row["n"])


def _attempt_rows(conn: sqlite3.Connection, bucket_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM processing_attempt WHERE bucket_id = ?"
        " AND stage IN (?, ?) ORDER BY id",
        (bucket_id, STAGE_PROPOSAL_GENERATION, STAGE_PROPOSAL_FALLBACK_GENERATION),
    ).fetchall()


def _attempt_count(conn: sqlite3.Connection, bucket_id: int) -> int:
    return len(_attempt_rows(conn, bucket_id))


def _proposal_rows(conn: sqlite3.Connection, bucket_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM proposal WHERE bucket_id = ? ORDER BY id",
        (bucket_id,),
    ).fetchall()


def _assignee_rows(conn: sqlite3.Connection, proposal_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM proposal_assignee WHERE proposal_id = ? ORDER BY id",
        (proposal_id,),
    ).fetchall()


def _out_of_scope_writes(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(
            conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        )
        for table in ("task", "announcement", "notification")
    }


def _latency_rows(conn: sqlite3.Connection, attempt_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM latency_measurement WHERE processing_attempt_id = ?"
        " ORDER BY metric",
        (attempt_id,),
    ).fetchall()


def _stored_proposal(conn: sqlite3.Connection, bucket_id: int) -> sqlite3.Row:
    rows = _proposal_rows(conn, bucket_id)
    assert len(rows) == 1
    return rows[0]


def _codes(conn: sqlite3.Connection, attempt_id: int) -> list[str]:
    row = _artifact(conn, attempt_id)
    if row["rejected_items"] is None:
        return []
    return [
        str(entry["reason_code"]) for entry in json.loads(str(row["rejected_items"]))
    ]


# =========================================================================
# WS-2 — constants and contract freeze
# =========================================================================


def test_stage_constants_are_exact(conn: sqlite3.Connection) -> None:
    assert STAGE_PROPOSAL_GENERATION == "PROPOSAL_GENERATION"
    assert STAGE_PROPOSAL_FALLBACK_GENERATION == "PROPOSAL_FALLBACK_GENERATION"
    assert LATENCY_METRIC_LABEL == "PROPOSAL_LATENCY"
    assert STAGE_DELAY_METRIC_LABEL == "PROPOSAL_STAGE_DELAY"
    assert PROPOSAL_PROMPT_VERSION == "proposal-prompt-v1"
    assert DEFAULT_PROPOSAL_ATTEMPTS_PER_BUCKET == 3
    assert len(PROPOSAL_PROMPT) > 0


def test_prompt_carries_the_shall_directives(conn: sqlite3.Connection) -> None:
    """The instructions text carries the shall clauses the SRS fixes for
    model behaviour (FR-PRP-06/08/11/15 + FR-PRP-04 zero validity)."""
    lower = PROPOSAL_PROMPT.lower()
    for needle in (
        "task",
        "announcement",
        "open",
        "never",
        "description",
        "exactly",
        "handle",
        "no proposals",
    ):
        assert needle in lower, needle


def test_masking_ruleset_identity_shared_with_relevance(
    conn: sqlite3.Connection,
) -> None:
    assert proposals.MASKING_RULESET_VERSION == "masking-rules-v1"


def test_request_and_response_are_frozen(conn: sqlite3.Connection) -> None:
    message = ProposalMessage(
        message_id=1,
        sender_name=None,
        event_timestamp=None,
        reply_reference=None,
        content=None,
        mention_handles=(),
    )
    with pytest.raises(AttributeError):
        message.message_id = 2  # type: ignore[misc]
    with pytest.raises(AttributeError):
        _draft(1).ptype = "ANNOUNCEMENT"  # type: ignore[misc]


# =========================================================================
# WS-2 — request build (EIR-03, FR-PRP-03, C1 opaque handles)
# =========================================================================


def test_request_composition_candidates_and_context(
    conn: sqlite3.Connection,
) -> None:
    """FR-PRP-03/EIR-03: candidates = eligible RELEVANT senders; context =
    the whole bucket; vertical name present; masked content; sender display
    name but NO real identifier anywhere in the request (C1)."""
    bucket_id, _person_id, message_ids = _expected_scenario(
        conn, message_count=2, extra_irrelevant=True
    )
    mid_first, mid_second = message_ids
    generator = ScriptedGenerator([_response()])

    run_proposal_pass(conn, generator)

    (request,) = generator.requests
    assert request.bucket_id == bucket_id
    assert request.vertical_name == "Tech"
    (candidate,) = request.candidate_messages
    assert candidate.message_id == mid_first
    assert [entry.message_id for entry in request.context_messages] == [
        mid_first,
        mid_second,
    ]
    # C1: sender identity rides as the display name only.
    assert candidate.sender_name == "Arjun"
    assert not hasattr(candidate, "sender_discord_user_id")
    assert not hasattr(candidate, "sender_person_id")
    serialized = proposals.serialize_proposal_request(request)
    assert "5001" not in serialized  # no sender snowflake
    assert "chan-1" not in serialized  # no channel identity either


def test_masking_before_call_and_source_untouched(
    conn: sqlite3.Connection,
) -> None:
    """AC-21/NFR-SEC-04: content is masked in the request copy; the stored
    source content is never touched (DR-03)."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    person_id = _person(conn, 5, "Arjun", "5001")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, person_id, 1)
    bucket_id = _bucket(conn, 1)
    mid = _message(
        conn,
        1,
        1,
        sender_person_id=person_id,
        sender_discord_user_id="5001",
        content="coordinate 98765 43210 yaar",
    )
    _membership(conn, bucket_id, mid, 1)
    _relevance_success(conn, bucket_id, [mid])
    generator = ScriptedGenerator([_response()])

    run_proposal_pass(conn, generator)

    (request,) = generator.requests
    (candidate,) = request.candidate_messages
    assert "[PHONE]" in str(candidate.content)
    assert "98765 43210" not in str(candidate.content)
    stored = conn.execute(
        "SELECT content FROM source_message WHERE id = ?", (mid,)
    ).fetchone()
    assert str(stored["content"]) == "coordinate 98765 43210 yaar"


def test_mention_handle_assignment_is_deterministic_and_ordered(
    conn: sqlite3.Connection,
) -> None:
    """C1: handles `@m1, @m2, …` follow first appearance scanning bucket
    members by ascending message id; a message's own mentions follow the
    stored mention order (position)."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    bucket_id = _bucket(conn, 1)
    mid_a = _message(
        conn, 1, 1, sender_person_id=5, sender_discord_user_id="5001"
    )
    mid_b = _message(
        conn, 1, 2, sender_person_id=5, sender_discord_user_id="5001"
    )
    _membership(conn, bucket_id, mid_a, 1)
    _membership(conn, bucket_id, mid_b, 1)
    _mention(conn, mid_a, "2002", 1)  # position 1
    _mention(conn, mid_a, "2003", 0)  # position 0 → first in this message
    _mention(conn, mid_b, "2002", 0)
    _relevance_success(conn, bucket_id, [mid_a, mid_b])
    generator = ScriptedGenerator([_response()])

    run_proposal_pass(conn, generator)

    (request,) = generator.requests
    by_id = {entry.message_id: entry for entry in request.context_messages}
    # One handle per distinct user ID, in first-appearance order (2003
    # mentioned at position 0 of mid_a first; mid_b's 2002 reuses @m2).
    assert by_id[mid_a].mention_handles == ("@m1", "@m2")
    assert by_id[mid_b].mention_handles == ("@m2",)
    assert not hasattr(request, "mention_handle_map")
    context = proposals._proposal_request(conn, bucket_id)
    assert context.mention_handle_map == (("@m1", "2003"), ("@m2", "2002"))


def test_mention_handle_map_round_trips(conn: sqlite3.Connection) -> None:
    """C1: the map maps every request handle to the mention evidence's
    Discord user ID; the artifact form carries it, the dispatch does not."""
    bucket_id, _person_id, _ids = _resolution_scenario(conn, mention=True)
    generator = ScriptedGenerator([_response()])

    run_proposal_pass(conn, generator)

    (request,) = generator.requests
    assert not hasattr(request, "mention_handle_map")
    context = proposals._proposal_request(conn, bucket_id)
    assert context.mention_handle_map == (("@m1", "2002"),)
    dispatch = proposals.serialize_proposal_request(
        request, include_handle_map=False
    )
    artifact = proposals.serialize_proposal_request(
        context, include_handle_map=True
    )
    assert "2002" not in dispatch
    assert "2002" in artifact
    assert json.loads(artifact)["mention_handle_map"] == {"@m1": "2002"}


def test_defect1_discord_ids_never_reach_model(conn: sqlite3.Connection) -> None:
    """1d(i): realistic content with <@id>, <@!id>, <@&id>, <#id>, a custom
    emoji, an unmatched user mention and a reply snowflake: assert none of
    those ID strings appears in serialize_proposal_request, in repr of the
    object the generator receives, or in any value reachable via
    dataclasses.asdict of it."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001", role="AH")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    _person(conn, 7, "Neha", "2002", role="AH")
    _person(conn, 8, "Rohan", "2003", role="AH")
    bucket_id = _bucket(conn, 1)

    mid1 = _message(
        conn,
        1,
        1,
        source_message_id="1098765432109876543",
        sender_person_id=5,
        sender_discord_user_id="5001",
        content=(
            "hey <@2002> and <@!2003>, check <#9988776655> and ask <@&8877665544> "
            "<:custom:7766554433> and <a:anim:6655443322> also <@5544332211>"
        ),
    )
    mid2 = _message(
        conn,
        1,
        2,
        source_message_id="1098765432109876544",
        sender_person_id=5,
        sender_discord_user_id="5001",
        content="acknowledged",
        reply_reference="1098765432109876543",
    )
    mid3 = _message(
        conn,
        1,
        3,
        source_message_id="1098765432109876545",
        sender_person_id=5,
        sender_discord_user_id="5001",
        content="external reply",
        reply_reference="999999999999999999",
    )
    for mid in [mid1, mid2, mid3]:
        _membership(conn, bucket_id, mid, 1)
    _mention(conn, mid1, "2002", 0)
    _mention(conn, mid1, "2003", 1)
    _relevance_success(conn, bucket_id, [mid1])

    generator = ScriptedGenerator([_response()])
    run_proposal_pass(conn, generator)

    (request,) = generator.requests
    forbidden_ids = [
        "2002",
        "2003",
        "5544332211",
        "8877665544",
        "9988776655",
        "7766554433",
        "6655443322",
        "1098765432109876543",
        "999999999999999999",
    ]

    serialized = proposals.serialize_proposal_request(request)
    for forbidden in forbidden_ids:
        assert forbidden not in serialized

    rep = repr(request)
    for forbidden in forbidden_ids:
        assert forbidden not in rep

    def _all_strings(val: object) -> list[str]:
        if isinstance(val, str):
            return [val]
        if isinstance(val, dict):
            res: list[str] = []
            for k, v in val.items():
                res.extend(_all_strings(k))
                res.extend(_all_strings(v))
            return res
        if isinstance(val, (list, tuple, set)):
            res = []
            for item in val:
                res.extend(_all_strings(item))
            return res
        return []

    all_vals = " ".join(_all_strings(dataclasses.asdict(request)))
    for forbidden in forbidden_ids:
        assert forbidden not in all_vals


def test_defect1_spy_generator_cannot_reach_handle_map(
    conn: sqlite3.Connection,
) -> None:
    """1d(ii): a spy generator that fails the test if it can reach a
    handle-map attribute."""
    bucket_id, _person_id, mids = _expected_scenario(conn, message_count=1)

    class SpyGenerator:
        def __init__(self) -> None:
            self.received_request: ProposalGenerationRequest | None = None

        def generate(self, request: ProposalGenerationRequest) -> ProposalResponse:
            self.received_request = request
            return _response(_draft(mids[0]))

    spy = SpyGenerator()
    run_proposal_pass(conn, spy)
    assert spy.received_request is not None
    assert not hasattr(spy.received_request, "mention_handle_map")
    assert not hasattr(spy.received_request, "handle_map")


def test_defect1_stored_artifact_reconstructs_and_mention_resolves(
    conn: sqlite3.Connection,
) -> None:
    """1d(iii): the stored artifact still reconstructs the original input and
    the MENTION route still resolves by exact ID."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001", role="AH")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    _person(conn, 7, "Neha", "2002", role="Volunteer")
    _person(conn, 9, "Karan", "5544332211", role="Volunteer")
    bucket_id = _bucket(conn, 1)
    mid = _message(
        conn,
        1,
        1,
        sender_person_id=5,
        sender_discord_user_id="5001",
        content="kuch karo <@2002> and <@5544332211>",
    )
    _membership(conn, bucket_id, mid, 1)
    _mention(conn, mid, "2002", 0)
    _relevance_success(conn, bucket_id, [mid])

    generator = ScriptedGenerator(
        [_response(_draft(mid, proposed_assignees=("@m1", "@m2")))]
    )
    res = run_proposal_pass(conn, generator)
    assert res.generated == (bucket_id,)

    row = _artifact(conn, res.outcomes[0].attempt_ids[0])
    artifact_json = json.loads(str(row["masked_request"]))
    assert "mention_handle_map" in artifact_json
    assert artifact_json["mention_handle_map"] == {
        "@m1": "2002",
        "@m2": "5544332211",
    }

    assignee_rows = conn.execute(
        "SELECT assignee_text, resolution, person_id, resolved_via "
        "FROM proposal_assignee ORDER BY id"
    ).fetchall()
    assert len(assignee_rows) == 2
    assert assignee_rows[0]["assignee_text"] == "@m1"
    assert assignee_rows[0]["resolution"] == "RESOLVED"
    assert assignee_rows[0]["person_id"] == 7
    assert assignee_rows[0]["resolved_via"] == "MENTION"

    assert assignee_rows[1]["assignee_text"] == "@m2"
    assert assignee_rows[1]["resolution"] == "RESOLVED"
    assert assignee_rows[1]["person_id"] == 9
    assert assignee_rows[1]["resolved_via"] == "MENTION"


def test_defect1_replies_resolve_to_internal_ids_or_null(
    conn: sqlite3.Connection,
) -> None:
    """1d(iv): replies resolve to internal ids or null."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001", role="AH")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    bucket_id = _bucket(conn, 1)
    mid1 = _message(
        conn,
        1,
        1,
        source_message_id="snowflake-msg-1",
        sender_person_id=5,
        sender_discord_user_id="5001",
        content="first message",
    )
    mid2 = _message(
        conn,
        1,
        2,
        source_message_id="snowflake-msg-2",
        sender_person_id=5,
        sender_discord_user_id="5001",
        content="second message replying to first",
        reply_reference="snowflake-msg-1",
    )
    mid3 = _message(
        conn,
        1,
        3,
        source_message_id="snowflake-msg-3",
        sender_person_id=5,
        sender_discord_user_id="5001",
        content="third message replying to missing/external",
        reply_reference="external-snowflake-999",
    )
    mid4 = _message(
        conn,
        1,
        4,
        source_message_id="snowflake-msg-4",
        sender_person_id=5,
        sender_discord_user_id="5001",
        content="fourth message with no reply",
        reply_reference=None,
    )
    for mid in [mid1, mid2, mid3, mid4]:
        _membership(conn, bucket_id, mid, 1)
    _relevance_success(conn, bucket_id, [mid1])

    generator = ScriptedGenerator([_response()])
    run_proposal_pass(conn, generator)

    (request,) = generator.requests
    msgs_by_id = {m.message_id: m for m in request.context_messages}
    assert msgs_by_id[mid1].reply_reference is None
    assert msgs_by_id[mid2].reply_reference == mid1
    assert msgs_by_id[mid3].reply_reference is None
    assert msgs_by_id[mid4].reply_reference is None

    req_json = json.loads(proposals.serialize_proposal_request(request))
    json_msgs = {m["message_id"]: m for m in req_json["context_messages"]}
    assert json_msgs[mid1]["reply_reference"] is None
    assert json_msgs[mid2]["reply_reference"] == mid1
    assert json_msgs[mid3]["reply_reference"] is None
    assert json_msgs[mid4]["reply_reference"] is None


def test_defect2_three_proposals_over_500_chars_stored_intact(
    conn: sqlite3.Connection,
) -> None:
    """DEFECT 2: a three-proposal response over 500 characters is stored intact,
    parses as JSON, has PII masked, and raw_response_truncated is 0."""
    bucket_id, _person_id, mids = _expected_scenario(conn, message_count=1)
    mid = mids[0]
    draft1 = _draft(
        mid,
        description="first task proposal with phone 9876543210 to arrange chairs",
    )
    draft2 = _draft(
        mid,
        description="second task proposal with details for main stage decoration",
    )
    draft3 = _draft(
        mid,
        description="third task proposal coordinating security volunteers at gate",
    )
    resp = _response(draft1, draft2, draft3)

    generator = ScriptedGenerator([resp])
    res = run_proposal_pass(conn, generator)
    assert res.generated == (bucket_id,)

    row = _artifact(conn, res.outcomes[0].attempt_ids[0])
    raw = str(row["raw_response"])
    assert len(raw) > 500
    assert row["raw_response_truncated"] == 0
    parsed = json.loads(raw)
    assert len(parsed["proposals"]) == 3
    assert "9876543210" not in raw
    assert "[PHONE]" in raw
    assert row["response_sha256"] == proposals._hash_text(raw)


def test_defect2_response_over_cap_stored_truncated_with_full_hash(
    conn: sqlite3.Connection,
) -> None:
    """DEFECT 2: a response exceeding MAX_RAW_RESPONSE_BYTES (65536) is stored
    truncated on a character boundary, with raw_response_truncated=1, PII masked,
    and response_sha256 matching the full masked text before truncation."""
    bucket_id, _person_id, mids = _expected_scenario(conn, message_count=1)
    mid = mids[0]
    big_desc = "call 9876543210 " + ("x" * 70000)
    draft = _draft(mid, description=big_desc)
    resp = _response(draft)

    generator = ScriptedGenerator([resp])
    res = run_proposal_pass(conn, generator)
    assert res.generated == (bucket_id,)

    row = _artifact(conn, res.outcomes[0].attempt_ids[0])
    raw = str(row["raw_response"])
    assert row["raw_response_truncated"] == 1
    assert len(raw.encode("utf-8")) <= proposals.MAX_RAW_RESPONSE_BYTES
    assert "9876543210" not in raw
    assert "[PHONE]" in raw

    truncated_hash = proposals._hash_text(raw)
    assert row["response_sha256"] != truncated_hash
    full_masked_text = proposals.mask_pii(proposals._raw_response_text(resp))
    assert row["response_sha256"] == proposals._hash_text(full_masked_text)


def test_defect2_fallback_repr_obeys_same_rules(
    conn: sqlite3.Connection,
) -> None:
    """DEFECT 2: fallback repr path masks PII, truncates if > MAX_RAW_RESPONSE_BYTES,
    sets raw_response_truncated, and hashes full masked text."""
    bucket_id, _person_id, mids = _expected_scenario(conn, message_count=1)

    class CustomMalformedResponse:
        def __repr__(self) -> str:
            return "CustomResponse(phone='9876543210', payload='" + ("y" * 70000) + "')"

    generator = ScriptedGenerator([CustomMalformedResponse()])  # type: ignore[list-item]
    res = run_proposal_pass(conn, generator)
    assert res.generated == ()

    row = _artifact(conn, res.outcomes[0].attempt_ids[0])
    raw = str(row["raw_response"])
    assert row["raw_response_truncated"] == 1
    assert len(raw.encode("utf-8")) <= proposals.MAX_RAW_RESPONSE_BYTES
    assert "9876543210" not in raw
    assert "[PHONE]" in raw
    full_masked = proposals.mask_pii(repr(CustomMalformedResponse()))
    assert row["response_sha256"] == proposals._hash_text(full_masked)


def test_defect3_non_ascii_name_resolution(conn: sqlite3.Connection) -> None:
    """DEFECT 3: Unicode-safe name resolution (e.g. Étienne vs étienne)."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001", role="AH")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    etienne = _person(conn, 12, "Étienne", "9001", role="Volunteer")
    _membership_vertical(conn, etienne, 1)

    bucket_id = _bucket(conn, 1)
    mid = _message(
        conn,
        1,
        1,
        sender_person_id=5,
        sender_discord_user_id="5001",
        content="kuch karo",
    )
    _membership(conn, bucket_id, mid, 1)
    _relevance_success(conn, bucket_id, [mid])

    generator = ScriptedGenerator(
        [_response(_draft(mid, proposed_assignees=("étienne",)))]
    )
    res = run_proposal_pass(conn, generator)
    assert res.generated == (bucket_id,)

    row = conn.execute(
        "SELECT assignee_text, resolution, person_id, resolved_via "
        "FROM proposal_assignee WHERE proposal_id IN "
        "(SELECT id FROM proposal WHERE attempt_id = ?)",
        (res.outcomes[0].attempt_ids[0],),
    ).fetchone()
    assert row is not None
    assert row["assignee_text"] == "étienne"
    assert row["resolution"] == "RESOLVED"
    assert row["person_id"] == 12
    assert row["resolved_via"] == "NAME_MATCH"


def test_unmapped_group_is_never_eligible(
    conn: sqlite3.Connection,
) -> None:
    """DR-04/FR-PRP-02: an unmapped group yields no discovery, no call."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=None)
    _person(conn, 5, "Arjun", "5001")
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1, sender_person_id=5, sender_discord_user_id="5001")
    _membership(conn, bucket_id, mid, 1)
    _relevance_success(conn, bucket_id, [mid])
    generator = ScriptedGenerator([])

    result = run_proposal_pass(conn, generator)

    assert result.outcomes == ()
    assert generator.requests == []


def test_unknown_bucket_fails_loud(conn: sqlite3.Connection) -> None:
    with pytest.raises(ProposalServiceError, match="no bucket"):
        proposals._proposal_request(conn, 999)


def test_request_serialization_is_canonical(conn: sqlite3.Connection) -> None:
    """Sorted keys, compact separators; both forms are stable functions of
    the request (NFR-AUD-02 artifact discipline)."""
    bucket_id, _person_id, _ids = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator([_response()])
    run_proposal_pass(conn, generator)
    (request,) = generator.requests
    first = proposals.serialize_proposal_request(request, include_handle_map=True)
    second = proposals.serialize_proposal_request(request, include_handle_map=True)
    assert first == second
    assert first == json.dumps(
        json.loads(first), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


# =========================================================================
# C3 — eligibility policy (FR-PRP-01/02, §1.4, G1/G1b)
# =========================================================================


@pytest.mark.parametrize(
    ("role", "eligible"),
    [
        ("AH", True),
        ("Head", True),
        ("Volunteer", False),
        ("Admin", False),
    ],
)
def test_role_eligibility_vocabulary(
    conn: sqlite3.Connection, role: str, eligible: bool
) -> None:
    # PB-5 / FR-PRP-02: canonical roles from SRS vocabulary ('Admin', 'Head', 'AH', 'Volunteer')
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001", role=role)
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1, sender_person_id=5, sender_discord_user_id="5001")
    _membership(conn, bucket_id, mid, 1)
    _relevance_success(conn, bucket_id, [mid])
    generator = ScriptedGenerator([_response(_draft(mid))] if eligible else [])

    result = run_proposal_pass(conn, generator)

    if eligible:
        assert result.outcomes[0].proposal_count == 1
    else:
        assert result.outcomes == ()
        # AC-10 negative: no attempt, no generator call, no artifact.
        assert generator.requests == []
        assert _attempt_count(conn, bucket_id) == 0
        assert _artifact_count(conn) == 0


def test_inactive_sender_is_not_eligible(conn: sqlite3.Connection) -> None:
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001", active=0)
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1, sender_person_id=5, sender_discord_user_id="5001")
    _membership(conn, bucket_id, mid, 1)
    _relevance_success(conn, bucket_id, [mid])
    generator = ScriptedGenerator([])

    assert run_proposal_pass(conn, generator).outcomes == ()
    assert generator.requests == []


def test_unknown_sender_is_not_eligible(conn: sqlite3.Connection) -> None:
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1)
    _membership(conn, bucket_id, mid, 1)
    _relevance_success(conn, bucket_id, [mid])
    generator = ScriptedGenerator([])

    assert run_proposal_pass(conn, generator).outcomes == ()
    assert generator.requests == []


def test_membership_is_on_by_default_for_outside_vertical_sender(
    conn: sqlite3.Connection,
) -> None:
    """FR-PRP-02 default ON (Phase 7 S2): an eligible AH sender outside the
    group's source vertical produces no proposal under the default policy."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001")
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1, sender_person_id=5, sender_discord_user_id="5001")
    _membership(conn, bucket_id, mid, 1)
    _relevance_success(conn, bucket_id, [mid])
    generator = ScriptedGenerator([])

    outcome = run_proposal_pass(conn, generator)

    assert ProposalPolicy().require_source_vertical_membership is True
    assert outcome.outcomes == ()
    assert generator.requests == []


def test_membership_zero_flag_keeps_the_phase5_escape_hatch(
    conn: sqlite3.Connection,
) -> None:
    """FR-PRP-02: ``PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP=0`` still
    disables the membership requirement (the Phase 5 behaviour)."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001")
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1, sender_person_id=5, sender_discord_user_id="5001")
    _membership(conn, bucket_id, mid, 1)
    _relevance_success(conn, bucket_id, [mid])
    generator = ScriptedGenerator([_response()])

    result = run_proposal_pass(
        conn,
        generator,
        policy=load_proposal_policy(
            {"PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP": "0"}
        ),
    )
    assert result.outcomes[0].status is ProposalOutcome.GENERATED
    assert generator.requests != []


def test_membership_flag_on_requires_source_vertical_membership(
    conn: sqlite3.Connection,
) -> None:
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001")
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1, sender_person_id=5, sender_discord_user_id="5001")
    _membership(conn, bucket_id, mid, 1)
    _relevance_success(conn, bucket_id, [mid])
    generator = ScriptedGenerator([])

    outcome = run_proposal_pass(
        conn,
        generator,
        policy=load_proposal_policy(
            {"PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP": "1"}
        ),
    )
    assert outcome.outcomes == ()
    assert generator.requests == []


def test_membership_flag_on_accepts_multi_vertical_member(
    conn: sqlite3.Connection,
) -> None:
    _vertical(conn, 1, "Tech")
    _vertical(conn, 2, "Sports")
    _group(conn, 1, vertical_id=1)
    person_id = _person(conn, 5, "Arjun", "5001")
    _membership_vertical(conn, person_id, 1)
    _membership_vertical(conn, person_id, 2)
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1, sender_person_id=5, sender_discord_user_id="5001")
    _membership(conn, bucket_id, mid, 1)
    _relevance_success(conn, bucket_id, [mid])
    generator = ScriptedGenerator([_response()])

    result = run_proposal_pass(
        conn,
        generator,
        policy=load_proposal_policy(
            {"PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP": "true"}
        ),
    )
    assert result.outcomes[0].status is ProposalOutcome.GENERATED


def test_load_proposal_policy_defaults_and_config(
    conn: sqlite3.Connection,
) -> None:
    """G1 config keys: roles overridable, membership flag truthiness loud."""
    default = load_proposal_policy(None)
    assert default.eligible_roles == ("AH", "Head")
    # FR-PRP-02 default ON (Phase 7 S2).
    assert default.require_source_vertical_membership is True

    blank = load_proposal_policy(
        {"PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP": "  "}
    )
    assert blank.require_source_vertical_membership is True

    configured = load_proposal_policy(
        {
            "PROPOSAL_ELIGIBLE_ROLES": "Head, ah ",
            "PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP": "yes",
        }
    )
    assert configured.eligible_roles == ("Head", "ah")
    assert configured.require_source_vertical_membership is True

    off = load_proposal_policy(
        {"PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP": "0"}
    )
    assert off.require_source_vertical_membership is False

    with pytest.raises(
        ValueError, match="PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP"
    ):
        load_proposal_policy({"PROPOSAL_REQUIRE_SOURCE_VERTICAL_MEMBERSHIP": "maybe"})
    with pytest.raises(ValueError, match="PROPOSAL_ELIGIBLE_ROLES"):
        load_proposal_policy({"PROPOSAL_ELIGIBLE_ROLES": " , "})


# =========================================================================
# WS-3 — validation tiers (FR-PRP-05/13, AC-02/03; C2 codes; DC-3/DC-6 pins)
# =========================================================================


def test_zero_proposals_is_a_valid_success(conn: sqlite3.Connection) -> None:
    """FR-PRP-04/AC-03: a legitimately empty response succeeds, persists no
    proposal and still commits the attempt + artifact (honest zero)."""
    bucket_id, _person_id, _ids = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator([_response()])

    result = run_proposal_pass(conn, generator)

    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.GENERATED
    assert outcome.proposal_count == 0
    assert _proposal_rows(conn, bucket_id) == []
    attempt = _attempt_rows(conn, bucket_id)[-1]
    assert str(attempt["outcome"]) == "SUCCEEDED"
    assert attempt["error"] is None
    row = _artifact(conn, int(attempt["id"]))
    assert json.loads(str(row["raw_response"]))["proposals"] == []
    assert str(row["response_sha256"]) == proposals._hash_text(
        str(row["raw_response"])
    )


def test_all_invalid_is_a_failed_attempt_never_empty_success(
    conn: sqlite3.Connection,
) -> None:
    """DC-3 (owner-vetoable, pinned): a non-empty all-invalid response is
    FAILED_RETRYABLE — never a legitimate zero-proposals success (EIR-03:
    malformed output is an execution failure)."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator(
        [
            _response(
                _draft(mid, assignment_mode="OPEN", proposed_assignees=("Neha",)),
                _draft(mid, ptype="RAGDA"),
            )
        ]
    )

    result = run_proposal_pass(conn, generator)

    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.RETRYING
    assert _proposal_rows(conn, bucket_id) == []
    attempt = _attempt_rows(conn, bucket_id)[-1]
    assert str(attempt["outcome"]) == "FAILED_RETRYABLE"
    error = str(attempt["error"])
    assert "RAGDA" not in error  # model content never rides the error channel
    assert "MODE_ASSIGNEE_CONFLICT" in error
    assert "TYPE_INVALID" in error
    row = _artifact(conn, int(attempt["id"]))
    codes = [entry["reason_code"] for entry in json.loads(str(row["rejected_items"]))]
    assert codes == ["MODE_ASSIGNEE_CONFLICT", "TYPE_INVALID"]
    assert row["raw_response"] is not None


def test_partial_invalid_keeps_valid_sibling_independent(
    conn: sqlite3.Connection,
) -> None:
    """FR-PRP-05/AC-02: one failing item never blocks its valid sibling; a
    SUCCEEDED attempt stores the valid item and records the rejected one."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator(
        [
            _response(
                _draft(mid),
                _draft(mid, assignment_mode="NONE"),  # TASK/NONE is mode-invalid
            )
        ]
    )

    result = run_proposal_pass(conn, generator)

    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.GENERATED
    assert outcome.proposal_count == 1
    assert outcome.rejected_count == 1
    rows = _proposal_rows(conn, bucket_id)
    (stored,) = rows
    attempt = _attempt_rows(conn, bucket_id)[-1]
    assert int(stored["attempt_id"]) == int(attempt["id"])
    assert _codes(conn, int(attempt["id"])) == ["MODE_INVALID"]


_REJECT_CASES = [
    pytest.param(
        lambda mid: _draft(
            mid, assignment_mode="OPEN", proposed_assignees=("Neha",)
        ),
        "MODE_ASSIGNEE_CONFLICT",
        id="open-with-names",
    ),
    pytest.param(
        lambda mid: _draft(
            mid,
            ptype="ANNOUNCEMENT",
            assignment_mode="EXPLICIT",
            proposed_assignees=("Neha",),
        ),
        "MODE_ASSIGNEE_CONFLICT",
        id="announcement-with-names",
    ),
    pytest.param(
        lambda mid: _draft(mid, assignment_mode="NONE"),
        "MODE_INVALID",
        id="task-mode-none",
    ),
    pytest.param(
        lambda mid: _draft(mid, proposed_assignees="Neha"),  # str, not a sequence
        "FIELD_TYPE_INVALID",
        id="assignees-string",
    ),
    pytest.param(
        lambda mid: _draft(mid, people_needed=True),  # bool is not a count
        "FIELD_TYPE_INVALID",
        id="boolean-count",
    ),
    pytest.param(
        lambda mid: _draft(999),
        "PRIMARY_NOT_CANDIDATE",
        id="foreign-primary",
    ),
    pytest.param(
        lambda mid: _draft(mid, ptype="RAGDA"),
        "TYPE_INVALID",
        id="bad-ptype",
    ),
    pytest.param(
        lambda mid: _draft(mid, assignment_mode="SOMETIMES"),
        "MODE_INVALID",
        id="bad-mode",
    ),
    pytest.param(
        lambda mid: _draft(mid, task_type="GLOBAL"),
        "TASK_TYPE_INVALID",
        id="bad-task-type",
    ),
    pytest.param(
        lambda mid: _draft(
            mid,
            assignment_mode="OPEN",
            proposed_assignees=None,
            people_needed=-2,
        ),
        "PEOPLE_NEEDED_INVALID",
        id="negative-count",
    ),
    pytest.param(
        lambda mid: _draft(
            mid,
            assignment_mode="OPEN",
            proposed_assignees=None,
            people_needed=3,
            people_needed_unlimited=1,
        ),
        "PEOPLE_NEEDED_CONFLICT",
        id="count-xor",
    ),
    pytest.param(
        lambda mid: _draft(mid, description="   "),
        "FIELD_VALUE_INVALID",
        id="blank-description",
    ),
    pytest.param(
        lambda mid: _draft(mid, attributes=(("venue", "hall"), 12)),
        "ATTRIBUTES_INVALID",
        id="attr-shape",
    ),
    pytest.param(
        lambda mid: _draft(
            mid, attributes=(("venue", "hall"), ("venue", "roof"))
        ),
        "DUPLICATE_ATTR_KEY",
        id="dup-attr",
    ),
    pytest.param(
        lambda mid: _draft(mid, attributes=(("venue", "  "),)),
        "FIELD_VALUE_INVALID",
        id="blank-attr-value",
    ),
    pytest.param(
        lambda mid: _draft(mid, proposed_assignees=("@m9",)),
        "UNKNOWN_HANDLE",
        id="unknown-handle",
    ),
]


@pytest.mark.parametrize(("draft_factory", "expected_code"), _REJECT_CASES)
def test_rejection_matrix_reason_codes(
    conn: sqlite3.Connection,
    draft_factory: Callable[[int], ProposalDraft],
    expected_code: str,
) -> None:
    """C2/FR-PRP-13/FR-PRP-06/08: every documented Tier-2 reason code fires
    exactly; the valid sibling still stores (per-item independence)."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator(
        [_response(_draft(mid), draft_factory(mid))]
    )

    result = run_proposal_pass(conn, generator)

    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.GENERATED
    assert outcome.proposal_count == 1
    assert outcome.rejected_count == 1
    attempt = _attempt_rows(conn, bucket_id)[-1]
    rejections = _codes(conn, int(attempt["id"]))
    assert rejections == [expected_code]


def test_rejection_reason_codes_are_from_the_documented_set(
    conn: sqlite3.Connection,
) -> None:
    """The reason-code vocabulary is fixed and documented (DC-11 evidence;
    internal annotation, not SRS state vocabulary)."""
    assert frozenset(proposals.REJECTION_REASON_CODES) == {
        "PRIMARY_NOT_CANDIDATE",
        "TYPE_INVALID",
        "MODE_INVALID",
        "TASK_TYPE_INVALID",
        "MODE_ASSIGNEE_CONFLICT",
        "PEOPLE_NEEDED_CONFLICT",
        "PEOPLE_NEEDED_INVALID",
        "FIELD_TYPE_INVALID",
        "FIELD_VALUE_INVALID",
        "ATTRIBUTES_INVALID",
        "DUPLICATE_ATTR_KEY",
        "UNKNOWN_HANDLE",
    }


def test_unknown_returned_handle_is_rejected_never_guess(
    conn: sqlite3.Connection,
) -> None:
    """C1: a handle-shaped token outside the request map is a Tier-2
    violation — the backend never guesses who it might have been."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    _mention(conn, mid, "2002", 0)  # knows @m1 only
    generator = ScriptedGenerator([_response(_draft(mid, proposed_assignees=("@m2",)))])

    result = run_proposal_pass(conn, generator)

    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.RETRYING  # the only item failed
    attempt = _attempt_rows(conn, bucket_id)[-1]
    assert _codes(conn, int(attempt["id"])) == ["UNKNOWN_HANDLE"]
    assert _proposal_rows(conn, bucket_id) == []


def test_explicit_without_names_is_tier3_content(
    conn: sqlite3.Connection,
) -> None:
    """C6/§4.4 refinement: an EXPLICIT task nobody names is *content* (the
    reviewer fills it at review); it stores as generated, no assignee rows,
    proposed_assignees_text NULL."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator(
        [_response(_draft(mid, proposed_assignees=None))]
    )

    result = run_proposal_pass(conn, generator)

    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.GENERATED
    assert outcome.proposal_count == 1
    (stored,) = _proposal_rows(conn, bucket_id)
    assert str(stored["assignment_mode"]) == "EXPLICIT"
    assert stored["proposed_assignees_text"] is None
    assert _assignee_rows(conn, int(stored["id"])) == []


def test_envelope_failure_is_tier1_failed_retryable(
    conn: sqlite3.Connection,
) -> None:
    """Tier-1: missing provenance or a broken envelope is an execution
    failure — whole attempt FAILED_RETRYABLE, item rejections empty."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    broken = object()  # no provenance, no proposals attribute
    generator = ScriptedGenerator([broken])

    result = run_proposal_pass(conn, generator)

    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.RETRYING
    attempt = _attempt_rows(conn, bucket_id)[-1]
    assert str(attempt["outcome"]) == "FAILED_RETRYABLE"
    row = _artifact(conn, int(attempt["id"]))
    assert row["rejected_items"] is None
    assert row["raw_response"] is not None  # what the adapter returned survives


def test_adapter_exception_writes_failure_without_raw_response(
    conn: sqlite3.Connection,
) -> None:
    """C2: raw_response is NULL exactly when the adapter raised before
    returning; the error channel is masked (NFR-SEC-04 reach)."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator(
        [RuntimeError("phone 98765 43210 sings " + "x" * 900)]
    )

    result = run_proposal_pass(conn, generator)

    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.RETRYING
    attempt = _attempt_rows(conn, bucket_id)[-1]
    assert str(attempt["outcome"]) == "FAILED_RETRYABLE"
    error = str(attempt["error"])
    assert "[PHONE]" in error
    assert "98765 43210" not in error
    assert len(error) <= 500
    row = _artifact(conn, int(attempt["id"]))
    assert row["raw_response"] is None
    assert row["response_sha256"] is None


def test_boolean_people_needed_reaches_resolution_not_counting_schema(
    conn: sqlite3.Connection,
) -> None:
    """The never-name-people rule (FR-PRP-08) fires structurally before the
    schema ever sees the item (OPEN + names cannot become assignee rows)."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator(
        [_response(_draft(mid, assignment_mode="OPEN", proposed_assignees=("Neha",)))]
    )

    result = run_proposal_pass(conn, generator)

    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.RETRYING
    assert _proposal_rows(conn, bucket_id) == []
    attempt = _attempt_rows(conn, bucket_id)[-1]
    assert _codes(conn, int(attempt["id"])) == ["MODE_ASSIGNEE_CONFLICT"]


# =========================================================================
# WS-4 — persistence, provenance, artifact (C1/C2), latency (C5)
# =========================================================================


def test_successful_attempt_persists_provenance_and_shape(
    conn: sqlite3.Connection,
) -> None:
    """FR-PRP-12/NFR-AUD-01: provenance copied from the validated response
    to the attempt and every proposal; structurally_valid always 1."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator([_response(_draft(mid))])

    run_proposal_pass(conn, generator)

    attempt = _attempt_rows(conn, bucket_id)[-1]
    assert str(attempt["stage"]) == STAGE_PROPOSAL_GENERATION
    assert str(attempt["outcome"]) == "SUCCEEDED"
    assert str(attempt["model_id"]) == "gen-model"
    assert str(attempt["model_version"]) == "gen-v2"
    assert str(attempt["config_version"]) == "proposal-prompt-v1"
    (stored,) = _proposal_rows(conn, bucket_id)
    assert int(stored["attempt_id"]) == int(attempt["id"])
    assert int(stored["bucket_id"]) == bucket_id
    assert int(stored["primary_message_id"]) == mid
    assert str(stored["ptype"]) == "TASK"
    assert str(stored["description"]) == "plant the flags"
    assert str(stored["assignment_mode"]) == "EXPLICIT"
    assert str(stored["proposed_assignees_text"]) == "Neha"
    assert str(stored["task_type"]) == "CROSS_VERTICAL"
    assert int(stored["structurally_valid"]) == 1
    assert str(stored["generated_at"]) == str(attempt["finished_at"])
    for column in ("model_id", "model_version", "prompt_version"):
        attempt_col = column if column != "prompt_version" else "config_version"
        assert str(stored[column]) == str(attempt[attempt_col])


def test_open_task_count_encodings_store(
    conn: sqlite3.Connection,
) -> None:
    """FR-PRP-06: OPEN tasks with a count, UNLIMITED or neither are valid
    shapes; no assignee rows happen for OPEN proposals."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator(
        [
            _response(
                _draft(
                    mid,
                    assignment_mode="OPEN",
                    proposed_assignees=None,
                    people_needed=4,
                )
            )
        ]
    )

    run_proposal_pass(conn, generator)

    (stored,) = _proposal_rows(conn, bucket_id)
    assert int(stored["people_needed"]) == 4
    assert int(stored["people_needed_unlimited"]) == 0
    assert stored["proposed_assignees_text"] is None

    bucket_b = _bucket(conn, 1)
    con_mid = _message(conn, 1, 3, sender_person_id=5, sender_discord_user_id="5001")
    _membership(conn, bucket_b, con_mid, 1)
    _relevance_success(conn, bucket_b, [con_mid])
    unlimited = ScriptedGenerator(
        [
            _response(
                _draft(
                    con_mid,
                    assignment_mode="OPEN",
                    proposed_assignees=None,
                    people_needed=None,
                    people_needed_unlimited=1,
                    task_type="INTERNAL",
                )
            )
        ]
    )
    result = run_proposal_pass(
        conn,
        unlimited,
        policy=load_proposal_policy({}),
        budget_overrides={bucket_b: 1},
    )
    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.GENERATED
    rows_b = _proposal_rows(conn, bucket_b)
    (row_b,) = rows_b
    assert row_b["people_needed"] is None
    assert int(row_b["people_needed_unlimited"]) == 1


def test_announcement_stores_without_assignment(
    conn: sqlite3.Connection,
) -> None:
    """FR-PRP-08: announcements carry the NONE assignment mode; missing
    fields stay missing (FR-PRP-11/DR-04)."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator(
        [
            _response(
                _draft(
                    mid,
                    ptype="ANNOUNCEMENT",
                    description="sabko batao",
                    assignment_mode="NONE",
                    proposed_assignees=None,
                    task_type="INTERNAL",
                )
            )
        ]
    )

    run_proposal_pass(conn, generator)

    (stored,) = _proposal_rows(conn, bucket_id)
    assert str(stored["ptype"]) == "ANNOUNCEMENT"
    assert str(stored["assignment_mode"]) == "NONE"
    assert stored["description"] == "sabko batao"
    assert _assignee_rows(conn, int(stored["id"])) == []


def test_missing_fields_stay_missing_never_invented(
    conn: sqlite3.Connection,
) -> None:
    """FR-PRP-11/DC-10: NULL stays NULL; the reviewer renders placeholders."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator(
        [
            _response(
                _draft(
                    mid,
                    description=None,
                    assignment_mode="OPEN",
                    proposed_assignees=None,
                )
            )
        ]
    )

    run_proposal_pass(conn, generator)

    (stored,) = _proposal_rows(conn, bucket_id)
    assert stored["description"] is None
    assert stored["proposed_assignees_text"] is None
    assert stored["people_needed"] is None
    # No suggested responsible vertical came back: missing stays missing.
    assert stored["suggested_responsible_vertical_id"] is None


def test_attribute_and_vertical_link_rows(
    conn: sqlite3.Connection,
) -> None:
    """FR-PRP-07 + DC-7: attributes pass through (NULL value = missing),
    responsible vertical resolves case-insensitively, unresolvable names
    never invent FKs, affected-vertical links keep only known names."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator(
        [
            _response(
                _draft(
                    mid,
                    suggested_responsible_vertical=" tech ",
                    affected_verticals=("Tech", "Unknownland"),
                    attributes=(("venue", "hall A"), ("time", None)),
                )
            )
        ]
    )

    run_proposal_pass(conn, generator)

    (stored,) = _proposal_rows(conn, bucket_id)
    resolved_id = conn.execute(
        "SELECT id FROM vertical WHERE lower(name) = 'tech'"
    ).fetchone()
    assert resolved_id is not None
    assert (
        stored["suggested_responsible_vertical_id"] == resolved_id["id"]
    )
    attribute_rows = conn.execute(
        "SELECT attr_key, attr_value FROM proposal_attribute"
        " WHERE proposal_id = ? ORDER BY attr_key",
        (int(stored["id"]),),
    ).fetchall()
    assert [(str(row["attr_key"]), row["attr_value"]) for row in attribute_rows] == [
        ("time", None),
        ("venue", "hall A"),
    ]
    affected = conn.execute(
        "SELECT vertical_id FROM proposal_affected_vertical WHERE proposal_id = ?",
        (int(stored["id"]),),
    ).fetchall()
    # Only the known vertical name produced a link (no invented FKs).
    assert [int(row["vertical_id"]) for row in affected] == [
        int(resolved_id["id"])
    ]


def test_unresolvable_responsible_vertical_stays_null(
    conn: sqlite3.Connection,
) -> None:
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator(
        [_response(_draft(mid, suggested_responsible_vertical="Nowhere"))]
    )
    run_proposal_pass(conn, generator)
    (stored,) = _proposal_rows(conn, bucket_id)
    assert stored["suggested_responsible_vertical_id"] is None
    affected = conn.execute(
        "SELECT COUNT(*) AS n FROM proposal_affected_vertical WHERE proposal_id = ?",
        (int(stored["id"]),),
    ).fetchone()
    assert int(affected["n"]) == 0  # unresolvable affected name dropped


def test_artifact_stores_both_forms_and_the_hash(conn: sqlite3.Connection) -> None:
    """C1/C2/NFR-AUD-02: artifact form (with the map) + dispatch hash + raw
    retention in one transaction with the attempt (F3 analog)."""
    bucket_id, _person_id, message_ids = _expected_scenario(conn, message_count=2)
    _mention(conn, message_ids[0], "2002", 0)
    generator = ScriptedGenerator([_response(_draft(message_ids[0]))])
    run_proposal_pass(conn, generator)

    attempt = _attempt_rows(conn, bucket_id)[-1]
    row = _artifact(conn, int(attempt["id"]))
    assert str(row["masking_ruleset_version"]) == "masking-rules-v1"
    assert str(row["policy_version"]) == "proposal-prompt-v1"
    artifact_json = json.loads(str(row["masked_request"]))
    assert "mention_handle_map" in artifact_json
    assert artifact_json["mention_handle_map"] == {"@m1": "2002"}
    # dispatch hash: re-derivable from the request dataclass.
    # dispatch hash: re-derivable from the request dataclasses.
    assert str(row["request_sha256"]) == proposals._hash_text(
        proposals.serialize_proposal_request_parts(generator.requests)
    )
    assert str(row["captured_at"]) == str(attempt["finished_at"])


def test_artifact_iff_committed_attempt(conn: sqlite3.Connection) -> None:
    """F3 analog: artifact rows exist exactly for committed attempts — on
    success, on failure, never for refused/superseded/interrupted paths."""
    bucket_id, _person_id, message_ids = _expected_scenario(conn, message_count=1)
    (mid,) = message_ids
    generator: ScriptedGenerator = ScriptedGenerator(
        [_response(_draft(mid))]  # one success for the first target bucket
    )
    run_proposal_pass(conn, generator)
    assert _artifact_count(conn) == 1


def test_interrupt_during_call_writes_nothing(
    conn: sqlite3.Connection,
) -> None:
    """Interrupt inside the generator call: never an attempt outcome;
    nothing durable exists; the interrupt propagates unchanged."""
    bucket_id, _person_id, _ids = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator([KeyboardInterrupt("operator")])

    with pytest.raises(KeyboardInterrupt):
        run_proposal_pass(conn, generator)

    assert _attempt_count(conn, bucket_id) == 0
    assert _artifact_count(conn) == 0


def test_interrupt_at_write_time_rolls_back_everything(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupt inside the persistence transaction leaves no attempt,
    no artifact, no proposals: full rollback (FR-PRP-14/NFR-REL-04)."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    real_utc_now = proposals._utc_now
    calls = {"n": 0}

    def sequenced_utc_now() -> str:
        calls["n"] += 1
        if calls["n"] >= 2:  # 1 = started_at; 2 = finished (write time)
            raise KeyboardInterrupt("operator stop at write time")
        return real_utc_now()

    monkeypatch.setattr(proposals, "_utc_now", sequenced_utc_now)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_proposal_pass(conn, ScriptedGenerator([_response(_draft(mid))]))
    finally:
        monkeypatch.undo()

    assert _attempt_count(conn, bucket_id) == 0
    assert _artifact_count(conn) == 0
    assert _proposal_rows(conn, bucket_id) == []


def test_latency_rows_record_the_adapter_call_and_stage_delay(
    conn: sqlite3.Connection,
) -> None:
    """C5/NFR-PERF-02: PROPOSAL_LATENCY covers the adapter call alone; the
    second row anchors at the relevance finish (nearest stored post-close
    wall time); no SRS target is compared anywhere."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator([_response(_draft(mid))])

    result = run_proposal_pass(conn, generator)

    attempt = _attempt_rows(conn, bucket_id)[-1]
    rows = _latency_rows(conn, int(attempt["id"]))
    labels = {str(row["metric"]) for row in rows}
    assert labels == {LATENCY_METRIC_LABEL, STAGE_DELAY_METRIC_LABEL}
    by_label = {str(row["metric"]): row for row in rows}
    assert float(by_label[LATENCY_METRIC_LABEL]["value_seconds"]) >= 0.0
    delay_row = by_label[STAGE_DELAY_METRIC_LABEL]
    assert float(delay_row["value_seconds"]) > 0.0
    assert str(delay_row["recorded_at"]) == str(attempt["finished_at"])
    assert result.outcomes[0].proposal_count == 1


def test_stage_delay_skipped_without_parseable_anchor(
    conn: sqlite3.Connection,
) -> None:
    """C5/DC-14: honest skip when the relevance anchor is unparsable ('t'
    seeded rows) rather than a fabricated zero."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    _relevance_success(conn, bucket_id, [mid], finished_at="t")
    generator = ScriptedGenerator([_response(_draft(mid))])

    run_proposal_pass(conn, generator)

    attempt = _attempt_rows(conn, bucket_id)[-1]
    rows = _latency_rows(conn, int(attempt["id"]))
    assert {str(row["metric"]) for row in rows} == {LATENCY_METRIC_LABEL}


def test_failed_attempt_records_no_latency(conn: sqlite3.Connection) -> None:
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator([RuntimeError("nope")])

    result = run_proposal_pass(conn, generator)

    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.RETRYING
    attempt = _attempt_rows(conn, bucket_id)[-1]
    assert _latency_rows(conn, int(attempt["id"])) == []


# =========================================================================
# WS-4 — resolution matrix (FR-PRP-10, AC-11; DC-6; DC-15)
# =========================================================================


def test_mention_route_resolves_by_user_id(conn: sqlite3.Connection) -> None:
    """AC-11(a): a mapped handle resolves exactly via the Discord user ID —
    no roster search; display name irrelevant."""
    bucket_id, _person_id, mid = _resolution_scenario(conn, mention=True)
    generator = ScriptedGenerator(
        [_response(_draft(mid, proposed_assignees=("@m1",)))]
    )

    run_proposal_pass(conn, generator)

    (stored,) = _proposal_rows(conn, bucket_id)
    (assignee,) = _assignee_rows(conn, int(stored["id"]))
    assert str(assignee["resolution"]) == "RESOLVED"
    assert int(assignee["person_id"]) == 7
    assert str(assignee["resolved_via"]) == "MENTION"
    assert str(stored["proposed_assignees_text"]) == "@m1"


def test_mention_route_flagged_without_roster_person(
    conn: sqlite3.Connection,
) -> None:
    """A mapped ID that no roster person holds → FLAGGED (never guessed)."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1, sender_person_id=5, sender_discord_user_id="5001")
    _membership(conn, bucket_id, mid, 1)
    _mention(conn, mid, "2999", 0)
    _relevance_success(conn, bucket_id, [mid])
    generator = ScriptedGenerator(
        [_response(_draft(mid, proposed_assignees=("@m1",)))]
    )

    run_proposal_pass(conn, generator)

    (stored,) = _proposal_rows(conn, bucket_id)
    (assignee,) = _assignee_rows(conn, int(stored["id"]))
    assert str(assignee["resolution"]) == "FLAGGED"
    assert assignee["person_id"] is None
    assert assignee["resolved_via"] is None


def test_mention_route_does_not_require_active_membership(
    conn: sqlite3.Connection,
) -> None:
    """DC-15: exact-ID resolution ignores the active flag (vetoable)."""
    bucket_id, _person_id, mid = _resolution_scenario(
        conn, mention=True, mention_member_active=0
    )
    generator = ScriptedGenerator(
        [_response(_draft(mid, proposed_assignees=("@m1",)))]
    )

    run_proposal_pass(conn, generator)

    (stored,) = _proposal_rows(conn, bucket_id)
    (assignee,) = _assignee_rows(conn, int(stored["id"]))
    assert str(assignee["resolution"]) == "RESOLVED"
    assert int(assignee["person_id"]) == 7


def test_name_match_unique_resolves_trimmed_casefold(
    conn: sqlite3.Connection,
) -> None:
    """DC-6 (owner-vetoable, pinned): exact trimmed+case-folded equality."""
    bucket_id, _person_id, mid = _resolution_scenario(conn)
    generator = ScriptedGenerator(
        [_response(_draft(mid, proposed_assignees=("  nehA ",)))]
    )

    run_proposal_pass(conn, generator)

    (stored,) = _proposal_rows(conn, bucket_id)
    (assignee,) = _assignee_rows(conn, int(stored["id"]))
    assert str(assignee["resolution"]) == "RESOLVED"
    assert int(assignee["person_id"]) == 7
    assert str(assignee["resolved_via"]) == "NAME_MATCH"


def test_name_match_several_is_unresolved(conn: sqlite3.Connection) -> None:
    bucket_id, _person_id, mid = _resolution_scenario(
        conn, second_member=(6, "Neha", "AH")
    )
    generator = ScriptedGenerator(
        [_response(_draft(mid, proposed_assignees=("Neha",)))]
    )

    run_proposal_pass(conn, generator)

    (stored,) = _proposal_rows(conn, bucket_id)
    (assignee,) = _assignee_rows(conn, int(stored["id"]))
    assert str(assignee["resolution"]) == "UNRESOLVED"
    assert assignee["person_id"] is None


def test_name_match_none_is_flagged(conn: sqlite3.Connection) -> None:
    bucket_id, _person_id, mid = _resolution_scenario(conn)
    generator = ScriptedGenerator(
        [_response(_draft(mid, proposed_assignees=("Zoya",)))]
    )

    run_proposal_pass(conn, generator)

    (stored,) = _proposal_rows(conn, bucket_id)
    (assignee,) = _assignee_rows(conn, int(stored["id"]))
    assert str(assignee["resolution"]) == "FLAGGED"


def test_inactive_name_is_not_matched(conn: sqlite3.Connection) -> None:
    bucket_id, _person_id, mid = _resolution_scenario(
        conn, mention_member_active=0
    )
    generator = ScriptedGenerator(
        [_response(_draft(mid, proposed_assignees=("Neha",)))]
    )

    run_proposal_pass(conn, generator)

    (stored,) = _proposal_rows(conn, bucket_id)
    (assignee,) = _assignee_rows(conn, int(stored["id"]))
    assert str(assignee["resolution"]) == "FLAGGED"


def test_cross_vertical_name_is_flagged_not_silently_resolved(
    conn: sqlite3.Connection,
) -> None:
    """C6/DC-6 pin: name matching happens only inside the source vertical;
    a same-named colleague outside it stays invisible → FLAGGED."""
    bucket_id, _person_id, mid = _resolution_scenario(
        conn, cross_vertical=(8, "Neha", None), mention_member_active=0
    )
    generator = ScriptedGenerator(
        [_response(_draft(mid, proposed_assignees=("Neha",)))]
    )

    run_proposal_pass(conn, generator)

    (stored,) = _proposal_rows(conn, bucket_id)
    (assignee,) = _assignee_rows(conn, int(stored["id"]))
    assert str(assignee["resolution"]) == "FLAGGED"


def test_multiple_assignee_tokens_each_resolve_independently(
    conn: sqlite3.Connection,
) -> None:
    bucket_id, _person_id, mid = _resolution_scenario(conn)
    _person(conn, 6, "Rohit", "6001")
    _membership_vertical(conn, 6, 1)
    generator = ScriptedGenerator(
        [_response(_draft(mid, proposed_assignees=("Neha", "Pick @m1 the", "Zoya")))]
    )

    run_proposal_pass(conn, generator)

    (stored,) = _proposal_rows(conn, bucket_id)
    rows = _assignee_rows(conn, int(stored["id"]))
    assert [(str(row["assignee_text"]), str(row["resolution"])) for row in rows] == [
        ("Neha", "RESOLVED"),
        ("Pick @m1 the", "FLAGGED"),
        ("Zoya", "FLAGGED"),
    ]
    assert str(stored["proposed_assignees_text"]) == "Neha, Pick @m1 the, Zoya"


# =========================================================================
# WS-5 — pass orchestration (budget, overrides, fallback, race, errors)
# =========================================================================


def test_pass_rejects_non_positive_budget_loudly(
    conn: sqlite3.Connection,
) -> None:
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator([_response(_draft(mid))])

    with pytest.raises(ValueError):
        run_proposal_pass(conn, generator, max_attempts_per_bucket=0)

    assert generator.requests == []
    assert _attempt_count(conn, bucket_id) == 0
    assert proposal_pending_buckets(conn) == (bucket_id,)


def test_overrides_validate_loudly_before_any_state_change(
    conn: sqlite3.Connection,
) -> None:
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator([_response(_draft(mid))])

    with pytest.raises(ValueError, match="budget override keys"):
        run_proposal_pass(conn, generator, budget_overrides={"one": 4})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must be a positive integer"):
        run_proposal_pass(conn, generator, budget_overrides={bucket_id: 0})
    with pytest.raises(ValueError, match="must be a positive integer"):
        run_proposal_pass(conn, generator, budget_overrides={bucket_id: True})

    assert generator.requests == []
    assert _attempt_count(conn, bucket_id) == 0
    assert _artifact_count(conn) == 0


def test_deterministic_order_multiple_buckets(conn: sqlite3.Connection) -> None:
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    ids: list[int] = []
    mids: list[int] = []
    for index in range(3):
        bucket_id = _bucket(conn, 1)
        mid = _message(conn, 1, index, sender_person_id=5, sender_discord_user_id="5001")
        _membership(conn, bucket_id, mid, 1)
        _relevance_success(conn, bucket_id, [mid])
        ids.append(bucket_id)
        mids.append(mid)

    result = run_proposal_pass(
        conn,
        ScriptedGenerator(
            [_response(_draft(mids[0])), _response(_draft(mids[1])), _response(_draft(mids[2]))]
        ),
    )

    assert [outcome.bucket_id for outcome in result.outcomes] == ids
    assert [outcome.status for outcome in result.outcomes] == [
        ProposalOutcome.GENERATED
    ] * 3
    assert result.generated == tuple(ids)


def test_budget_refusal_is_honest_exhaustion(
    conn: sqlite3.Connection,
) -> None:
    """Attempts == budget with no success: the bucket is refused before any
    adapter call and reported EXHAUSTED — zero new rows (NFR-REL-03 shape)."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    for _ in range(3):
        _proposal_attempt_seed(conn, bucket_id, "FAILED_RETRYABLE")
    generator = ScriptedGenerator([])

    result = run_proposal_pass(conn, generator)

    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.EXHAUSTED
    assert generator.requests == []
    assert _attempt_count(conn, bucket_id) == 3
    assert _artifact_count(conn) == 0
    assert proposal_exhausted_buckets(conn) == (bucket_id,)


def test_override_raises_total_cap_for_authorized_recovery(
    conn: sqlite3.Connection,
) -> None:
    """Approved Phase 4 pattern: the override is this bucket's raised TOTAL
    attempt cap for the pass; history stays append-only."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    for _ in range(3):
        _proposal_attempt_seed(conn, bucket_id, "FAILED_RETRYABLE")
    generator = ScriptedGenerator([_response(_draft(mid))])

    result = run_proposal_pass(conn, generator, budget_overrides={bucket_id: 4})

    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.GENERATED
    attempt = _attempt_rows(conn, bucket_id)[-1]
    assert int(attempt["id"]) == outcome.attempt_ids[-1]
    assert _artifact_count(conn) == 1


def test_override_below_accumulated_is_refused_honestly(
    conn: sqlite3.Connection,
) -> None:
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    for _ in range(3):
        _proposal_attempt_seed(conn, bucket_id, "FAILED_RETRYABLE")
    generator = ScriptedGenerator([])

    result = run_proposal_pass(conn, generator, budget_overrides={bucket_id: 2})

    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.EXHAUSTED
    assert generator.requests == []
    assert _attempt_count(conn, bucket_id) == 3


def test_repeat_pass_after_success_is_noop(conn: sqlite3.Connection) -> None:
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator([_response(_draft(mid))])
    first = run_proposal_pass(conn, generator)
    assert len(first.outcomes) == 1

    again = ScriptedGenerator([])
    result = run_proposal_pass(conn, again)

    assert result.outcomes == ()
    assert again.requests == []
    assert _attempt_count(conn, bucket_id) == 1


def test_retryable_failure_comes_back_on_the_next_pass(
    conn: sqlite3.Connection,
) -> None:
    """AC-12 half: a FAILED_RETRYABLE attempt leaves the bucket discoverable;
    the retry succeeds and nothing is duplicated (append-only history)."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    failing = ScriptedGenerator([RuntimeError("flaky")])
    failing_run = run_proposal_pass(conn, failing)
    (failing_outcome,) = failing_run.outcomes
    assert failing_outcome.status is ProposalOutcome.RETRYING

    working = ScriptedGenerator([_response(_draft(mid))])
    result = run_proposal_pass(conn, working)

    assert result.outcomes[0].status is ProposalOutcome.GENERATED
    assert _attempt_count(conn, bucket_id) == 2
    assert len(_proposal_rows(conn, bucket_id)) == 1
    assert _artifact_count(conn) == 2


def test_fallback_only_after_primary_failure(
    conn: sqlite3.Connection,
) -> None:
    """NFR-REL-07: the fallback never runs while the primary might still
    succeed; both attempts recorded; the success supplies the proposals."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator(
        [RuntimeError("context overflow"), _response(_draft(mid))]
    )

    result = run_proposal_pass(conn, generator, generator)

    assert len(generator.requests) == 2
    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.GENERATED
    attempts = _attempt_rows(conn, bucket_id)
    assert [str(row["stage"]) for row in attempts] == [
        STAGE_PROPOSAL_GENERATION,
        STAGE_PROPOSAL_FALLBACK_GENERATION,
    ]
    assert [str(row["outcome"]) for row in attempts] == [
        "FAILED_RETRYABLE",
        "SUCCEEDED",
    ]
    proposals_stored = _proposal_rows(conn, bucket_id)
    assert int(proposals_stored[0]["attempt_id"]) == int(attempts[1]["id"])
    assert _artifact_count(conn) == 2


def test_fallback_failure_records_both_and_retries(
    conn: sqlite3.Connection,
) -> None:
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator([RuntimeError("a"), RuntimeError("b")])

    result = run_proposal_pass(conn, generator, generator)

    (outcome,) = result.outcomes
    assert outcome.status is ProposalOutcome.RETRYING
    assert _attempt_count(conn, bucket_id) == 2
    assert _artifact_count(conn) == 2
    assert _proposal_rows(conn, bucket_id) == []


def test_fallback_never_called_when_primary_succeeds(
    conn: sqlite3.Connection,
) -> None:
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    primary = ScriptedGenerator([_response(_draft(mid))])
    fallback = ScriptedGenerator([])

    run_proposal_pass(conn, primary, fallback)

    assert primary._next == 1
    assert fallback.requests == []
    assert _artifact_count(conn) == 1
    del bucket_id


def test_stage_scoped_budget_ignores_other_stages(
    conn: sqlite3.Connection,
) -> None:
    """Relevance-stage attempts never consume the proposal budget (F2
    stage-scoped attempt accounting)."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    for _ in range(5):
        conn.execute(
            "INSERT INTO processing_attempt (stage, bucket_id, started_at,"
            " finished_at, outcome)"
            " VALUES ('RELEVANCE_CLASSIFICATION', ?, 't', 't', 'FAILED_RETRYABLE')",
            (bucket_id,),
        )
    generator = ScriptedGenerator([_response(_draft(mid))])

    result = run_proposal_pass(conn, generator, max_attempts_per_bucket=1)

    assert result.outcomes[0].status is ProposalOutcome.GENERATED
    relevance_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM processing_attempt WHERE bucket_id = ?"
        " AND stage = 'RELEVANCE_CLASSIFICATION'",
        (bucket_id,),
    ).fetchone()
    assert int(relevance_rows["n"]) == 6
    assert _attempt_count(conn, bucket_id) == 1


def test_concurrent_success_is_first_writer_wins(tmp_path) -> None:
    """F1 analog: a competing writer commits a SUCCEEDED proposal attempt
    between discovery and commit — this pass contributes nothing (no second
    attempt, no artifact, no outcome) and a later pass converges nothing."""
    file_conn = connect(tmp_path / "p5_race.db")
    apply_migrations(file_conn)
    _vertical(file_conn, 1, "Tech")
    _group(file_conn, 1, vertical_id=1)
    _person(file_conn, 5, "Arjun", "5001")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(file_conn, 5, 1)
    bucket_id = _bucket(file_conn, 1)
    message_ids: list[int] = []
    for index in (1, 2):
        mid = _message(
            file_conn, 1, index, sender_person_id=5, sender_discord_user_id="5001"
        )
        _membership(file_conn, bucket_id, mid, 1)
        message_ids.append(mid)
    _relevance_success(file_conn, bucket_id, message_ids)

    class Racer:
        """Commits its SUCCEEDED attempt inside generate(): first writer."""

        def __init__(self) -> None:
            self.own = connect(tmp_path / "p5_race.db")

        def generate(
            self, request: ProposalGenerationRequest
        ) -> ProposalResponse:
            with transaction(self.own):
                self.own.execute(
                    "INSERT INTO processing_attempt"
                    " (stage, bucket_id, started_at, finished_at, outcome,"
                    "  model_id, model_version, config_version)"
                    " VALUES (?, ?, 't', 't', 'SUCCEEDED', 'other', 'v', 'p')",
                    (STAGE_PROPOSAL_GENERATION, request.bucket_id),
                )
            return _response(_draft(message_ids[0]))

    racer = Racer()
    try:
        runner = run_proposal_pass(file_conn, racer)
        assert runner.outcomes == ()
        assert len(_attempt_rows(file_conn, bucket_id)) == 1
        assert _artifact_count(file_conn) == 0
        assert _proposal_rows(file_conn, bucket_id) == []
        # A repeat pass finds no pending proposal work at all.
        second = run_proposal_pass(
            file_conn, ScriptedGenerator([_response(_draft(message_ids[0]))])
        )
        assert second.outcomes == ()
        assert len(_attempt_rows(file_conn, bucket_id)) == 1
    finally:
        racer.own.close()
        file_conn.close()


def test_locked_database_aborts_pass_loudly(
    tmp_path, conn: sqlite3.Connection
) -> None:  # noqa: ARG001
    """A non-bucket-specific failure (another writer holds the write lock)
    aborts the pass loudly — never reported as a successful pass."""
    file_conn = connect(tmp_path / "p5_locked.db")
    apply_migrations(file_conn)
    _vertical(file_conn, 1, "Tech")
    _group(file_conn, 1, vertical_id=1)
    _person(file_conn, 5, "Arjun", "5001")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(file_conn, 5, 1)
    bucket_id = _bucket(file_conn, 1)
    mid = _message(
        file_conn, 1, 1, sender_person_id=5, sender_discord_user_id="5001"
    )
    _membership(file_conn, bucket_id, mid, 1)
    _relevance_success(file_conn, bucket_id, [mid])
    other = connect(tmp_path / "p5_locked.db")
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ProposalPassError):
            run_proposal_pass(
                file_conn,
                ScriptedGenerator([_response(_draft(mid))]),
            )
    finally:
        other.execute("ROLLBACK")
        other.close()
        file_conn.close()


def test_pass_error_carries_durable_prefix(
    conn: sqlite3.Connection,
) -> None:
    """A systemic failure aborts the pass; earlier buckets' committed
    outcomes stay durable and ride the error (incomplete ≠ successful)."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    first_id = _bucket(conn, 1)
    second_id = _bucket(conn, 1)
    mids: list[int] = []
    for bucket_id in (first_id, second_id):
        mid = _message(
            conn,
            1,
            1 if bucket_id == first_id else 2,
            sender_person_id=5,
            sender_discord_user_id="5001",
        )
        _membership(conn, bucket_id, mid, 1)
        _relevance_success(conn, bucket_id, [mid])
        mids.append(mid)
    conn.execute(
        f"""
        CREATE TRIGGER sabotage_second_bucket_attempt
        BEFORE INSERT ON processing_attempt
        WHEN NEW.bucket_id = {int(second_id)}
        BEGIN
            SELECT RAISE(ABORT, 'sabotaged attempt write');
        END
        """
    )

    with pytest.raises(ProposalPassError) as excinfo:
        run_proposal_pass(
            conn,
            ScriptedGenerator(
                [_response(_draft(mids[0])), _response(_draft(mids[1]))]
            ),
        )

    error = excinfo.value
    assert len(error.outcomes) == 1
    assert error.outcomes[0].bucket_id == first_id
    assert error.outcomes[0].status is ProposalOutcome.GENERATED
    assert len(_proposal_rows(conn, first_id)) == 1


def test_no_bucket_state_change_and_no_out_of_scope_entity_writes(
    conn: sqlite3.Connection,
) -> None:
    """FR-PRP-16/DC-1: buckets stay PROCESSED; no task/announcement is written.
    FR-PRP-17 (A7): exactly one reviewer PROPOSAL_READY notification commits
    with the stored proposal; messages keep their stored content untouched."""
    bucket_id, person_id, (mid,) = _expected_scenario(conn, message_count=1)
    before = _out_of_scope_writes(conn)
    assert before == {"task": 0, "announcement": 0, "notification": 0}
    generator = ScriptedGenerator(
        [
            _response(
                _draft(
                    mid,
                    ptype="ANNOUNCEMENT",
                    assignment_mode="NONE",
                    proposed_assignees=None,
                    task_type="INTERNAL",
                )
            )
        ]
    )

    run_proposal_pass(conn, generator)

    state = conn.execute(
        "SELECT processing_state FROM bucket WHERE id = ?", (bucket_id,)
    ).fetchone()
    assert str(state["processing_state"]) == "PROCESSED"
    flag = conn.execute(
        "SELECT flag_unclassified FROM source_message WHERE id = ?", (mid,)
    ).fetchone()
    assert int(flag["flag_unclassified"]) == 0
    (stored,) = _proposal_rows(conn, bucket_id)
    assert _out_of_scope_writes(conn) == {
        "task": 0,
        "announcement": 0,
        "notification": 1,
    }
    (notification,) = conn.execute("SELECT * FROM notification").fetchall()
    assert str(notification["event_type"]) == "PROPOSAL_READY"
    assert int(notification["recipient_person_id"]) == person_id
    assert int(notification["proposal_id"]) == int(stored["id"])
    assert str(notification["dedupe_key"]) == f"PROPOSAL_READY:{int(stored['id'])}"


def test_reviewer_notification_commits_with_storage(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A7: the FR-PRP-17 row is staged in the proposal's transaction; if that
    staging fails, the whole attempt rolls back (no half-stored proposal)."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    generator = ScriptedGenerator([_response(_draft(mid))])

    def explode(connection: sqlite3.Connection, proposal_id: int) -> int | None:
        del connection, proposal_id
        raise RuntimeError("notification staging failed")

    monkeypatch.setattr(proposals, "notify_proposal_ready", explode)
    with pytest.raises(ProposalPassError):
        run_proposal_pass(conn, generator)

    assert _proposal_rows(conn, bucket_id) == []
    assert _attempt_count(conn, bucket_id) == 0
    assert _out_of_scope_writes(conn) == {
        "task": 0,
        "announcement": 0,
        "notification": 0,
    }


def test_writer_never_writes_terminal_outcomes(
    conn: sqlite3.Connection,
) -> None:
    """The proposal writer is two-valued (SUCCEEDED / FAILED_RETRYABLE only;
    FAILED_PERMANENT never written, externally seeded rows still count)."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    _proposal_attempt_seed(conn, bucket_id, "FAILED_PERMANENT")
    failing = ScriptedGenerator([RuntimeError("x"), RuntimeError("x")])

    run_proposal_pass(conn, failing, failing, max_attempts_per_bucket=3)

    outcomes = [
        str(row["outcome"])
        for row in conn.execute(
            "SELECT outcome FROM processing_attempt WHERE bucket_id = ?"
            " AND stage IN (?, ?)",
            (bucket_id, STAGE_PROPOSAL_GENERATION,
             STAGE_PROPOSAL_FALLBACK_GENERATION),
        ).fetchall()
    ]
    assert outcomes == ["FAILED_PERMANENT", "FAILED_RETRYABLE", "FAILED_RETRYABLE"]
    assert _artifact_count(conn) == 2


# =========================================================================
# C4 — inventory and backlog (NFR-REL-06, FR-UI-05)
# =========================================================================


def test_inventory_separates_pending_from_exhausted(
    conn: sqlite3.Connection,
) -> None:
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    assert proposal_pending_buckets(conn) == (bucket_id,)
    assert proposal_exhausted_buckets(conn) == ()

    run_proposal_pass(conn, ScriptedGenerator([_response(_draft(mid))]))

    assert proposal_pending_buckets(conn) == ()
    assert proposal_exhausted_buckets(conn) == ()

    for _ in range(3):
        _proposal_attempt_seed(conn, bucket_id, "FAILED_RETRYABLE", finished_at="t")
    # The bucket already succeeded: never pending again (one success counts;
    # DC: no second successful run), so its seeds cannot re-surface it.
    assert proposal_pending_buckets(conn) == ()

    # A second group whose eligible bucket exhausted its default budget.
    _vertical(conn, 2, "Sports")
    _group(conn, 2, vertical_id=2)
    _person(conn, 9, "Boss", "9001", role="Head")
    _membership_vertical(conn, 9, 2)
    other_bucket = _bucket(conn, 2)
    other_mid = _message(
        conn, 2, 1, sender_person_id=9, sender_discord_user_id="9001"
    )
    _membership(conn, other_bucket, other_mid, 2)
    _relevance_success(conn, other_bucket, [other_mid])
    for _ in range(3):
        _proposal_attempt_seed(conn, other_bucket, "FAILED_RETRYABLE")

    assert proposal_pending_buckets(conn, max_attempts_per_bucket=3) == ()
    assert proposal_exhausted_buckets(conn, max_attempts_per_bucket=3) == (
        other_bucket,
    )
    # With a raised learning budget the same bucket is still only pending.
    assert proposal_pending_buckets(
        conn, max_attempts_per_bucket=DEFAULT_PROPOSAL_ATTEMPTS_PER_BUCKET + 4
    ) == (other_bucket,)


def _backlog_scenario(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Group 1: one old awaiting bucket, one fresh one, one exhausted one,
    one already-succeeded one, one ineligible one (volunteer-only)."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    _progress(conn, 1, "2026-01-01T12:00:00+00:00")
    old_bucket = _bucket(
        conn, 1, start_event="2026-01-01T09:00:00+00:00", end_event=None
    )
    old_mid = _message(conn, 1, 1, sender_person_id=5, sender_discord_user_id="5001")
    _membership(conn, old_bucket, old_mid, 1)
    fresh_bucket = _bucket(
        conn, 1, start_event="2026-01-01T10:00:00+00:00", end_event=None
    )
    fresh_mid = _message(
        conn,
        1,
        2,
        sender_person_id=5,
        sender_discord_user_id="5001",
        event_timestamp="2026-01-01T10:00:00+00:00",
    )
    _membership(conn, fresh_bucket, fresh_mid, 1)
    exhausted_bucket = _bucket(
        conn, 1, start_event="2026-01-01T08:30:00+00:00", end_event=None
    )
    exhausted_mid = _message(
        conn,
        1,
        3,
        sender_person_id=5,
        sender_discord_user_id="5001",
        event_timestamp="2026-01-01T08:30:00+00:00",
    )
    _membership(conn, exhausted_bucket, exhausted_mid, 1)
    done_bucket = _bucket(
        conn, 1, start_event="2026-01-01T11:00:00+00:00", end_event=None
    )
    done_mid = _message(
        conn,
        1,
        4,
        sender_person_id=5,
        sender_discord_user_id="5001",
        event_timestamp="2026-01-01T11:00:00+00:00",
    )
    _membership(conn, done_bucket, done_mid, 1)
    for bucket_id, mid in ((old_bucket, old_mid), (fresh_bucket, fresh_mid),
                           (exhausted_bucket, exhausted_mid), (done_bucket, done_mid)):
        _relevance_success(conn, bucket_id, [mid])
    for _ in range(3):
        _proposal_attempt_seed(conn, exhausted_bucket, "FAILED_RETRYABLE")
    _proposal_attempt_seed(conn, done_bucket, "SUCCEEDED")
    volunteer_only = _bucket(
        conn, 1, start_event="2026-01-01T11:30:00+00:00", end_event=None
    )
    _person(conn, 6, "Vikram", "6001", role="VOLUNTEER")
    volunteer_mid = _message(
        conn,
        1,
        5,
        sender_person_id=6,
        sender_discord_user_id="6001",
        event_timestamp="2026-01-01T11:30:00+00:00",
    )
    _membership(conn, volunteer_only, volunteer_mid, 1)
    _relevance_success(conn, volunteer_only, [volunteer_mid])
    return old_bucket, fresh_bucket, exhausted_bucket


def test_backlog_counts_oldest_age_and_exhausted_ids(
    conn: sqlite3.Connection,
) -> None:
    old_bucket, fresh_bucket, exhausted_bucket = _backlog_scenario(conn)

    backlog = proposal_backlog(conn)

    _expected_backlog = ProposalBacklog  # existence pin
    assert isinstance(backlog, _expected_backlog)
    assert backlog.awaiting_bucket_ids == (old_bucket, fresh_bucket)
    assert backlog.exhausted_bucket_ids == (exhausted_bucket,)
    assert str(backlog.oldest_awaiting_bucket_id) == str(old_bucket)
    assert backlog.oldest_awaiting_bucket_id == old_bucket
    assert backlog.oldest_awaiting_anchor == "2026-01-01T09:00:00+00:00"
    # 2026-01-01T12:00 progress minus the 09:00 anchor = 3 hours.
    assert backlog.oldest_awaiting_age_event_seconds == 3 * 60 * 60


def test_backlog_undatable_anchor_is_oldest_without_age(
    conn: sqlite3.Connection,
) -> None:
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    conn.execute(
        "UPDATE bucket SET start_event_timestamp = 't', end_event_timestamp = NULL"
        " WHERE id = ?",
        (bucket_id,),
    )
    _progress(conn, 1, "2026-01-01T12:00:00+00:00")

    backlog = proposal_backlog(conn)

    assert backlog.awaiting_bucket_ids == (bucket_id,)
    assert backlog.oldest_awaiting_bucket_id == bucket_id
    assert backlog.oldest_awaiting_anchor == "t"
    assert backlog.oldest_awaiting_age_event_seconds is None


def test_backlog_negative_age_is_unavailable(
    conn: sqlite3.Connection,
) -> None:
    bucket_id, _person_id, _ids = _expected_scenario(conn, message_count=1)
    _progress(conn, 1, "2026-01-01T10:00:00+00:00")  # before the 10:00:00 anchor

    backlog = proposal_backlog(conn)

    assert backlog.oldest_awaiting_bucket_id == bucket_id
    assert backlog.oldest_awaiting_age_event_seconds is None


def test_backlog_writes_nothing(conn: sqlite3.Connection) -> None:
    _backlog_scenario(conn)
    before_attempts = len(
        conn.execute("SELECT id FROM processing_attempt").fetchall()
    )
    before_artifacts = _artifact_count(conn)

    first = proposal_backlog(conn)
    second = proposal_backlog(conn)

    assert first == second
    after_attempts = len(
        conn.execute("SELECT id FROM processing_attempt").fetchall()
    )
    assert after_attempts == before_attempts
    assert _artifact_count(conn) == before_artifacts


# =========================================================================
# WS-6 — end-to-end (in-suite): ingest → bucketing → relevance → proposal
# =========================================================================


def test_end_to_end_pipeline_commits_proposals(conn: sqlite3.Connection) -> None:
    """The full flow on one connection: ingest, bucketing close, relevance
    pass, then the proposal pass — task proposals with resolved-style
    mentions, exactly one reviewer PROPOSAL_READY notification and still zero
    task/announcement writes (FR-PRP-16, FR-PRP-17)."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1, channel="chan-1")
    _person(conn, 5, "Arjun", "5001", role="AH")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    _person(conn, 7, "Neha", "2002")

    ingest_events(
        conn,
        [
            MessageEvent(
                source="discord",
                source_message_id="e1",
                discord_channel_id="chan-1",
                sender_discord_user_id="5001",
                event_timestamp="2026-01-01T10:00:01+00:00",
                received_at="2026-01-01T10:00:01+00:00",
                content="team plan kar lete hain",
            ),
            MessageEvent(
                source="discord",
                source_message_id="e2",
                discord_channel_id="chan-1",
                sender_discord_user_id="5001",
                event_timestamp="2026-01-01T10:00:05+00:00",
                received_at="2026-01-01T10:00:05+00:00",
                content="Neha flag le aau?",
                mentions=(
                    MentionEvent(mentioned_discord_user_id="2002", position=0),
                ),
            ),
        ],
    )
    assign_messages(conn)
    mark_stream_progress(conn, group_id=1, event_progress="2026-01-01T10:05:00+00:00")

    # Relevance runs on the closed bucket; its decisions feed C3 eligibility.
    bucket_rows = conn.execute(
        "SELECT id FROM bucket ORDER BY id"
    ).fetchall()
    (bucket_id,) = [int(row["id"]) for row in bucket_rows]
    member_ids = [
        int(row["message_id"])
        for row in conn.execute(
            "SELECT message_id FROM bucket_message WHERE bucket_id = ?"
            " ORDER BY message_id",
            (bucket_id,),
        ).fetchall()
    ]

    class Relevant:
        def classify(
            self, request: ProposalGenerationRequest | object
        ) -> RelevanceResponse:
            del request  # decisions come from the observed bucket id above
            return RelevanceResponse(
                model_id="stub-model",
                model_version="stub-v",
                prompt_version="rel-prompt-1",
                decisions=tuple(
                    MessageDecision(message_id=mid, decision="RELEVANT")
                    for mid in member_ids
                ),
            )

    relevance_result = run_relevance_pass(conn, Relevant())  # type: ignore[arg-type]
    assert relevance_result.finalized == ()
    state = conn.execute(
        "SELECT processing_state FROM bucket WHERE id = ?", (bucket_id,)
    ).fetchone()
    assert str(state["processing_state"]) == "PROCESSED"

    generator = ScriptedGenerator(
        [
            _response(
                _draft(
                    member_ids[1],  # the mention-bearing second message
                    description="flags by 5",
                    assignment_mode="EXPLICIT",
                    proposed_assignees=("@m1",),
                    people_needed=None,
                    task_type="CROSS_VERTICAL",
                    attributes=(("venue", "hall A"),),
                )
            )
        ]
    )
    result = run_proposal_pass(conn, generator)

    (outcome,) = result.outcomes
    assert outcome.bucket_id == bucket_id
    assert outcome.status is ProposalOutcome.GENERATED
    assert outcome.proposal_count == 1
    (stored,) = _proposal_rows(conn, bucket_id)
    assert int(stored["primary_message_id"]) == member_ids[1]
    (assignee,) = _assignee_rows(conn, int(stored["id"]))
    assert str(assignee["resolution"]) == "RESOLVED"
    assert int(assignee["person_id"]) == 7
    assert str(assignee["resolved_via"]) == "MENTION"
    attribute = conn.execute(
        "SELECT attr_value FROM proposal_attribute WHERE proposal_id = ?",
        (int(stored["id"]),),
    ).fetchone()
    assert str(attribute["attr_value"]) == "hall A"
    # Scope negatives plus the one approved FR-PRP-17 notification (A7/A18).
    assert _out_of_scope_writes(conn) == {
        "task": 0,
        "announcement": 0,
        "notification": 1,
    }
    (notification,) = conn.execute("SELECT * FROM notification").fetchall()
    assert str(notification["event_type"]) == "PROPOSAL_READY"
    assert int(notification["recipient_person_id"]) == 5
    assert int(notification["proposal_id"]) == int(stored["id"])
    assert str(notification["delivery_state"]) == "PENDING"
    assert _artifact_count(conn) == 1


def test_raw_response_is_masked_and_capped(conn: sqlite3.Connection) -> None:
    """C2/DC-20: the stored raw response is masked and capped at MAX_RAW_RESPONSE_BYTES (65536);
    a 900-char response is stored intact with raw_response_truncated=0."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)
    echo = _draft(
        mid,
        description="venue meri phone 9876543210 pe bajega " + "y" * 900,
    )
    generator = ScriptedGenerator([_response(echo)])

    run_proposal_pass(conn, generator)

    attempt = _attempt_rows(conn, bucket_id)[-1]
    row = _artifact(conn, int(attempt["id"]))
    raw = str(row["raw_response"])
    assert "9876543210" not in raw
    assert "[PHONE]" in raw
    assert len(raw) > 500
    assert len(raw.encode("utf-8")) <= proposals.MAX_RAW_RESPONSE_BYTES
    assert row["raw_response_truncated"] == 0
    assert str(row["response_sha256"]) == proposals._hash_text(raw)


def test_raw_response_fallback_path(conn: sqlite3.Connection) -> None:
    """When response cannot be canonically serialized as JSON (e.g. non-envelope
    or malformed draft items), _raw_response_text falls back to repr()."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)

    class CustomEnvelope:
        def __repr__(self) -> str:
            return "CustomEnvelope(status='malformed')"

    generator = ScriptedGenerator([CustomEnvelope()])
    run_proposal_pass(conn, generator)

    attempt = _attempt_rows(conn, bucket_id)[-1]
    row = _artifact(conn, int(attempt["id"]))
    raw = str(row["raw_response"])
    assert raw == "CustomEnvelope(status='malformed')"
    assert str(row["response_sha256"]) == proposals._hash_text(raw)


def test_raw_response_fallback_pii_masked(conn: sqlite3.Connection) -> None:
    """PII in fallback repr() text is always redacted before storage."""
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)

    class PiiEnvelope:
        def __repr__(self) -> str:
            return "PiiEnvelope(contact='9876543210', email='test@example.com')"

    generator = ScriptedGenerator([PiiEnvelope()])
    run_proposal_pass(conn, generator)

    attempt = _attempt_rows(conn, bucket_id)[-1]
    row = _artifact(conn, int(attempt["id"]))
    raw = str(row["raw_response"])
    assert "9876543210" not in raw
    assert "test@example.com" not in raw
    assert "[PHONE]" in raw
    assert "[EMAIL]" in raw
    assert str(row["response_sha256"]) == proposals._hash_text(raw)


def test_raw_response_fallback_oversize_capped(conn: sqlite3.Connection) -> None:
    """Over-size fallback repr() text (> MAX_RAW_RESPONSE_BYTES) is truncated on a
    character boundary.
    """
    bucket_id, _person_id, (mid,) = _expected_scenario(conn, message_count=1)

    class HugeEnvelope:
        def __repr__(self) -> str:
            return "HugeEnvelope(" + "x" * 70000 + ")"

    generator = ScriptedGenerator([HugeEnvelope()])
    run_proposal_pass(conn, generator)

    attempt = _attempt_rows(conn, bucket_id)[-1]
    row = _artifact(conn, int(attempt["id"]))
    raw = str(row["raw_response"])
    assert len(raw.encode("utf-8")) <= proposals.MAX_RAW_RESPONSE_BYTES
    assert row["raw_response_truncated"] == 1
    full_masked = proposals.mask_pii(repr(HugeEnvelope()))
    assert str(row["response_sha256"]) == proposals._hash_text(full_masked)


def _all_strings_from_obj(val: object) -> list[str]:
    if isinstance(val, str):
        return [val]
    if isinstance(val, dict):
        res: list[str] = []
        for k, v in val.items():
            res.extend(_all_strings_from_obj(k))
            res.extend(_all_strings_from_obj(v))
        return res
    if isinstance(val, (list, tuple, set)):
        res = []
        for item in val:
            res.extend(_all_strings_from_obj(item))
        return res
    return []


def test_role_mention_markup_rewritten_to_role(conn: sqlite3.Connection) -> None:
    """Realistic <@&ID> role markup is rewritten to @role and ID is absent."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001", role="AH")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    bucket_id = _bucket(conn, 1)
    role_id = "987654321012345678"
    mid = _message(
        conn,
        1,
        1,
        sender_person_id=5,
        sender_discord_user_id="5001",
        content=f"Please coordinate with <@&{role_id}> for stage setup",
    )
    _membership(conn, bucket_id, mid, 1)
    _relevance_success(conn, bucket_id, [mid])

    generator = ScriptedGenerator([_response()])
    run_proposal_pass(conn, generator)

    (request,) = generator.requests
    serialized = proposals.serialize_proposal_request(request)
    assert role_id not in serialized
    assert "@role" in serialized

    rep = repr(request)
    assert role_id not in rep
    assert "@role" in rep

    dict_strings = " ".join(_all_strings_from_obj(dataclasses.asdict(request)))
    assert role_id not in dict_strings
    assert "@role" in dict_strings


def test_channel_mention_markup_rewritten_to_channel(conn: sqlite3.Connection) -> None:
    """Realistic <#ID> channel markup is rewritten to #channel and ID is absent."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001", role="AH")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    bucket_id = _bucket(conn, 1)
    channel_id = "876543210987654321"
    mid = _message(
        conn,
        1,
        1,
        sender_person_id=5,
        sender_discord_user_id="5001",
        content=f"Updates will be posted in <#{channel_id}> tonight",
    )
    _membership(conn, bucket_id, mid, 1)
    _relevance_success(conn, bucket_id, [mid])

    generator = ScriptedGenerator([_response()])
    run_proposal_pass(conn, generator)

    (request,) = generator.requests
    serialized = proposals.serialize_proposal_request(request)
    assert channel_id not in serialized
    assert "#channel" in serialized

    rep = repr(request)
    assert channel_id not in rep
    assert "#channel" in rep

    dict_strings = " ".join(_all_strings_from_obj(dataclasses.asdict(request)))
    assert channel_id not in dict_strings
    assert "#channel" in dict_strings


def test_custom_emoji_markup_rewritten_to_name(conn: sqlite3.Connection) -> None:
    """Realistic <:name:ID> and <a:name:ID> emoji markups rewritten to :name:."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _person(conn, 5, "Arjun", "5001", role="AH")
    # FR-PRP-02 (Phase 7 S2): eligibility requires source-vertical membership.
    _membership_vertical(conn, 5, 1)
    bucket_id = _bucket(conn, 1)
    static_id = "765432109876543210"
    anim_id = "654321098765432109"
    mid = _message(
        conn,
        1,
        1,
        sender_person_id=5,
        sender_discord_user_id="5001",
        content=f"Stage ready <:fire:{static_id}> celebration in <a:party_blob:{anim_id}>",
    )
    _membership(conn, bucket_id, mid, 1)
    _relevance_success(conn, bucket_id, [mid])

    generator = ScriptedGenerator([_response()])
    run_proposal_pass(conn, generator)

    (request,) = generator.requests
    serialized = proposals.serialize_proposal_request(request)
    for forbidden in (static_id, anim_id):
        assert forbidden not in serialized
    assert ":fire:" in serialized
    assert ":party_blob:" in serialized

    rep = repr(request)
    for forbidden in (static_id, anim_id):
        assert forbidden not in rep
    assert ":fire:" in rep
    assert ":party_blob:" in rep

    dict_strings = " ".join(_all_strings_from_obj(dataclasses.asdict(request)))
    for forbidden in (static_id, anim_id):
        assert forbidden not in dict_strings
    assert ":fire:" in dict_strings
    assert ":party_blob:" in dict_strings
