"""Relevance-classification service tests (Phase 4).

Requires FR-REL-01..07 (docs/SRS.md §3.3), EIR-03 (§5, lines 419–426),
NFR-REL-01..05/07 (§6.1), NFR-AUD-02 (§6.2), NFR-PERF-02 (§6.4),
NFR-SEC-04 masking at the pre-AI boundary, AI-01..04 (§7) and acceptance
criteria AC-10/AC-12/AC-13. Storage contracts come from migrations
0002/0004/0011/0012 (committed); lifecycle from backend.bucket_lifecycle
(Phase 3); queue from backend.orchestration.processing_queue (read-only).

Subtasks (docs/PHASE2_PLAN.md §13, Phase 4):

- A — stateless injected classifier adapter, whole-bucket masked request,
      opaque mention handles, provenance contract (EIR-03).
- B — backend-side schema validation of the complete response and
      all-or-nothing persistence of decisions/latency/flags.
- C — deterministic discovery (queue + the retryable gap the queue omits,
      docs/PHASE2_PLAN.md §12 coverage note) and idempotent retry.
- D — lifecycle wiring through legal transitions, retry budget, primary/
      fallback recording (NFR-REL-07), deferred UNCLASSIFIED clearing.
- E — acceptance: AC-10, AC-12, AC-13, NFR audit/latency behaviours.

The "doubt → RELEVANT" policy (FR-REL-03) and the no-UNCERTAIN vocabulary
(FR-REL-02) are carried through the classifier request contract; schema
validation can only enforce the vocabulary deterministically — semantic
compliance of a real model is a review/evaluation matter (AI-05/AI-06)
and is documented as such in the implementation record. No production
model, endpoint or credential ships here: only deterministic injected
test doubles (EIR-03: the AI layer never writes to the database).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import FrozenInstanceError

import pytest

import backend.relevance as relevance
from backend.bucket_lifecycle import (
    BucketState,
    InvalidBucketTransitionError,
    bucket_processing_state,
)
from backend.db.connection import connect, transaction
from backend.db.migrations import apply_migrations
from backend.masking import mask_pii
from backend.orchestration import processing_queue
from backend.relevance import (
    CLASSIFICATION_POLICY,
    CLASSIFICATION_POLICY_VERSION,
    DECISION_VOCABULARY,
    MASKING_RULESET_VERSION,
    STAGE_RELEVANCE_CLASSIFICATION,
    STAGE_RELEVANCE_FALLBACK_CLASSIFICATION,
    ContextMessage,
    MessageDecision,
    RelevanceOutcome,
    RelevancePassError,
    RelevanceRequest,
    RelevanceResponse,
    UnknownBucketError,
    requeue_failed_bucket,
    run_relevance_pass,
    stalled_terminal_buckets,
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    apply_migrations(connection)
    yield connection
    connection.close()


# --- helpers -------------------------------------------------------------


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


def _person(conn: sqlite3.Connection, person_id: int, name: str, discord_id: str) -> int:
    conn.execute(
        "INSERT INTO person (id, name, discord_user_id, role, active)"
        " VALUES (?, ?, ?, 'VOLUNTEER', 1)",
        (person_id, name, discord_id),
    )
    return person_id


def _message(
    conn: sqlite3.Connection,
    group_id: int,
    index: int,
    *,
    sender_person_id: int | None = None,
    sender_discord_user_id: str | None = None,
    content: str | None = None,
    reply_reference: str | None = None,
    event_timestamp: str | None = None,
) -> int:
    """Insert one source_message row with per-call unique identity; returns id."""
    cursor = conn.execute(
        "INSERT INTO source_message"
        " (source, source_message_id, group_id, sender_person_id,"
        "  sender_discord_user_id, event_timestamp, received_at, content,"
        "  reply_reference, flag_unclassified)"
        " VALUES ('discord', ?, ?, ?, ?, ?, '2026-01-01T10:00:00+00:00', ?, ?, 0)",
        (
            f"mid-{group_id}-{index}",
            group_id,
            sender_person_id,
            sender_discord_user_id,
            event_timestamp if event_timestamp is not None else "2026-01-01T10:00:00+00:00",
            content if content is not None else "kuch karo yaar",
            reply_reference,
        ),
    )
    return int(cursor.lastrowid or 0)


def _bucket(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    state: str = "CLOSED",
) -> int:
    cursor = conn.execute(
        "INSERT INTO bucket (group_id, start_event_timestamp, end_event_timestamp,"
        " parameters_used, processing_state)"
        " VALUES (?, '2026-01-01T10:00:00+00:00', '2026-01-01T10:00:10+00:00',"
        "         '{\"window_w\": 10, \"burst_gap_g\": 5, \"max_duration_m\": 30,"
        "          \"close_grace_c\": 3}', ?)",
        (group_id, state),
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


def _standard_scenario(
    conn: sqlite3.Connection, *, message_count: int = 2
) -> tuple[int, list[tuple[int, str]]]:
    """A mapped group + vertical, one CLOSED bucket, message_count messages."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    members: list[tuple[int, str]] = []
    for index in range(1, message_count + 1):
        message_id = _message(conn, 1, index, content=f"message no {index}")
        _membership(conn, bucket_id, message_id, 1)
        members.append((message_id, f"mid-1-{index}"))
    return bucket_id, members


def _decisions_from_members(
    members: Sequence[tuple[int, str]],
    decision: str = "RELEVANT",
) -> RelevanceResponse:
    return RelevanceResponse(
        model_id="stub-model",
        model_version="stub-7b-v2",
        prompt_version="rel-prompt-1",
        decisions=tuple(
            MessageDecision(message_id=message_id, decision=decision)
            for message_id, _ in members
        ),
    )


class ScriptedClassifier:
    """Deterministic classifier double (stateless per call, injected).

    ``script`` items are :class:`RelevanceResponse` (returned) or ``Exception``
    (raised). Requests are captured for masking/contract assertions.
    """

    def __init__(self, script: Sequence[RelevanceResponse | Exception]):
        self._script = list(script)
        self._next = 0
        self.requests: list[RelevanceRequest] = []

    def classify(self, request: RelevanceRequest) -> RelevanceResponse:
        self.requests.append(request)
        if self._next >= len(self._script):
            raise AssertionError("script exhausted; unexpected additional call")
        item = self._script[self._next]
        self._next += 1
        if isinstance(item, BaseException):
            raise item
        return item


def _attempts(conn: sqlite3.Connection, bucket_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM processing_attempt WHERE bucket_id = ? ORDER BY id",
        (bucket_id,),
    ).fetchall()


def _attempt_count(conn: sqlite3.Connection, bucket_id: int) -> int:
    return len(_attempts(conn, bucket_id))


def _decisions(conn: sqlite3.Connection, bucket_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM relevance_decision WHERE bucket_id = ? ORDER BY message_id",
        (bucket_id,),
    ).fetchall()


def _message_ids(conn: sqlite3.Connection, bucket_id: int) -> list[int]:
    return [
        int(row["message_id"])
        for row in conn.execute(
            "SELECT message_id FROM bucket_message WHERE bucket_id = ? ORDER BY message_id",
            (bucket_id,),
        ).fetchall()
    ]


# --- Subtask A — adapter contract, masking, provenance --------------------


def test_request_carries_whole_bucket_context(
    conn: sqlite3.Connection,
) -> None:
    """FR-REL-01/EIR-03: ids, sender info, times, reply links, mention
    handles, group and vertical context — the whole bucket as input."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1, channel="chan-nine")
    person_id = _person(conn, 3, "Arjun", "1001")
    bucket_id = _bucket(conn, 1)
    mid = _message(
        conn,
        1,
        1,
        sender_person_id=person_id,
        sender_discord_user_id="1001",
        content="call me",
        reply_reference="some-reply-ref",
        event_timestamp="2026-01-01T10:00:05+00:00",
    )
    _membership(conn, bucket_id, mid, 1)
    _mention(conn, mid, "2002", 0)
    classifier = ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])

    result = run_relevance_pass(conn, classifier)
    assert result.outcomes[0].status is RelevanceOutcome.CLASSIFIED
    request = classifier.requests[0]
    assert request.bucket_id == bucket_id
    assert request.group_discord_channel_id == "chan-nine"
    assert request.vertical_name == "Tech"
    (entry,) = request.messages
    assert entry.message_id == mid
    assert entry.sender_name == "Arjun"
    assert entry.sender_discord_user_id == "1001"
    assert entry.sender_person_id == person_id
    assert entry.event_timestamp == "2026-01-01T10:00:05+00:00"
    assert entry.reply_reference == "some-reply-ref"
    assert entry.mention_handles == ("2002",)
    assert entry.masked_content == "call me"


def test_unmapped_group_carries_vertical_context_as_missing(
    conn: sqlite3.Connection,
) -> None:
    """DR-04/A2: an unmapped group's absent vertical is missing in the request,
    never a guessed name."""
    _group(conn, 2, vertical_id=None)
    bucket_id = _bucket(conn, 2)
    mid = _message(conn, 2, 1)
    _membership(conn, bucket_id, mid, 2)
    classifier = ScriptedClassifier(
        [_decisions_from_members([(mid, "mid-2-1")], decision="IRRELEVANT")]
    )

    run_relevance_pass(conn, classifier)
    request = classifier.requests[0]
    assert request.vertical_name is None
    assert request.group_discord_channel_id == "chan-2"


def test_unknown_sender_carries_absent_sender_information(
    conn: sqlite3.Connection,
) -> None:
    """FR-REL-01: unknown senders are classified like everyone else (AC-10);
    their absent sender identity stays absent (DR-04)."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1, sender_person_id=None, sender_discord_user_id=None)
    _membership(conn, bucket_id, mid, 1)
    classifier = ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])

    result = run_relevance_pass(conn, classifier)
    (entry,) = classifier.requests[0].messages
    assert entry.sender_name is None
    assert entry.sender_discord_user_id is None
    assert entry.sender_person_id is None
    assert result.outcomes[0].decision_count == 1


def test_masking_applied_before_every_invocation(
    conn: sqlite3.Connection,
) -> None:
    """NFR-SEC-04/EIR-03: personal data is masked immediately before the model
    call; backend content stays verbatim (DR-03)."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    original = ("phone 98765 43210 email a.b@site.co PAN ABCDE1234F")
    mid = _message(conn, 1, 1, sender_person_id=None, content=original)
    _membership(conn, bucket_id, mid, 1)
    classifier = ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])

    result = run_relevance_pass(conn, classifier)
    assert result.outcomes[0].status is RelevanceOutcome.CLASSIFIED
    (entry,) = classifier.requests[0].messages
    assert entry.masked_content == mask_pii(original)
    assert "[PHONE]" in entry.masked_content
    assert "[EMAIL]" in entry.masked_content
    assert "[GOV_ID]" in entry.masked_content
    stored = conn.execute(
        "SELECT content FROM source_message WHERE id = ?", (mid,)
    ).fetchone()
    assert stored["content"] == original


def test_masking_applied_before_fallback_invocation(
    conn: sqlite3.Connection,
) -> None:
    """NFR-SEC-04: the fallback call is masked exactly like the primary."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    original = "phone 98765 43210"
    mid = _message(conn, 1, 1, content=original)
    _membership(conn, bucket_id, mid, 1)
    primary = ScriptedClassifier([RuntimeError("primary outage")])
    fallback = ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])

    result = run_relevance_pass(conn, primary, fallback)
    assert result.outcomes[0].status is RelevanceOutcome.CLASSIFIED
    assert primary.requests and fallback.requests
    (entry,) = fallback.requests[0].messages
    assert entry.masked_content == mask_pii(original)


def test_mention_handles_stay_opaque(
    conn: sqlite3.Connection,
) -> None:
    """EIR-03: mentions are opaque handles; 17-digit Discord snowflakes are
    never mangled by masking or remapping at this boundary."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1, content="generator")
    _membership(conn, bucket_id, mid, 1)
    _mention(conn, mid, "1234567890123456789", 0)
    classifier = ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])

    run_relevance_pass(conn, classifier)
    (entry,) = classifier.requests[0].messages
    assert entry.mention_handles == ("1234567890123456789",)
    assert "1234567890123456789" not in entry.masked_content


def test_policy_contract_carried_on_every_request(
    conn: sqlite3.Connection,
) -> None:
    """FR-REL-02/03 vocabulary and doubt policy pass through the classifier
    contract; they cannot be guaranteed semantically, only conveyed."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1)
    _membership(conn, bucket_id, mid, 1)
    classifier = ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])

    run_relevance_pass(conn, classifier)
    (request,) = classifier.requests
    assert CLASSIFICATION_POLICY in request.instructions


def test_policy_carried_on_fallback_request_too(
    conn: sqlite3.Connection,
) -> None:
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1)
    _membership(conn, bucket_id, mid, 1)
    primary = ScriptedClassifier([RuntimeError("down")])
    fallback = ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])

    run_relevance_pass(conn, primary, fallback)
    assert CLASSIFICATION_POLICY in fallback.requests[0].instructions


def test_classifier_failure_with_raw_exception_is_retryable(
    conn: sqlite3.Connection,
) -> None:
    """FR-REL-07: an exception from the model boundary is an execution
    failure; nothing partial is ever written."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1)
    _membership(conn, bucket_id, mid, 1)
    classifier = ScriptedClassifier([ConnectionError("model down")])

    result = run_relevance_pass(conn, classifier)
    outcome = result.outcomes[0]
    assert outcome.status is RelevanceOutcome.RETRYING
    assert outcome.attempt_ids != ()
    row = conn.execute(
        "SELECT outcome, model_id, error FROM processing_attempt WHERE id = ?",
        (outcome.attempt_ids[0],),
    ).fetchone()
    assert row["outcome"] == "FAILED_RETRYABLE"
    assert row["model_id"] is None
    assert row["error"] is not None


# --- Subtask B — validation and persistence ------------------------------


_MISSING_COVERAGE = "incomplete: not every bucket message appears exactly once"


def test_valid_output_persists_provenance_and_timestamps(
    conn: sqlite3.Connection,
) -> None:
    """FR-REL-02/05: attempt-scoped decisions with copied provenance and a
    backend-stamped decided_at."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    mid_a = _message(conn, 1, 1)
    mid_b = _message(conn, 1, 2)
    _membership(conn, bucket_id, mid_a, 1)
    _membership(conn, bucket_id, mid_b, 1)
    response = RelevanceResponse(
        model_id="stub-model",
        model_version="stub-7b-v2",
        prompt_version="rel-prompt-1",
        decisions=(
            MessageDecision(message_id=mid_a, decision="RELEVANT"),
            MessageDecision(message_id=mid_b, decision="IRRELEVANT"),
        ),
    )
    classifier = ScriptedClassifier([response])

    result = run_relevance_pass(conn, classifier)
    outcome = result.outcomes[0]
    assert outcome.status is RelevanceOutcome.CLASSIFIED
    assert outcome.decision_count == 2
    attempt_id = outcome.attempt_ids[0]
    rows = _decisions(conn, bucket_id)
    assert len(rows) == 2
    by_message = {int(row["message_id"]): row for row in rows}
    assert by_message[mid_a]["decision"] == "RELEVANT"
    assert by_message[mid_b]["decision"] == "IRRELEVANT"
    for row in rows:
        assert row["attempt_id"] == attempt_id
        assert row["bucket_id"] == bucket_id
        assert row["model_id"] == "stub-model"
        assert row["model_version"] == "stub-7b-v2"
        assert row["prompt_version"] == "rel-prompt-1"
        assert row["decided_at"] is not None and row["decided_at"] != ""
    attempt = conn.execute(
        "SELECT * FROM processing_attempt WHERE id = ?", (attempt_id,)
    ).fetchone()
    assert attempt["stage"] == STAGE_RELEVANCE_CLASSIFICATION
    assert attempt["outcome"] == "SUCCEEDED"
    assert attempt["finished_at"] is not None
    assert attempt["model_id"] == "stub-model"
    assert attempt["model_version"] == "stub-7b-v2"
    assert attempt["config_version"] == "rel-prompt-1"


def test_missing_message_in_output_rejected_whole(
    conn: sqlite3.Connection,
) -> None:
    """EIR-03: an incomplete output is an execution failure — no decisions
    are written for that attempt, never partially."""
    bucket_id, members = _standard_scenario(conn)
    response = _decisions_from_members(members[:1])
    classifier = ScriptedClassifier([response])

    result = run_relevance_pass(conn, classifier)
    outcome = result.outcomes[0]
    assert outcome.status is RelevanceOutcome.RETRYING
    assert _decisions(conn, bucket_id) == []
    attempt = conn.execute(
        "SELECT * FROM processing_attempt WHERE id = ?", (outcome.attempt_ids[0],)
    ).fetchone()
    assert attempt["outcome"] == "FAILED_RETRYABLE"
    assert attempt["error"] is not None


def test_foreign_message_id_rejected(conn: sqlite3.Connection) -> None:
    bucket_id, members = _standard_scenario(conn)
    response = RelevanceResponse(
        model_id="stub-model",
        model_version="v1",
        prompt_version="p1",
        decisions=(
            MessageDecision(message_id=999999, decision="RELEVANT"),
            MessageDecision(message_id=members[0][0], decision="RELEVANT"),
        ),
    )
    classifier = ScriptedClassifier([response])

    result = run_relevance_pass(conn, classifier)
    assert result.outcomes[0].status is RelevanceOutcome.RETRYING
    assert _decisions(conn, bucket_id) == []


def test_duplicate_message_in_output_rejected(
    conn: sqlite3.Connection,
) -> None:
    bucket_id, members = _standard_scenario(conn)
    response = RelevanceResponse(
        model_id="stub-model",
        model_version="v1",
        prompt_version="p1",
        decisions=tuple(
            MessageDecision(message_id=members[0][0], decision="RELEVANT")
            for _ in range(2)
        ),
    )
    classifier = ScriptedClassifier([response])

    result = run_relevance_pass(conn, classifier)
    assert result.outcomes[0].status is RelevanceOutcome.RETRYING
    assert _decisions(conn, bucket_id) == []


def test_vocabulary_violation_rejected(conn: sqlite3.Connection) -> None:
    """FR-REL-02: there is no UNCERTAIN state; the backend enforces the
    vocabulary deterministically (schema-check, EIR-03)."""
    bucket_id, members = _standard_scenario(conn)
    response = _decisions_from_members(members, decision="UNCERTAIN")
    classifier = ScriptedClassifier([response])

    result = run_relevance_pass(conn, classifier)
    assert result.outcomes[0].status is RelevanceOutcome.RETRYING
    assert _decisions(conn, bucket_id) == []


def test_lowercase_decision_is_not_the_accepted_vocabulary(
    conn: sqlite3.Connection,
) -> None:
    """The stored CHECK is case-sensitive; 'relevant' is not RELEVANT."""
    bucket_id, members = _standard_scenario(conn)
    response = _decisions_from_members(members, decision="relevant")
    classifier = ScriptedClassifier([response])

    result = run_relevance_pass(conn, classifier)
    assert result.outcomes[0].status is RelevanceOutcome.RETRYING


def test_missing_provenance_rejected(conn: sqlite3.Connection) -> None:
    """EIR-03: model id/version and prompt/config version are required and
    validated by the backend, never trusted blindly."""
    bucket_id, members = _standard_scenario(conn)
    response = RelevanceResponse(
        model_id="stub-model",
        model_version="stub-7b-v2",
        prompt_version="   ",
        decisions=tuple(
            MessageDecision(message_id=message_id, decision="RELEVANT")
            for message_id, _ in members
        ),
    )
    classifier = ScriptedClassifier([response])

    result = run_relevance_pass(conn, classifier)
    assert result.outcomes[0].status is RelevanceOutcome.RETRYING
    assert _decisions(conn, bucket_id) == []


def test_blank_model_version_rejected(conn: sqlite3.Connection) -> None:
    bucket_id, members = _standard_scenario(conn)
    response = RelevanceResponse(
        model_id="stub-model",
        model_version="",
        prompt_version="p1",
        decisions=tuple(
            MessageDecision(message_id=message_id, decision="RELEVANT")
            for message_id, _ in members
        ),
    )
    classifier = ScriptedClassifier([response])

    result = run_relevance_pass(conn, classifier)
    assert result.outcomes[0].status is RelevanceOutcome.RETRYING


def test_from_empty_bucket_is_a_valid_no_message_pass(
    conn: sqlite3.Connection,
) -> None:
    """A bucket without members has vacuous coverage: no decisions, still a
    successful — recorded — attempt (missing staged, never fabricated)."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    response = RelevanceResponse(
        model_id="stub-model", model_version="v1", prompt_version="p1", decisions=()
    )
    classifier = ScriptedClassifier([response])

    result = run_relevance_pass(conn, classifier)
    outcome = result.outcomes[0]
    assert outcome.status is RelevanceOutcome.CLASSIFIED
    assert outcome.decision_count == 0
    assert bucket_processing_state(conn, bucket_id) is BucketState.PROCESSED


def test_invalid_output_failure_marks_messages_unclassified(
    conn: sqlite3.Connection,
) -> None:
    """FR-REL-07: failed classification marks the affected messages
    UNCLASSIFIED (flag exists since 0002; clearing stays deferred)."""
    bucket_id, _members = _standard_scenario(conn)
    message_rows = conn.execute(
        "SELECT message_id FROM bucket_message WHERE bucket_id = ?", (bucket_id,)
    ).fetchall()
    # Sabotage: the response covers a message id not in this bucket, so
    # validation fails even though the real messages look fully covered.
    response = RelevanceResponse(
        model_id="stub-model",
        model_version="v1",
        prompt_version="p1",
        decisions=tuple(
            MessageDecision(message_id=int(row["message_id"]), decision="RELEVANT")
            for row in message_rows
        )
        + (MessageDecision(message_id=999999, decision="RELEVANT"),),
    )
    classifier = ScriptedClassifier([response])

    result = run_relevance_pass(conn, classifier)
    assert result.outcomes[0].status is RelevanceOutcome.RETRYING
    flags = conn.execute(
        "SELECT sm.flag_unclassified FROM source_message sm"
        " JOIN bucket_message bm ON bm.message_id = sm.id WHERE bm.bucket_id = ?",
        (bucket_id,),
    ).fetchall()
    assert all(int(row["flag_unclassified"]) == 1 for row in flags)


def test_failure_keeps_source_content_verbatim(
    conn: sqlite3.Connection,
) -> None:
    """DR-03/AC-12: failures never touch source content; nothing survives as
    a derived replacement."""
    bucket_id, _members = _standard_scenario(conn)
    classifier = ScriptedClassifier([RuntimeError("boom")])

    run_relevance_pass(conn, classifier)
    contents = conn.execute(
        "SELECT content FROM source_message ORDER BY id"
    ).fetchall()
    assert [row["content"] for row in contents] == ["message no 1", "message no 2"]


def test_classified_attempt_records_latency(
    conn: sqlite3.Connection,
) -> None:
    """NFR-PERF-02: a classifier latency measurement is recorded per
    successful classification attempt, tied to the attempt (DR-01)."""
    bucket_id, members = _standard_scenario(conn)
    classifier = ScriptedClassifier([_decisions_from_members(members)])

    result = run_relevance_pass(conn, classifier)
    attempt_id = result.outcomes[0].attempt_ids[0]
    rows = conn.execute(
        "SELECT * FROM latency_measurement WHERE processing_attempt_id = ?",
        (attempt_id,),
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["metric"] == "CLASSIFIER_LATENCY"
    assert row["recorded_at"] is not None and row["recorded_at"] != ""
    assert float(row["value_seconds"]) >= 0.0
    assert row["bucket_id"] is None and row["source_message_id"] is None
    assert row["proposal_id"] is None


def test_failed_attempt_records_no_latency(conn: sqlite3.Connection) -> None:
    """Only real classifier calls contribute a classifier-latency value."""
    bucket_id, _members = _standard_scenario(conn)
    classifier = ScriptedClassifier([RuntimeError("boom")])

    result = run_relevance_pass(conn, classifier)
    rows = conn.execute(
        "SELECT * FROM latency_measurement WHERE processing_attempt_id = ?",
        (result.outcomes[0].attempt_ids[0],),
    ).fetchall()
    assert rows == []


# --- Subtask C — discovery, coverage and idempotency ---------------------


def test_open_bucket_is_never_attempted(conn: sqlite3.Connection) -> None:
    """FR-REL-01: the classifier runs on *closed* buckets; the pass must not
    touch OPEN ones."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1, state="OPEN")
    mid = _message(conn, 1, 1)
    _membership(conn, bucket_id, mid, 1)
    classifier = ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])

    result = run_relevance_pass(conn, classifier)
    assert result.outcomes == ()
    assert classifier.requests == []
    assert _attempt_count(conn, bucket_id) == 0
    assert bucket_processing_state(conn, bucket_id) is BucketState.OPEN


def test_never_started_closed_bucket_is_classified(conn: sqlite3.Connection) -> None:
    bucket_id, members = _standard_scenario(conn)
    classifier = ScriptedClassifier([_decisions_from_members(members)])

    result = run_relevance_pass(conn, classifier)
    outcome = result.outcomes[0]
    assert outcome.status is RelevanceOutcome.CLASSIFIED
    assert outcome.decision_count == len(members)
    assert bucket_processing_state(conn, bucket_id) is BucketState.PROCESSED


def test_retryable_failed_gap_bucket_is_retried(conn: sqlite3.Connection) -> None:
    """docs/PHASE2_PLAN.md §12 coverage note: a CLOSED bucket whose attempt
    rows are all FAILED_RETRYABLE is in no processing_queue list but is
    still outstanding work — the pass discovers and retries it."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1)
    _membership(conn, bucket_id, mid, 1)
    conn.execute(
        "INSERT INTO processing_attempt (stage, bucket_id, started_at,"
        " finished_at, outcome, error)"
        " VALUES (?, ?, '2026-01-01T10:01:00+00:00', '2026-01-01T10:01:01+00:00',"
        "         'FAILED_RETRYABLE', 'owl outage')",
        (STAGE_RELEVANCE_CLASSIFICATION, bucket_id),
    )
    from backend.orchestration import processing_queue

    queue = processing_queue(conn)
    assert bucket_id not in queue.never_started
    assert bucket_id not in queue.awaiting_recovery

    classifier = ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])
    result = run_relevance_pass(conn, classifier)
    assert len(result.outcomes) == 1
    assert result.outcomes[0].status is RelevanceOutcome.CLASSIFIED
    assert _attempt_count(conn, bucket_id) == 2
    assert bucket_processing_state(conn, bucket_id) is BucketState.PROCESSED


def test_awaiting_recovery_bucket_resumes_after_outage(
    conn: sqlite3.Connection,
) -> None:
    """NFR-REL-05/AC-13: an AWAITING_RECOVERY bucket is reprocessed by the
    pass without a manual re-entry."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1, state="AWAITING_RECOVERY")
    mid = _message(conn, 1, 1)
    _membership(conn, bucket_id, mid, 1)
    classifier = ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])

    result = run_relevance_pass(conn, classifier)
    assert result.outcomes[0].status is RelevanceOutcome.CLASSIFIED
    assert bucket_processing_state(conn, bucket_id) is BucketState.PROCESSED


def test_failed_bucket_is_not_auto_attempted(conn: sqlite3.Connection) -> None:
    """NFR-REL-03: FAILED buckets return only by manual re-queue."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1, state="FAILED")
    mid = _message(conn, 1, 1)
    _membership(conn, bucket_id, mid, 1)
    classifier = ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])

    result = run_relevance_pass(conn, classifier)
    assert result.outcomes == ()
    assert classifier.requests == []
    assert _attempt_count(conn, bucket_id) == 0
    assert bucket_processing_state(conn, bucket_id) is BucketState.FAILED


def test_manual_requeue_moves_failed_to_awaiting(
    conn: sqlite3.Connection,
) -> None:
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1, state="FAILED")

    assert requeue_failed_bucket(conn, bucket_id) is True
    assert bucket_processing_state(conn, bucket_id) is BucketState.AWAITING_RECOVERY


def test_manual_requeue_is_idempotent_noop(
    conn: sqlite3.Connection,
) -> None:
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1, state="AWAITING_RECOVERY")

    assert requeue_failed_bucket(conn, bucket_id) is False
    assert bucket_processing_state(conn, bucket_id) is BucketState.AWAITING_RECOVERY


def test_succeeded_bucket_is_never_reprocessed(
    conn: sqlite3.Connection,
) -> None:
    """NFR-REL-01: retries are safe; a processed bucket gets no new attempts."""
    bucket_id, members = _standard_scenario(conn)
    classifier = ScriptedClassifier([_decisions_from_members(members)])
    run_relevance_pass(conn, classifier)
    first_count = _attempt_count(conn, bucket_id)

    second = ScriptedClassifier([_decisions_from_members(members)])
    result = run_relevance_pass(conn, second)
    assert result.outcomes == ()
    assert second.requests == []
    assert _attempt_count(conn, bucket_id) == first_count


def test_untransitioned_success_converges_to_processed(
    conn: sqlite3.Connection,
) -> None:
    """NFR-REL-04: a crash between the attempt commit and the lifecycle
    transition is recovered by the next pass — no new attempts, no loss."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1, state="AWAITING_RECOVERY")
    mid = _message(conn, 1, 1)
    _membership(conn, bucket_id, mid, 1)
    attempt = conn.execute(
        "INSERT INTO processing_attempt (stage, bucket_id, started_at, finished_at,"
        " outcome, model_id, model_version, config_version)"
        " VALUES (?, ?, '2026-01-01T10:01:00+00:00', '2026-01-01T10:01:01+00:00',"
        "         'SUCCEEDED', 'stub-model', 'v1', 'p1')",
        (STAGE_RELEVANCE_CLASSIFICATION, bucket_id),
    )
    conn.execute(
        "INSERT INTO relevance_decision (bucket_id, message_id, attempt_id, decision,"
        " model_id, model_version, prompt_version, decided_at)"
        " VALUES (?, ?, ?, 'RELEVANT', 'stub-model', 'v1', 'p1',"
        "         '2026-01-01T10:01:01+00:00')",
        (bucket_id, mid, attempt.lastrowid),
    )

    result = run_relevance_pass(conn, ScriptedClassifier([]))
    assert result.outcomes == ()
    assert bucket_processing_state(conn, bucket_id) is BucketState.PROCESSED
    assert _attempt_count(conn, bucket_id) == 1


def test_multiple_buckets_processed_in_deterministic_order(
    conn: sqlite3.Connection,
) -> None:
    """Discovery is deterministic: ascending bucket id, regardless of the
    order rows were inserted."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_b = _bucket(conn, 1)
    _bucket(conn, 1)  # bucket id > bucket_b creates the gap bucket later
    members: list[tuple[int, str]] = []
    for bucket_id in (bucket_b, bucket_b + 1):
        mid = _message(conn, 1, index=bucket_id, content="m")
        _membership(conn, bucket_id, mid, 1)
        members.append((mid, f"mid-1-{bucket_id}"))
    classifier = ScriptedClassifier(
        [_decisions_from_members(members[:1]), _decisions_from_members(members[1:])]
    )

    result = run_relevance_pass(conn, classifier)
    assert [outcome.bucket_id for outcome in result.outcomes] == [
        bucket_b,
        bucket_b + 1,
    ]
    assert all(
        outcome.status is RelevanceOutcome.CLASSIFIED for outcome in result.outcomes
    )


def test_decision_rows_are_attempt_scoped_and_prior_attempt_intact(
    conn: sqlite3.Connection,
) -> None:
    """NFR-REL-01/03: a retry creates a new attempt; the earlier attempt's
    rows stay untouched (no erase, no overwrite)."""
    bucket_id, members = _standard_scenario(conn)
    first = ScriptedClassifier([RuntimeError("transient outage")])
    run_relevance_pass(conn, first)
    first_attempt_rows = [
        dict(row) for row in _attempts(conn, bucket_id)
    ]

    second = ScriptedClassifier([_decisions_from_members(members)])
    result = run_relevance_pass(conn, second)
    assert result.outcomes[0].status is RelevanceOutcome.CLASSIFIED

    rows = _attempts(conn, bucket_id)
    assert len(rows) == 2
    assert dict(rows[0]) == first_attempt_rows[0]
    assert rows[0]["outcome"] == "FAILED_RETRYABLE"
    assert rows[1]["outcome"] == "SUCCEEDED"


def test_empty_workspace_pass_is_clean_noop(
    conn: sqlite3.Connection,
) -> None:
    classifier = ScriptedClassifier([])
    result = run_relevance_pass(conn, classifier)
    assert result.outcomes == ()
    assert result.finalized == ()
    assert classifier.requests == []


def test_unknown_bucket_in_isolation_fails_loud(
    conn: sqlite3.Connection,
) -> None:
    """The request builder refuses an absent bucket id (defence in depth)."""
    import backend.relevance as relevance

    with pytest.raises(UnknownBucketError):
        relevance._bucket_request(conn, 424242)


# --- Subtask D — lifecycle, retry budget, fallback ------------------------


def test_primary_fails_fallback_succeeds_both_recorded(
    conn: sqlite3.Connection,
) -> None:
    """NFR-REL-07: the fallback runs only after the primary failed; both
    attempts are recorded; the successful attempt's result is the effective
    one; the primary failure stays visible."""
    bucket_id, members = _standard_scenario(conn)
    primary = ScriptedClassifier([RuntimeError("primary down")])
    fallback = ScriptedClassifier([_decisions_from_members(members)])

    result = run_relevance_pass(conn, primary, fallback)
    outcome = result.outcomes[0]
    assert outcome.status is RelevanceOutcome.CLASSIFIED
    assert len(outcome.attempt_ids) == 2
    rows = _attempts(conn, bucket_id)
    assert [row["id"] for row in rows] == list(outcome.attempt_ids)
    assert rows[0]["outcome"] == "FAILED_RETRYABLE"
    assert rows[0]["error"] is not None
    assert rows[1]["outcome"] == "SUCCEEDED"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM relevance_decision WHERE bucket_id = ?",
        (bucket_id,),
    ).fetchone()["n"] == len(members)
    assert bucket_processing_state(conn, bucket_id) is BucketState.PROCESSED
    # Fallback stage token distinguishes the attempts (free text, no SRS
    # vocabulary per 0004).
    assert rows[0]["stage"] == STAGE_RELEVANCE_CLASSIFICATION
    assert rows[1]["stage"] == STAGE_RELEVANCE_FALLBACK_CLASSIFICATION


def test_both_fail_within_budget_marks_awaiting_recovery(
    conn: sqlite3.Connection,
) -> None:
    """Transient failure inside the retry budget: the bucket pauses in
    AWAITING_RECOVERY (CLOSED or AWAITING → the legal edge), never FAILED.
    Fallback runs after the primary; both attempts recorded; message flags
    set; nothing erased."""
    bucket_id, members = _standard_scenario(conn)
    primary = ScriptedClassifier([RuntimeError("primary down")])
    fallback = ScriptedClassifier([RuntimeError("fallback down")])

    result = run_relevance_pass(conn, primary, fallback)
    outcome = result.outcomes[0]
    assert outcome.status is RelevanceOutcome.RETRYING
    assert len(outcome.attempt_ids) == 2
    assert bucket_processing_state(conn, bucket_id) is BucketState.AWAITING_RECOVERY
    assert _attempt_count(conn, bucket_id) == 2
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM relevance_decision WHERE bucket_id = ?",
            (bucket_id,),
        ).fetchone()["n"]
        == 0
    )


def test_retry_budget_exhaustion_transitions_failed(
    conn: sqlite3.Connection,
) -> None:
    """A finite, deterministic budget (default 3 attempt rows): once the
    committed attempt count reaches it and the bucket is still outstanding,
    the pass stops trying and leaves the bucket FAILED for manual re-queue
    (NFR-REL-03); the attempts themselves stay FAILED_RETRYABLE records.
    FAILED_PERMANENT is reserved per the 0004 vocabulary and never written by
    this phase (documented owner-vetoable choice)."""
    bucket_id, members = _standard_scenario(conn)
    failing = ScriptedClassifier([RuntimeError("down")])

    run_relevance_pass(conn, failing)  # 1 attempt → AWAITING_RECOVERY
    assert bucket_processing_state(conn, bucket_id) is BucketState.AWAITING_RECOVERY
    run_relevance_pass(conn, failing)
    run_relevance_pass(conn, failing, failing)  # attempt 3 → EXHAUSTED → FAILED
    assert bucket_processing_state(conn, bucket_id) is BucketState.FAILED
    assert _attempt_count(conn, bucket_id) == 3

    # A FAILED bucket is never auto-attempted again (manual re-queue only).
    result = run_relevance_pass(conn, failing, failing)
    assert result.outcomes == ()
    assert _attempt_count(conn, bucket_id) == 3
    assert bucket_processing_state(conn, bucket_id) is BucketState.FAILED


def test_budget_refusal_for_backlog_bucket_transitions_failed(
    conn: sqlite3.Connection,
) -> None:
    """A CLOSED bucket whose committed attempts already consume the budget is
    moved to FAILED by its would-be retry pass — deterministically, with no
    new attempt rows."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1)
    _membership(conn, bucket_id, mid, 1)
    for _n in range(3):
        conn.execute(
            "INSERT INTO processing_attempt (stage, bucket_id, started_at,"
            " finished_at, outcome, error)"
            " VALUES (?, ?, '2026-01-01T10:01:00+00:00',"
            "         '2026-01-01T10:01:01+00:00', 'FAILED_RETRYABLE', 'err')",
            (STAGE_RELEVANCE_CLASSIFICATION, bucket_id),
        )
    classifier = ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])

    result = run_relevance_pass(conn, classifier)
    outcome = result.outcomes[0]
    assert outcome.status is RelevanceOutcome.EXHAUSTED
    assert outcome.attempt_ids == ()
    assert _attempt_count(conn, bucket_id) == 3
    assert bucket_processing_state(conn, bucket_id) is BucketState.FAILED


def test_fallback_budget_respected_inside_one_pass(
    conn: sqlite3.Connection,
) -> None:
    """The in-transaction budget guard also bounds the fallback attempt, so
    one pass cannot exceed the cap."""
    bucket_id, members = _standard_scenario(conn)
    primary = ScriptedClassifier([RuntimeError("down")])
    fallback = ScriptedClassifier([RuntimeError("still down")])

    run_relevance_pass(conn, primary, fallback)   # attempts 1, 2
    result = run_relevance_pass(conn, primary, fallback)
    # Attempt 3 created; the fallback attempt is refused (count already 3).
    outcome = result.outcomes[0]
    assert outcome.status is RelevanceOutcome.EXHAUSTED
    assert outcome.attempt_ids != () and len(outcome.attempt_ids) == 1
    assert _attempt_count(conn, bucket_id) == 3
    assert bucket_processing_state(conn, bucket_id) is BucketState.FAILED


def test_outage_recovery_full_cycle_ac13(
    conn: sqlite3.Connection,
) -> None:
    """AC-13/NFR-REL-05: messages sent during an outage are UNCLASSIFIED,
    processed automatically after recovery, none lost, none marked
    irrelevant."""
    bucket_id, members = _standard_scenario(conn, message_count=3)
    outage = ScriptedClassifier([ConnectionError("outage")])
    recovered = ScriptedClassifier([_decisions_from_members(members)])

    outage_result = run_relevance_pass(conn, outage)
    assert outage_result.outcomes[0].status is RelevanceOutcome.RETRYING
    assert bucket_processing_state(conn, bucket_id) is BucketState.AWAITING_RECOVERY

    recovery_result = run_relevance_pass(conn, recovered)
    outcome = recovery_result.outcomes[0]
    assert outcome.status is RelevanceOutcome.CLASSIFIED
    assert outcome.decision_count == len(members)
    assert _decisions(conn, bucket_id) != []
    assert all(
        row["decision"] == "RELEVANT" for row in _decisions(conn, bucket_id)
    )
    assert bucket_processing_state(conn, bucket_id) is BucketState.PROCESSED
    # Deferred-clearing boundary (audit F9): a later *successful*
    # classification still must not clear the flag — clearing is an owner
    # decision (docs/PHASE2_PLAN.md §13.6), and any regression there is a
    # boundary violation, not a cleanup.
    recovery_flags = conn.execute(
        "SELECT sm.flag_unclassified FROM source_message sm"
        " JOIN bucket_message bm ON bm.message_id = sm.id WHERE bm.bucket_id = ?",
        (bucket_id,),
    ).fetchall()
    assert all(int(row["flag_unclassified"]) == 1 for row in recovery_flags)


def test_interrupt_during_call_writes_nothing(
    conn: sqlite3.Connection,
) -> None:
    """An interrupt inside the classifier call is never recorded as an
    attempt outcome; nothing durable existed yet (transactionality of the
    pre-write design); the interrupt propagates unchanged."""
    bucket_id, _members = _standard_scenario(conn)
    classifier = ScriptedClassifier([KeyboardInterrupt("operator")])

    with pytest.raises(KeyboardInterrupt):
        run_relevance_pass(conn, classifier)
    assert _attempt_count(conn, bucket_id) == 0
    assert _decisions(conn, bucket_id) == []


def test_pass_is_idempotent_for_processed_and_open_states(
    conn: sqlite3.Connection,
) -> None:
    """A second pass on already-classified (and unclassified OPEN) buckets is
    a pure no-op."""
    bucket_id, members = _standard_scenario(conn)
    open_id = _bucket(conn, 1, state="OPEN")
    classifier = ScriptedClassifier([_decisions_from_members(members)])
    run_relevance_pass(conn, classifier)

    again = ScriptedClassifier([])
    result = run_relevance_pass(conn, again)
    assert again.requests == []
    assert result.outcomes == ()
    # Real checks (audit F8): the OPEN bucket stays untouched and
    # attempt-less; the processed bucket keeps exactly its one attempt.
    assert bucket_processing_state(conn, open_id) is BucketState.OPEN
    assert _attempt_count(conn, open_id) == 0
    assert _attempt_count(conn, bucket_id) == 1


# --- Subtask E — acceptance mapping --------------------------------------


def test_ac10_volunteer_and_unknown_messages_classification_only(
    conn: sqlite3.Connection,
) -> None:
    """AC-10/FR-PRP-02: volunteers and unknown senders are stored and
    classified; classification itself creates no proposal and no
    notification; nothing is delivered further."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    volunteer = _person(conn, 5, "Vikram", "3001")
    bucket_id = _bucket(conn, 1)
    mid_v = _message(conn, 1, 1, sender_person_id=volunteer, sender_discord_user_id="3001")
    mid_u = _message(conn, 1, 2, sender_person_id=None, sender_discord_user_id=None)
    _membership(conn, bucket_id, mid_v, 1)
    _membership(conn, bucket_id, mid_u, 1)
    response = RelevanceResponse(
        model_id="stub-model",
        model_version="v1",
        prompt_version="p1",
        decisions=(
            MessageDecision(message_id=mid_v, decision="RELEVANT"),
            MessageDecision(message_id=mid_u, decision="IRRELEVANT"),
        ),
    )
    classifier = ScriptedClassifier([response])

    result = run_relevance_pass(conn, classifier)
    assert result.outcomes[0].decision_count == 2
    stored_decisions = {
        int(row["message_id"]): row["decision"] for row in _decisions(conn, bucket_id)
    }
    assert stored_decisions == {mid_v: "RELEVANT", mid_u: "IRRELEVANT"}
    # No Phase 5 behaviour exists or fired.
    assert conn.execute("SELECT COUNT(*) AS n FROM proposal").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM notification").fetchone()["n"] == 0
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM notification_attempt").fetchone()["n"]
        == 0
    )


def test_ac12_failure_never_irrelevant_and_retry_creates_no_duplicates(
    conn: sqlite3.Connection,
) -> None:
    """AC-12: failures stay recorded failures; the source is kept; retries
    create no duplicate decisions."""
    bucket_id, members = _standard_scenario(conn)
    bad = RelevanceResponse(
        model_id="stub-model",
        model_version="v1",
        prompt_version="p1",
        decisions=tuple(
            MessageDecision(message_id=mid, decision="IRRELEVANT")
            for mid, _ in members[:-1]  # incomplete on purpose
        ),
    )
    classifier = ScriptedClassifier([bad, _decisions_from_members(members)])

    first = run_relevance_pass(conn, classifier)
    assert first.outcomes[0].status is RelevanceOutcome.RETRYING
    assert _decisions(conn, bucket_id) == []
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM relevance_decision WHERE bucket_id = ?",
        (bucket_id,),
    ).fetchone()["n"] == 0

    second = run_relevance_pass(conn, classifier)
    assert second.outcomes[0].status is RelevanceOutcome.CLASSIFIED
    decision_rows = _decisions(conn, bucket_id)
    ids = [int(row["message_id"]) for row in decision_rows]
    assert sorted(ids) == sorted(mid for mid, _ in members)
    assert _attempt_count(conn, bucket_id) == 2
    # Nothing was ever recorded as a decision stemming from a failure.
    for row in decision_rows:
        attempt = conn.execute(
            "SELECT outcome FROM processing_attempt WHERE id = ?", (row["attempt_id"],)
        ).fetchone()
        assert attempt["outcome"] == "SUCCEEDED"


def test_illegal_lifecycle_transition_never_bypassed(
    conn: sqlite3.Connection,
) -> None:
    """D: the pass only reaches states via the legal edge set; from FAILED it
    never auto-advances."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    failed_id = _bucket(conn, 1, state="FAILED")
    proc_id = _bucket(conn, 1, state="PROCESSED")
    classifier = ScriptedClassifier([])
    result = run_relevance_pass(conn, classifier)
    assert result.outcomes == ()
    assert bucket_processing_state(conn, failed_id) is BucketState.FAILED
    assert bucket_processing_state(conn, proc_id) is BucketState.PROCESSED


def test_locked_database_aborts_pass_loudly(
    tmp_path, conn: sqlite3.Connection
) -> None:  # noqa: ARG001
    """A non-bucket-specific failure (another writer holding the write lock)
    aborts the pass loudly — the raw error surfaces, never eaten and never
    reported as a successful pass (ingest P-2 systemic-abort pattern)."""
    file_conn = connect(tmp_path / "relevance_locked.db")
    apply_migrations(file_conn)
    _vertical(file_conn, 1, "Tech")
    _group(file_conn, 1, vertical_id=1)
    bucket_id = _bucket(file_conn, 1)
    mid = _message(file_conn, 1, 1)
    _membership(file_conn, bucket_id, mid, 1)
    other = connect(tmp_path / "relevance_locked.db")
    other.execute("BEGIN IMMEDIATE")
    try:
        # The lock hold aborts the pass loudly with the P-2-style systemic
        # error (durable prefix carried on the exception, original chained).
        with pytest.raises(RelevancePassError):
            run_relevance_pass(
                file_conn, ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])
            )
    finally:
        other.execute("ROLLBACK")
        other.close()
        file_conn.close()


def test_partial_success_persistence_rolls_back_the_whole_attempt(
    conn: sqlite3.Connection,
) -> None:
    """The attempt+decisions+latency+flag write is atomic: a systemic failure
    inside it (here: the latency table is gone) leaves NO partial attempt, NO
    decisions and NO lifecycle change — and aborts the pass loudly with a
    RelevancePassError, never a reported success."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1)
    _membership(conn, bucket_id, mid, 1)
    conn.execute("DROP TABLE latency_measurement")

    with pytest.raises(RelevancePassError):
        run_relevance_pass(
            conn, ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])
        )
    assert _attempt_count(conn, bucket_id) == 0
    assert _decisions(conn, bucket_id) == []
    assert bucket_processing_state(conn, bucket_id) is BucketState.CLOSED


def test_relevance_pass_error_carries_durable_prefix(
    conn: sqlite3.Connection,
) -> None:
    """A systemic failure aborts the pass while earlier buckets' committed,
    classified results stay durable and carried in the error's prefix."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    first_id = _bucket(conn, 1)
    mid_first = _message(conn, 1, 1)
    second_id = _bucket(conn, 1)
    mid_second = _message(conn, 1, 2)
    _membership(conn, first_id, mid_first, 1)
    _membership(conn, second_id, mid_second, 1)
    # Sabotage only the second bucket's attempt write via a fail trigger on
    # the row's actual id (the first bucket commits normally before the
    # abort; systemic failure, not record-specific, per the P-2 taxonomy).
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

    with pytest.raises(RelevancePassError) as excinfo:
        run_relevance_pass(
            conn,
            ScriptedClassifier(
                [
                    _decisions_from_members([(mid_first, "mid-1-1")]),
                    _decisions_from_members([(mid_second, "mid-1-2")]),
                ]
            ),
        )
    error = excinfo.value
    assert len(error.outcomes) == 1
    assert error.outcomes[0].bucket_id == first_id
    assert error.outcomes[0].status is RelevanceOutcome.CLASSIFIED
    assert bucket_processing_state(conn, first_id) is BucketState.PROCESSED
    assert _decisions(conn, first_id) != []
    assert _attempt_count(conn, second_id) == 0


def test_vocabulary_constant_is_exact(conn: sqlite3.Connection) -> None:
    """FR-REL-02 verbatim vocabulary surface check."""
    assert DECISION_VOCABULARY == ("RELEVANT", "IRRELEVANT")


def test_context_message_frozen_contract(conn: sqlite3.Connection) -> None:
    """The request/response dataclasses are immutable value objects."""
    entry = ContextMessage(
        message_id=1,
        sender_person_id=None,
        sender_discord_user_id=None,
        sender_name=None,
        event_timestamp=None,
        reply_reference=None,
        masked_content=None,
        mention_handles=(),
    )
    with pytest.raises(FrozenInstanceError):
        entry.message_id = 2  # type: ignore[misc]


def test_response_contract_shape(conn: sqlite3.Connection) -> None:
    """The response is a value object; the backend treats its fields as
    untrusted and validates coverage/vocabulary/provenance itself."""
    response = MessageDecision(message_id=1, decision="RELEVANT")
    with pytest.raises(FrozenInstanceError):
        response.decision = "IRRELEVANT"  # type: ignore[misc]


# --- Consolidated audit correction pass (2026-10-21) ----------------------
# Findings 1, 5, 6, 9, 10, 11, 12 from the independent Phase 4 audit
# (verdict: PASS WITH NON-BLOCKING FINDINGS); 2/3/4 are recorded as documents.
# Tests are written first; the four behaviour changes (first-writer-wins
# guard, failure-convergence guard, budget-argument validation, error-text
# redaction) must observe RED before their implementation.


def test_pass_rejects_non_positive_budget_loudly(
    conn: sqlite3.Connection,
) -> None:
    """Audit F5: ``max_attempts_per_bucket <= 0`` must fail loudly before any
    state change — a caller typo must not mass-fail the whole backlog."""
    bucket_id, members = _standard_scenario(conn)
    with pytest.raises(ValueError):
        run_relevance_pass(
            conn,
            ScriptedClassifier([_decisions_from_members(members)]),
            max_attempts_per_bucket=0,
        )
    assert _attempt_count(conn, bucket_id) == 0
    assert bucket_processing_state(conn, bucket_id) is BucketState.CLOSED


def test_failure_error_text_is_redacted_and_capped(
    conn: sqlite3.Connection,
) -> None:
    """Audit F6: adapter error strings are masked (the error channel is a
    review surface; NFR-SEC-04 reach) and length-capped before storage."""
    bucket_id, _members = _standard_scenario(conn)
    classifier = ScriptedClassifier(
        [RuntimeError("phone 98765 43210 in detail " + "x" * 900)]
    )
    result = run_relevance_pass(conn, classifier)
    row = conn.execute(
        "SELECT error FROM processing_attempt WHERE id = ?",
        (result.outcomes[0].attempt_ids[0],),
    ).fetchone()
    error = str(row["error"])
    assert "[PHONE]" in error
    assert "98765 43210" not in error
    assert len(error) <= 500


def test_concurrent_success_is_first_writer_wins(tmp_path) -> None:
    """Audit F1: a competing writer's committed SUCCEEDED attempt precedes
    ours — this pass must record no second SUCCEEDED attempt, no duplicate
    decisions, no lifecycle change of its own, and its result must carry no
    outcome for the bucket; the competing success converges to PROCESSED on
    the next pass."""
    file_conn = connect(tmp_path / "p4_race.db")
    apply_migrations(file_conn)
    _vertical(file_conn, 1, "Tech")
    _group(file_conn, 1, vertical_id=1)
    bucket_id = _bucket(file_conn, 1)
    members: list[tuple[int, str]] = []
    for index in (1, 2):
        mid = _message(file_conn, 1, index)
        _membership(file_conn, bucket_id, mid, 1)
        members.append((mid, f"mid-1-{index}"))

    class Racer:
        """Commits its SUCCEEDED attempt (and decisions) inside classify()."""

        def __init__(self) -> None:
            self.own = connect(tmp_path / "p4_race.db")

        def classify(self, request: RelevanceRequest) -> RelevanceResponse:
            with transaction(self.own):
                cursor = self.own.execute(
                    "INSERT INTO processing_attempt"
                    " (stage, bucket_id, started_at, finished_at, outcome,"
                    "  model_id, model_version, config_version)"
                    " VALUES (?, ?, 't', 't', 'SUCCEEDED', 'other', 'v', 'p')",
                    (STAGE_RELEVANCE_CLASSIFICATION, request.bucket_id),
                )
                for entry in request.messages:
                    self.own.execute(
                        "INSERT INTO relevance_decision"
                        " (bucket_id, message_id, attempt_id, decision,"
                        "  model_id, model_version, prompt_version, decided_at)"
                        " VALUES (?, ?, ?, 'RELEVANT', 'other', 'v', 'p', 't')",
                        (request.bucket_id, entry.message_id, cursor.lastrowid),
                    )
            return _decisions_from_members(members)

    racer = Racer()
    try:
        result = run_relevance_pass(file_conn, racer)
        assert result.outcomes == ()
        assert _attempt_count(file_conn, bucket_id) == 1
        decisions = _decisions(file_conn, bucket_id)
        assert len(decisions) == 2
        assert all(row["model_id"] == "other" for row in decisions)
        assert bucket_processing_state(file_conn, bucket_id) is BucketState.CLOSED

        # The next pass converges the competing writer's committed success
        # without new attempts (crash-convergence but for races).
        runner = run_relevance_pass(file_conn, ScriptedClassifier([]))
        assert runner.outcomes == ()
        assert runner.finalized == (bucket_id,)
        assert bucket_processing_state(file_conn, bucket_id) is BucketState.PROCESSED
        assert _attempt_count(file_conn, bucket_id) == 1
    finally:
        racer.own.close()
        file_conn.close()


def test_failure_convergence_never_marks_failed_over_a_success(
    tmp_path,
) -> None:
    """Audit F1 (interleaving half): our attempt fails while a competing
    writer commits a SUCCEEDED attempt first — the bucket must not be
    marked FAILED over that success; the next pass finalizes it instead.

    The competing success must land after this pass's finalize sweep (it
    would otherwise converge deterministically before discovery), so the
    competitor commits inside ``classify()`` via its own connection and
    then returns an invalid response.
    """
    file_conn = connect(tmp_path / "p4_race_fail.db")
    apply_migrations(file_conn)
    t_conn = None
    try:
        _vertical(file_conn, 1, "Tech")
        _group(file_conn, 1, vertical_id=1)
        bucket_id = _bucket(file_conn, 1)
        members: list[tuple[int, str]] = []
        for index in (1, 2):
            mid = _message(file_conn, 1, index)
            _membership(file_conn, bucket_id, mid, 1)
            members.append((mid, f"mid-1-{index}"))

        class Racer:
            """Commits its SUCCEEDED attempt inside classify(), then hands
            back an invalid response so our own attempt fails."""

            def __init__(self) -> None:
                self.own = connect(tmp_path / "p4_race_fail.db")

            def classify(self, request: RelevanceRequest) -> RelevanceResponse:
                with transaction(self.own):
                    cursor = self.own.execute(
                        "INSERT INTO processing_attempt"
                        " (stage, bucket_id, started_at, finished_at, outcome,"
                        "  model_id, model_version, config_version)"
                        " VALUES (?, ?, 't', 't', 'SUCCEEDED', 'other', 'v', 'p')",
                        (STAGE_RELEVANCE_CLASSIFICATION, request.bucket_id),
                    )
                    for entry in request.messages:
                        self.own.execute(
                            "INSERT INTO relevance_decision"
                            " (bucket_id, message_id, attempt_id, decision,"
                            "  model_id, model_version, prompt_version, decided_at)"
                            " VALUES (?, ?, ?, 'RELEVANT', 'other', 'v', 'p', 't')",
                            (request.bucket_id, entry.message_id, cursor.lastrowid),
                        )
                incomplete = RelevanceResponse(
                    model_id="stub-model",
                    model_version="v1",
                    prompt_version="p1",
                    decisions=(
                        MessageDecision(
                            message_id=members[0][0],
                            decision="RELEVANT",
                        ),
                    ),
                )
                return incomplete  # incomplete: second member uncovered

        racer = Racer()
        result = run_relevance_pass(file_conn, racer)
        t_conn = racer.own
        assert result.outcomes == ()
        assert _attempt_count(file_conn, bucket_id) == 2  # other writer's + our failed one
        our_row = file_conn.execute(
            "SELECT outcome, model_id FROM processing_attempt WHERE id = 2"
        ).fetchone()
        assert str(our_row["outcome"]) == "FAILED_RETRYABLE"
        assert our_row["model_id"] is None
        assert bucket_processing_state(file_conn, bucket_id) is BucketState.CLOSED

        second = run_relevance_pass(
            file_conn, ScriptedClassifier([_decisions_from_members(members)])
        )
        assert second.outcomes == ()
        assert second.finalized == (bucket_id,)
        assert bucket_processing_state(file_conn, bucket_id) is BucketState.PROCESSED
        assert _attempt_count(file_conn, bucket_id) == 2
    finally:
        if t_conn is not None:
            t_conn.close()
        file_conn.close()


@pytest.mark.parametrize("state", ["CLOSED", "PROCESSED"])
def test_manual_requeue_rejects_non_failed_states(
    conn: sqlite3.Connection, state: str
) -> None:
    """Audit F10: the manual re-queue must never silently "heal" a healthy
    bucket — rejection is loud for anything that is not FAILED/AWAITING."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1, state=state)
    with pytest.raises(InvalidBucketTransitionError):
        requeue_failed_bucket(conn, bucket_id)
    assert bucket_processing_state(conn, bucket_id) is BucketState(state)


def test_non_iterable_decisions_rejected(conn: sqlite3.Connection) -> None:
    """Audit F11: a structurally wrong response body is an execution failure
    recorded as FAILED_RETRYABLE — never an unclassified pass error, never
    an IRRELEVANT."""
    bucket_id, _members = _standard_scenario(conn)
    response = RelevanceResponse(
        model_id="stub-model",
        model_version="v1",
        prompt_version="p1",
        decisions=None,  # type: ignore[arg-type]
    )
    result = run_relevance_pass(conn, ScriptedClassifier([response]))
    outcome = result.outcomes[0]
    assert outcome.status is RelevanceOutcome.RETRYING
    assert _decisions(conn, bucket_id) == []
    row = conn.execute(
        "SELECT outcome FROM processing_attempt WHERE id = ?",
        (outcome.attempt_ids[0],),
    ).fetchone()
    assert row["outcome"] == "FAILED_RETRYABLE"


def test_decision_items_without_contract_attributes_rejected(
    conn: sqlite3.Connection,
) -> None:
    """Audit F11: an item lacking the contract attributes fails whole, with
    no decisions recorded for that attempt."""
    bucket_id, _members = _standard_scenario(conn)
    response = RelevanceResponse(
        model_id="stub-model",
        model_version="v1",
        prompt_version="p1",
        decisions=("junk",),  # type: ignore[list-item]
    )
    result = run_relevance_pass(conn, ScriptedClassifier([response]))
    assert result.outcomes[0].status is RelevanceOutcome.RETRYING
    assert _decisions(conn, bucket_id) == []


def test_systemexit_propagates_with_nothing_written(
    conn: sqlite3.Connection,
) -> None:
    """Audit F11: SystemExit (like KeyboardInterrupt) is never an attempt
    outcome; nothing partial is written and the interrupt surfaces raw."""
    bucket_id, _members = _standard_scenario(conn)
    with pytest.raises(SystemExit):
        run_relevance_pass(conn, ScriptedClassifier([SystemExit(3)]))
    assert _attempt_count(conn, bucket_id) == 0
    assert _decisions(conn, bucket_id) == []


def test_interrupt_inside_persistence_transaction_rolls_back(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit F12: an interrupt landing inside the persistence transaction
    (between budget check and COMMIT) must roll back the whole in-flight
    attempt — zero rows, lifecycle state unchanged, interrupt raw."""
    bucket_id, members = _standard_scenario(conn)
    real_utc_now = relevance._utc_now
    calls = {"n": 0}

    def sequenced_utc_now() -> str:
        calls["n"] += 1
        if calls["n"] == 2:  # 1: started_at; 2: finished_at inside the txn
            raise KeyboardInterrupt("operator stop at write time")
        return real_utc_now()

    monkeypatch.setattr(relevance, "_utc_now", sequenced_utc_now)
    with pytest.raises(KeyboardInterrupt):
        run_relevance_pass(conn, ScriptedClassifier([_decisions_from_members(members)]))
    assert _attempt_count(conn, bucket_id) == 0
    assert _decisions(conn, bucket_id) == []
    assert bucket_processing_state(conn, bucket_id) is BucketState.CLOSED


def test_budget_counts_only_relevance_stage_attempts(
    conn: sqlite3.Connection,
) -> None:
    """Audit F2 (stage-scoped budget): non-relevance attempt rows for the
    same bucket must not consume the relevance retry budget. Unreachable
    today (relevance is the table's only writer) — a forward-compat guard."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    for _n in range(3):
        conn.execute(
            "INSERT INTO processing_attempt (stage, bucket_id, started_at,"
            " finished_at, outcome, error)"
            " VALUES ('FUTURE_STAGE', ?, 't', 't', 'FAILED_RETRYABLE', 'e')",
            (bucket_id,),
        )
    mid = _message(conn, 1, 1)
    _membership(conn, bucket_id, mid, 1)
    response = _decisions_from_members([(mid, "mid-1-1")])

    result = run_relevance_pass(conn, ScriptedClassifier([response]))
    # Without the stage filter the pre-check would see 3 >= 3 attempts and
    # refuse (EXHAUSTED -> FAILED); scoped, the first relevance attempt
    # simply runs and succeeds.
    assert result.outcomes[0].status is RelevanceOutcome.CLASSIFIED
    assert _attempt_count(conn, bucket_id) == 4
    assert bucket_processing_state(conn, bucket_id) is BucketState.PROCESSED


# --- F3/F4 approved correction batch (2026-10-21) -------------------------
# F3: durable canonical masked-input snapshot per committed adapter-executing
#     attempt (NFR-AUD-02, §13.10(a)); F4: terminal-failure discoverability,
#     convergence to FAILED via existing legal edges, explicit mixed-outcome
#     policy (retryable dominates), and validated per-bucket budget overrides
#     for authorized manual recovery (NFR-REL-02/03/04, §13.10(b)).
# Tests are written first: they must observe RED before the module and
# migration changes land.


def _snapshot_rows(
    conn: sqlite3.Connection, attempt_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM relevance_attempt_input WHERE attempt_id = ?", (attempt_id,)
    ).fetchall()


# F3 --- canonical masked-input snapshot ------------------------------------


def test_snapshot_persists_canonical_masked_input_on_success(
    conn: sqlite3.Connection,
) -> None:
    """F3/NFR-AUD-02: a successful attempt stores the canonical serialization
    of the exact request object the adapter received, plus the ruleset/policy
    version identities and an integrity hash of the artifact."""
    bucket_id, members = _standard_scenario(conn)
    classifier = ScriptedClassifier([_decisions_from_members(members)])
    result = run_relevance_pass(conn, classifier)
    attempt_id = result.outcomes[0].attempt_ids[0]

    rows = _snapshot_rows(conn, attempt_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["masking_ruleset_version"] == MASKING_RULESET_VERSION
    assert row["policy_version"] == CLASSIFICATION_POLICY_VERSION
    expected = relevance._serialize_masked_request(classifier.requests[0])
    assert row["masked_request"] == expected
    assert row["request_sha256"] == relevance._request_sha256(expected)
    assert str(row["captured_at"]).strip() != ""

    stored = json.loads(str(row["masked_request"]))
    assert stored["bucket_id"] == bucket_id
    assert stored["instructions"] == CLASSIFICATION_POLICY
    assert [entry["message_id"] for entry in stored["messages"]] == [
        member_id for member_id, _ in members
    ]


def test_snapshot_persists_canonical_masked_input_on_failure(
    conn: sqlite3.Connection,
) -> None:
    """F3: execution failures also executed an adapter call over a real
    input — their committed attempt keeps the same durable artifact."""
    bucket_id, _members = _standard_scenario(conn)
    classifier = ScriptedClassifier([RuntimeError("model unavailable")])
    result = run_relevance_pass(conn, classifier)
    attempt_id = result.outcomes[0].attempt_ids[0]

    assert result.outcomes[0].status is RelevanceOutcome.RETRYING
    rows = _snapshot_rows(conn, attempt_id)
    assert len(rows) == 1
    stored = json.loads(str(rows[0]["masked_request"]))
    assert stored["bucket_id"] == bucket_id


def test_snapshot_persists_for_primary_and_fallback_attempts(
    conn: sqlite3.Connection,
) -> None:
    """F3: every committed adapter-executing attempt carries its own artifact,
    so primary and fallback inputs are distinguishable per attempt."""
    bucket_id, members = _standard_scenario(conn)
    primary = ScriptedClassifier([RuntimeError("primary outage")])
    fallback = ScriptedClassifier([_decisions_from_members(members)])
    result = run_relevance_pass(conn, primary, fallback)
    attempt_ids = result.outcomes[0].attempt_ids
    assert len(attempt_ids) == 2

    stages = {
        str(row["outcome"]): str(row["stage"])
        for row in conn.execute(
            "SELECT id, stage, outcome FROM processing_attempt WHERE id IN (?, ?)",
            attempt_ids,
        ).fetchall()
    }
    assert set(stages.values()) == {
        STAGE_RELEVANCE_CLASSIFICATION,
        STAGE_RELEVANCE_FALLBACK_CLASSIFICATION,
    }
    assert len(_snapshot_rows(conn, attempt_ids[0])) == 1
    assert len(_snapshot_rows(conn, attempt_ids[1])) == 1
    assert relevance._serialize_masked_request(
        primary.requests[0]
    ) == _snapshot_rows(conn, attempt_ids[0])[0]["masked_request"]
    assert relevance._serialize_masked_request(
        fallback.requests[0]
    ) == _snapshot_rows(conn, attempt_ids[1])[0]["masked_request"]


def test_no_unmasked_sensitive_input_in_snapshot(
    conn: sqlite3.Connection,
) -> None:
    """F3/NFR-SEC-04 alignment: the artifact is the *masked* request — raw
    personal data never enters the snapshot; what the adapter received is
    exactly what is stored."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    sensitive = (
        "call 98765 43210 or mail me ritesh.k@example.org, aadhaar"
        " 8234 5678 9012, pan ABCDE1234F, roll B25CS1044"
    )
    mid = _message(conn, 1, 1, content=sensitive)
    _membership(conn, bucket_id, mid, 1)

    classifier = ScriptedClassifier([_decisions_from_members([(mid, "mid-1-1")])])
    result = run_relevance_pass(conn, classifier)
    attempt_id = result.outcomes[0].attempt_ids[0]
    masked_request = str(_snapshot_rows(conn, attempt_id)[0]["masked_request"])
    assert "[PHONE]" in masked_request
    for raw in (
        "98765 43210",
        "ritesh.k@example.org",
        "8234 5678 9012",
        "ABCDE1234F",
        "B25CS1044",
    ):
        assert raw not in masked_request
    # self-consistency: the artifact equals what the adapter actually saw
    assert masked_request == relevance._serialize_masked_request(classifier.requests[0])


def test_snapshot_is_stable_when_masking_rules_change_later(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3 core guarantee: a later masking-rule change cannot alter the audit
    interpretation of an earlier attempt — the stored artifact is captured at
    write time and never re-derived."""
    bucket_a, members_a = _standard_scenario(conn)
    _person_id = _person(conn, 1, "Ravi", "discord-999")
    mid_b = _message(conn, 1, 7, sender_person_id=_person_id)
    bucket_b = _bucket(conn, 1)
    _membership(conn, bucket_b, mid_b, 1)
    members_b = [(mid_b, "mid-1-7")]

    first = run_relevance_pass(
        conn,
        ScriptedClassifier(
            [
                _decisions_from_members(members_a),
                _decisions_from_members(members_b),
            ]
        ),
    )
    assert [outcome.bucket_id for outcome in first.outcomes] == [
        bucket_a,
        bucket_b,
    ]
    attempt_a, _attempt_b = (
        first.outcomes[0].attempt_ids[0],
        first.outcomes[1].attempt_ids[0],
    )
    artifact_a_before = _snapshot_rows(conn, attempt_a)[0]["masked_request"]
    hash_a_before = _snapshot_rows(conn, attempt_a)[0]["request_sha256"]

    # A future ruleset edit: NEW attempts would mask differently…
    monkeypatch.setattr(relevance, "mask_pii", lambda text: "MASKED-DIFFERENTLY")
    mid_c = _message(conn, 1, 9)
    bucket_c = _bucket(conn, 1)
    _membership(conn, bucket_c, mid_c, 1)
    second = run_relevance_pass(
        conn,
        ScriptedClassifier([_decisions_from_members([(mid_c, "mid-1-9")])]),
    )
    attempt_c = second.outcomes[0].attempt_ids[0]
    artifact_c = _snapshot_rows(conn, attempt_c)[0]["masked_request"]
    assert '"masked_content":"MASKED-DIFFERENTLY"' in artifact_c

    # …while the earlier snapshots are byte-stable, hash included.
    assert _snapshot_rows(conn, attempt_a)[0]["masked_request"] == artifact_a_before
    assert _snapshot_rows(conn, attempt_a)[0]["request_sha256"] == hash_a_before


def test_snapshot_absent_without_an_attempt_row(
    conn: sqlite3.Connection,
) -> None:
    """F3 invariant: a snapshot exists exactly where a commit-accepted attempt
    exists — budget-refused paths create neither."""
    bucket_id, members = _standard_scenario(conn)
    for _n in range(3):
        conn.execute(
            "INSERT INTO processing_attempt (stage, bucket_id, started_at,"
            " finished_at, outcome, error)"
            " VALUES (?, ?, '2026-01-01T10:01:00+00:00',"
            "         '2026-01-01T10:01:01+00:00', 'FAILED_RETRYABLE', 'err')",
            (STAGE_RELEVANCE_CLASSIFICATION, bucket_id),
        )
    result = run_relevance_pass(
        conn, ScriptedClassifier([_decisions_from_members(members)])
    )
    assert result.outcomes[0].status is RelevanceOutcome.EXHAUSTED
    assert _attempt_count(conn, bucket_id) == 3
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM relevance_attempt_input"
        " WHERE attempt_id IN"
        " (SELECT id FROM processing_attempt WHERE bucket_id = ?)",
        (bucket_id,),
    ).fetchone()["n"] == 0


def test_snapshot_written_for_empty_members_bucket(
    conn: sqlite3.Connection,
) -> None:
    """F3: an empty-members bucket's valid zero-decision attempt still keeps
    its artifact (missing staged as missing, DR-04)."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    _bucket(conn, 1)
    result = run_relevance_pass(conn, ScriptedClassifier([_decisions_from_members([])]))
    attempt_id = result.outcomes[0].attempt_ids[0]
    rows = _snapshot_rows(conn, attempt_id)
    assert len(rows) == 1
    stored = json.loads(str(rows[0]["masked_request"]))
    assert stored["messages"] == []


# F4 --- terminal-failure discoverability, convergence and recovery ---------


def _seed_attempt(
    conn: sqlite3.Connection,
    bucket_id: int,
    outcome: str,
    *,
    stage: str = STAGE_RELEVANCE_CLASSIFICATION,
) -> int:
    cursor = conn.execute(
        "INSERT INTO processing_attempt (stage, bucket_id, started_at,"
        " finished_at, outcome, error)"
        " VALUES (?, ?, '2026-01-01T10:01:00+00:00',"
        "         '2026-01-01T10:01:01+00:00', ?, 'err')",
        (stage, bucket_id, outcome),
    )
    return int(cursor.lastrowid or 0)


def test_terminal_only_closed_bucket_converges_to_failed(
    conn: sqlite3.Connection,
) -> None:
    """F4/NFR-REL-02/04: a CLOSED bucket holding only FAILED_PERMANENT
    attempts is never auto-retried; it converges to FAILED (existing legal
    edge) so the NFR-REL-03 manual path can act on it, and it disappears
    visibly from the stalled set."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    _seed_attempt(conn, bucket_id, "FAILED_PERMANENT")

    assert stalled_terminal_buckets(conn) == (bucket_id,)
    classifier = ScriptedClassifier([])  # must never be called
    result = run_relevance_pass(conn, classifier)
    assert classifier.requests == []
    assert result.outcomes == ()
    assert result.terminal_failed == (bucket_id,)
    assert bucket_processing_state(conn, bucket_id) is BucketState.FAILED
    assert bucket_id in processing_queue(conn).failed
    assert stalled_terminal_buckets(conn) == ()

    # FAILED is not silently rediscovered by later passes either way.
    again = run_relevance_pass(conn, ScriptedClassifier([]))
    assert again.outcomes == ()
    assert again.terminal_failed == ()
    assert _attempt_count(conn, bucket_id) == 1


def test_requeued_terminal_bucket_is_recovery_work(
    conn: sqlite3.Connection,
) -> None:
    """F4 refinement conflict resolved (recorded in §13.11): convergence is
    CLOSED-only. An owner-requeued AWAITING bucket with only-permanent
    attempts is authorized recovery work — the pass must not sweep it back
    to FAILED before recovery can attempt; it classifies under the ordinary
    budget after the owner's explicit requeue."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    bucket_id = _bucket(conn, 1)
    mid = _message(conn, 1, 1)
    _membership(conn, bucket_id, mid, 1)
    _seed_attempt(conn, bucket_id, "FAILED_PERMANENT")
    assert stalled_terminal_buckets(conn) == (bucket_id,)

    classifier = ScriptedClassifier([])  # must never be called pre-requeue
    result = run_relevance_pass(conn, classifier)
    assert classifier.requests == []
    assert result.terminal_failed == (bucket_id,)
    assert bucket_processing_state(conn, bucket_id) is BucketState.FAILED

    # Owner authorization: the requeue opens the recovery queue.
    assert requeue_failed_bucket(conn, bucket_id) is True
    member_ids = _message_ids(conn, bucket_id)
    assert len(member_ids) == 1
    response = _decisions_from_members([(member_ids[0], "mid-1-1")])
    recovered_classifier = ScriptedClassifier([response])
    recovered = run_relevance_pass(conn, recovered_classifier)
    # The sweep did not undo the requeue: exactly one classification call.
    assert recovered_classifier.requests
    assert recovered.terminal_failed == ()
    assert recovered.outcomes[0].status is RelevanceOutcome.CLASSIFIED
    assert bucket_processing_state(conn, bucket_id) is BucketState.PROCESSED
    assert _attempt_count(conn, bucket_id) == 2  # permanent + recovery attempt


def test_mixed_retryable_and_permanent_stay_retriable(
    conn: sqlite3.Connection,
) -> None:
    """F4 explicit mixed-outcome policy (owner-approved): a bucket is
    automatically retriable iff at least one FAILED_RETRYABLE exists and no
    success; FAILED_PERMANENT rows neither block nor trigger."""
    bucket_id, members = _standard_scenario(conn)
    _seed_attempt(conn, bucket_id, "FAILED_RETRYABLE")
    _seed_attempt(conn, bucket_id, "FAILED_PERMANENT")

    result = run_relevance_pass(
        conn, ScriptedClassifier([_decisions_from_members(members)])
    )
    assert result.outcomes[0].status is RelevanceOutcome.CLASSIFIED
    assert result.terminal_failed == ()
    assert bucket_processing_state(conn, bucket_id) is BucketState.PROCESSED
    assert _attempt_count(conn, bucket_id) == 3


def test_prior_success_excluded_from_terminal_convergence(
    conn: sqlite3.Connection,
) -> None:
    """F4: a success dominates — finalize converges the bucket to PROCESSED;
    terminal convergence contributes nothing."""
    bucket_id, _members = _standard_scenario(conn)
    _seed_attempt(conn, bucket_id, "SUCCEEDED")
    _seed_attempt(conn, bucket_id, "FAILED_PERMANENT")

    result = run_relevance_pass(conn, ScriptedClassifier([]))
    assert result.outcomes == ()
    assert result.finalized == (bucket_id,)
    assert result.terminal_failed == ()
    assert bucket_processing_state(conn, bucket_id) is BucketState.PROCESSED


def _exhaust_to_failed(
    conn: sqlite3.Connection, bucket_id: int, members: list[tuple[int, str]]
) -> None:
    failing = ScriptedClassifier([RuntimeError("outage")])
    run_relevance_pass(conn, failing)
    run_relevance_pass(conn, failing)
    run_relevance_pass(conn, failing, failing)
    assert bucket_processing_state(conn, bucket_id) is BucketState.FAILED
    assert _attempt_count(conn, bucket_id) == 3


def test_recovery_without_override_reports_exhaustion_honestly(
    conn: sqlite3.Connection,
) -> None:
    """F4/R3: an authorized requeue of budget-exhausted work cannot silently
    'resume' under the unchanged budget — the recovery attempt is refused and
    reported EXHAUSTED, never a fake success, and the bucket returns to
    FAILED (the operator-visible disposition)."""
    bucket_id, members = _standard_scenario(conn)
    _exhaust_to_failed(conn, bucket_id, members)
    assert requeue_failed_bucket(conn, bucket_id) is True
    assert bucket_processing_state(conn, bucket_id) is BucketState.AWAITING_RECOVERY

    result = run_relevance_pass(
        conn, ScriptedClassifier([_decisions_from_members(members)])
    )
    assert result.outcomes[0].status is RelevanceOutcome.EXHAUSTED
    assert result.outcomes[0].attempt_ids == ()
    assert _attempt_count(conn, bucket_id) == 3
    assert bucket_processing_state(conn, bucket_id) is BucketState.FAILED


def test_scoped_budget_override_lets_requeued_bucket_execute(
    conn: sqlite3.Connection,
) -> None:
    """F4/R3: a validated per-bucket override (the bucket's raised total cap)
    lets authorized recovery execute real new attempts — scoped, without
    touching history or other buckets' budgets."""
    bucket_id, members = _standard_scenario(conn)
    _exhaust_to_failed(conn, bucket_id, members)
    prior_rows = conn.execute(
        "SELECT id, stage, bucket_id, outcome, error, model_id, model_version,"
        " config_version, started_at, finished_at FROM processing_attempt"
        " WHERE bucket_id = ? ORDER BY id",
        (bucket_id,),
    ).fetchall()
    assert requeue_failed_bucket(conn, bucket_id) is True

    # The override is the raised TOTAL cap: it must exceed the accumulated
    # attempts, or the refusal happens before any adapter call.
    recovery = run_relevance_pass(
        conn,
        ScriptedClassifier([_decisions_from_members(members)]),
        budget_overrides={bucket_id: 5},
    )
    assert recovery.outcomes[0].status is RelevanceOutcome.CLASSIFIED
    assert len(recovery.outcomes[0].attempt_ids) == 1
    assert _attempt_count(conn, bucket_id) == 4
    assert bucket_processing_state(conn, bucket_id) is BucketState.PROCESSED

    # History intact: prior attempt rows byte-identical; new rows appended.
    later_rows = conn.execute(
        "SELECT id, stage, bucket_id, outcome, error, model_id, model_version,"
        " config_version, started_at, finished_at FROM processing_attempt"
        " WHERE bucket_id = ? ORDER BY id",
        (bucket_id,),
    ).fetchall()
    assert [tuple(row) for row in prior_rows] == [tuple(row) for row in later_rows[:3]]
    assert len(later_rows) == 4
    # F3 rides along: the recovered attempt has its snapshot.
    assert len(_snapshot_rows(conn, recovery.outcomes[0].attempt_ids[0])) == 1


def test_budget_override_validation_fails_loud(
    conn: sqlite3.Connection,
) -> None:
    """F4/R3: invalid override values raise before any state change."""
    bucket_id, members = _standard_scenario(conn)
    good = ScriptedClassifier([_decisions_from_members(members)])
    for bad_value in (0, -1, True, "2"):
        with pytest.raises(ValueError):
            run_relevance_pass(
                conn, good, budget_overrides={bucket_id: bad_value}  # type: ignore[dict-item]
            )
    assert _attempt_count(conn, bucket_id) == 0
    assert bucket_processing_state(conn, bucket_id) is BucketState.CLOSED


def test_budget_override_key_validation_fails_loud(
    conn: sqlite3.Connection,
) -> None:
    """Audit F3 (correction pass): non-int override keys are refused loudly
    instead of silently matching nothing (symmetry with value validation)."""
    bucket_id, members = _standard_scenario(conn)
    good = ScriptedClassifier([_decisions_from_members(members)])
    with pytest.raises(ValueError):
        run_relevance_pass(
            conn, good, budget_overrides={"not-an-id": 5}  # type: ignore[dict-item]
        )
    assert _attempt_count(conn, bucket_id) == 0
    assert bucket_processing_state(conn, bucket_id) is BucketState.CLOSED


def test_override_is_per_bucket(
    conn: sqlite3.Connection,
) -> None:
    """F4/R3: overrides are scoped — an unrelated bucket recovers nothing and
    follows the ordinary default budget."""
    bucket_a, members_a = _standard_scenario(conn)
    _group(conn, 2, vertical_id=1)  # group only: no bucket in discovery yet
    _exhaust_to_failed(conn, bucket_a, members_a)
    assert requeue_failed_bucket(conn, bucket_a) is True

    # B is created only after A's exhaust passes: any discoverable bucket is
    # processed by every pass, so coexisting earlier would make B partake in
    # A's failures (and its own budget math) before the recovery pass.
    bucket_b = _bucket(conn, 2)
    mid_b = _message(conn, 2, 1)
    _membership(conn, bucket_b, mid_b, 2)
    for _n in range(2):
        _seed_attempt(conn, bucket_b, "FAILED_RETRYABLE")

    result = run_relevance_pass(
        conn,
        ScriptedClassifier([_decisions_from_members(members_a)]),
        budget_overrides={bucket_a: 5},
    )
    statuses = {outcome.bucket_id: outcome.status for outcome in result.outcomes}
    assert statuses[bucket_a] is RelevanceOutcome.CLASSIFIED
    # B: ordinary default budget — its third attempt arrives, exhausts the
    # default cap and converges; no override leaked onto it.
    assert statuses[bucket_b] is RelevanceOutcome.EXHAUSTED
    assert _attempt_count(conn, bucket_a) == 4
    assert _attempt_count(conn, bucket_b) == 3
    assert bucket_processing_state(conn, bucket_a) is BucketState.PROCESSED
    assert bucket_processing_state(conn, bucket_b) is BucketState.FAILED


def test_interrupt_during_recovery_writes_nothing_and_recovers(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4/R3 crash safety: an interrupt during the recovery attempt rolls back
    the whole in-flight attempt and leaves the requeued bucket discoverable
    for the next pass."""
    bucket_id, members = _standard_scenario(conn)
    _exhaust_to_failed(conn, bucket_id, members)
    assert requeue_failed_bucket(conn, bucket_id) is True

    real_utc_now = relevance._utc_now
    calls = {"n": 0}

    def sequenced_utc_now() -> str:
        calls["n"] += 1
        if calls["n"] == 2:  # 1: started_at; 2: finished_at inside the txn
            raise KeyboardInterrupt("operator stop during recovery")
        return real_utc_now()

    monkeypatch.setattr(relevance, "_utc_now", sequenced_utc_now)
    with pytest.raises(KeyboardInterrupt):
        run_relevance_pass(
            conn,
            ScriptedClassifier([_decisions_from_members(members)]),
            budget_overrides={bucket_id: 5},
        )
    assert _attempt_count(conn, bucket_id) == 3
    assert bucket_processing_state(conn, bucket_id) is BucketState.AWAITING_RECOVERY

    monkeypatch.undo()
    recovery = run_relevance_pass(
        conn,
        ScriptedClassifier([_decisions_from_members(members)]),
        budget_overrides={bucket_id: 5},
    )
    assert recovery.outcomes[0].status is RelevanceOutcome.CLASSIFIED
    assert bucket_processing_state(conn, bucket_id) is BucketState.PROCESSED


def test_repeated_recovery_pass_is_idempotent(
    conn: sqlite3.Connection,
) -> None:
    """F4/R3: a completed recovery leaves nothing for later passes — no new
    attempts, no duplicated decisions, empty result sets."""
    bucket_id, members = _standard_scenario(conn)
    _exhaust_to_failed(conn, bucket_id, members)
    requeue_failed_bucket(conn, bucket_id)
    run_relevance_pass(
        conn,
        ScriptedClassifier([_decisions_from_members(members)]),
        budget_overrides={bucket_id: 5},
    )
    again = run_relevance_pass(
        conn,
        ScriptedClassifier([]),
        budget_overrides={bucket_id: 5},
    )
    assert again.outcomes == ()
    assert again.finalized == ()
    assert again.terminal_failed == ()
    assert _attempt_count(conn, bucket_id) == 4
