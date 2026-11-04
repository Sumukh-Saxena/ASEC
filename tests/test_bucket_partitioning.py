"""Oversize-bucket partitioning tests (Phase 9 W6; FR-BKT-11).

FR-BKT-11 verbatim (docs/SRS.md §3.2): "No message shall ever be silently
truncated or deleted. A bucket too large for a model's input shall be
processed in parts, never cut." Plan §4.8 fixes the contract:

- the closed bucket is split into contiguous parts in ascending message id;
- relevance sends only its own messages per part but the full group/vertical
  context, and every part's provenance must agree before decisions merge;
- proposals send the whole bucket context to every part and only that part's
  candidates;
- the 0013/0014 attempt artifact stores a ``parts`` array instead of one
  request;
- any part failure fails the whole attempt and writes no partial decisions or
  proposals.

The tests below assert coverage (every message appears in exactly one part,
nothing truncated or dropped), provenance agreement, all-or-nothing failure
and the artifact shape.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence

import pytest

import backend.proposals as proposals
import backend.relevance as relevance
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.proposals import (
    ProposalDraft,
    ProposalGenerationRequest,
    ProposalOutcome,
    ProposalResponse,
    run_proposal_pass,
)
from backend.relevance import (
    MessageDecision,
    RelevanceOutcome,
    RelevanceRequest,
    RelevanceResponse,
    run_relevance_pass,
)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    apply_migrations(connection)
    yield connection
    connection.close()


# --- scenario helpers -----------------------------------------------------


def _vertical(conn: sqlite3.Connection, vertical_id: int, name: str) -> None:
    conn.execute("INSERT INTO vertical (id, name) VALUES (?, ?)", (vertical_id, name))


def _group(conn: sqlite3.Connection, group_id: int, *, vertical_id: int) -> None:
    conn.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (?, ?, ?)",
        (group_id, f"chan-{group_id}", vertical_id),
    )


def _person(conn: sqlite3.Connection, person_id: int, name: str, discord_id: str) -> int:
    conn.execute(
        "INSERT INTO person (id, name, discord_user_id, phone, role, active,"
        " roll_number) VALUES (?, ?, ?, ?, 'AH', 1, ?)",
        (
            person_id,
            name,
            discord_id,
            f"+919800003{person_id:03d}",
            f"PART-{person_id:03d}",
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
    sender_person_id: int | None = None,
    sender_discord_user_id: str | None = None,
    content: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO source_message"
        " (source, source_message_id, group_id, sender_person_id,"
        "  sender_discord_user_id, event_timestamp, received_at, content,"
        "  flag_unclassified)"
        " VALUES ('discord', ?, ?, ?, ?, '2026-01-01T10:00:00+00:00',"
        "         '2026-01-01T10:00:00+00:00', ?, 0)",
        (
            f"mid-{group_id}-{index}",
            group_id,
            sender_person_id,
            sender_discord_user_id,
            content if content is not None else f"message no {index}",
        ),
    )
    return int(cursor.lastrowid or 0)


def _bucket(conn: sqlite3.Connection, group_id: int, *, state: str = "CLOSED") -> int:
    cursor = conn.execute(
        "INSERT INTO bucket (group_id, start_event_timestamp, end_event_timestamp,"
        " parameters_used, processing_state)"
        " VALUES (?, '2026-01-01T10:00:00+00:00', '2026-01-01T10:00:10+00:00',"
        " '{\"window_w\": 15, \"burst_gap_g\": 5, \"max_duration_m\": 30,"
        "  \"close_grace_c\": 2}', ?)",
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


def _relevance_scenario(
    conn: sqlite3.Connection, *, message_count: int
) -> tuple[int, list[int]]:
    """One CLOSED bucket with ``message_count`` messages, sender known."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    person = _person(conn, 5, "Arjun", "5001")
    _membership_vertical(conn, person, 1)
    bucket_id = _bucket(conn, 1)
    message_ids: list[int] = []
    for index in range(1, message_count + 1):
        mid = _message(
            conn,
            1,
            index,
            sender_person_id=person,
            sender_discord_user_id="5001",
        )
        _membership(conn, bucket_id, mid, 1)
        message_ids.append(mid)
    return bucket_id, message_ids


def _relevance_success(
    conn: sqlite3.Connection, bucket_id: int, message_ids: Sequence[int]
) -> int:
    cursor = conn.execute(
        "INSERT INTO processing_attempt"
        " (stage, bucket_id, started_at, finished_at, outcome, model_id,"
        "  model_version, config_version)"
        " VALUES ('RELEVANCE_CLASSIFICATION', ?, '2026-01-01T10:01:00+00:00',"
        "  '2026-01-01T10:01:01+00:00', 'SUCCEEDED', 'rel-model', 'v1',"
        "  'rel-prompt-1')",
        (bucket_id,),
    )
    attempt_id = int(cursor.lastrowid or 0)
    for message_id in message_ids:
        conn.execute(
            "INSERT INTO relevance_decision"
            " (bucket_id, message_id, attempt_id, decision, model_id,"
            "  model_version, prompt_version, decided_at)"
            " VALUES (?, ?, ?, 'RELEVANT', 'rel-model', 'v1', 'rel-prompt-1', 't')",
            (bucket_id, message_id, attempt_id),
        )
    return attempt_id


def _proposal_scenario(
    conn: sqlite3.Connection, *, message_count: int
) -> tuple[int, list[int]]:
    """One PROCESSED bucket whose messages are all eligible RELEVANT candidates."""
    _vertical(conn, 1, "Tech")
    _group(conn, 1, vertical_id=1)
    person = _person(conn, 5, "Arjun", "5001")
    _membership_vertical(conn, person, 1)
    bucket_id = _bucket(conn, 1, state="PROCESSED")
    message_ids: list[int] = []
    for index in range(1, message_count + 1):
        mid = _message(
            conn,
            1,
            index,
            sender_person_id=person,
            sender_discord_user_id="5001",
        )
        _membership(conn, bucket_id, mid, 1)
        message_ids.append(mid)
    _relevance_success(conn, bucket_id, message_ids)
    return bucket_id, message_ids


# --- deterministic doubles ------------------------------------------------


class PartClassifier:
    """Classifier double that answers each request part by part.

    ``fail_on_call`` (0-based) raises before answering that call; ``versions``
    overrides the provenance model version per call so a disagreement between
    parts can be exercised.
    """

    def __init__(
        self,
        *,
        decision: str = "RELEVANT",
        versions: Sequence[str] | None = None,
        fail_on_call: int | None = None,
    ) -> None:
        self.decision = decision
        self.versions = versions
        self.fail_on_call = fail_on_call
        self.requests: list[RelevanceRequest] = []

    def classify(self, request: RelevanceRequest) -> RelevanceResponse:
        index = len(self.requests)
        self.requests.append(request)
        if self.fail_on_call == index:
            raise RuntimeError("classifier unavailable")
        version = (
            self.versions[index] if self.versions is not None else "rel-v1"
        )
        return RelevanceResponse(
            model_id="rel-model",
            model_version=version,
            prompt_version="rel-prompt-1",
            decisions=tuple(
                MessageDecision(message_id=message.message_id, decision=self.decision)
                for message in request.messages
            ),
        )


def _open_draft(primary_message_id: int) -> ProposalDraft:
    return ProposalDraft(
        primary_message_id=primary_message_id,
        ptype="TASK",
        description="plant the flags",
        assignment_mode="OPEN",
        proposed_assignees=None,
        task_type="INTERNAL",
    )


class PartGenerator:
    """Generator double; ``drafts_for`` may customise a part's output."""

    def __init__(
        self,
        *,
        versions: Sequence[str] | None = None,
        fail_on_call: int | None = None,
        drafts_for: (
            Callable[[ProposalGenerationRequest, int], tuple[ProposalDraft, ...]]
            | None
        ) = None,
    ) -> None:
        self.versions = versions
        self.fail_on_call = fail_on_call
        self.drafts_for = drafts_for
        self.requests: list[ProposalGenerationRequest] = []

    def generate(self, request: ProposalGenerationRequest) -> ProposalResponse:
        index = len(self.requests)
        self.requests.append(request)
        if self.fail_on_call == index:
            raise RuntimeError("generator unavailable")
        version = (
            self.versions[index] if self.versions is not None else "gen-v1"
        )
        if self.drafts_for is not None:
            drafts = self.drafts_for(request, index)
        else:
            drafts = tuple(
                _open_draft(message.message_id)
                for message in request.candidate_messages
            )
        return ProposalResponse(
            model_id="gen-model",
            model_version=version,
            prompt_version="proposal-prompt-v1",
            proposals=drafts,
        )


# --- relevance parts ------------------------------------------------------


def test_relevance_parts_cover_every_member_exactly_once(
    conn: sqlite3.Connection,
) -> None:
    """FR-BKT-11: contiguous parts in ascending id; every message appears in
    exactly one part, nothing truncated or dropped, and every decision lands."""
    bucket_id, message_ids = _relevance_scenario(conn, message_count=5)
    classifier = PartClassifier()

    result = run_relevance_pass(conn, classifier, max_messages_per_call=2)

    assert result.outcomes[0].status is RelevanceOutcome.CLASSIFIED
    assert [len(request.messages) for request in classifier.requests] == [2, 2, 1]
    part_ids = [
        [message.message_id for message in request.messages]
        for request in classifier.requests
    ]
    assert part_ids == [
        message_ids[0:2],
        message_ids[2:4],
        message_ids[4:5],
    ]
    assert [mid for part in part_ids for mid in part] == message_ids
    assert len(set(mid for part in part_ids for mid in part)) == len(message_ids)

    # Nothing cut: each original content appears in exactly one part.
    sent_content = [
        message.masked_content
        for request in classifier.requests
        for message in request.messages
    ]
    assert sent_content == [f"message no {index}" for index in range(1, 6)]

    decisions = conn.execute(
        "SELECT message_id FROM relevance_decision WHERE bucket_id = ?"
        " ORDER BY message_id",
        (bucket_id,),
    ).fetchall()
    assert [int(row["message_id"]) for row in decisions] == message_ids


def test_relevance_default_part_size_is_the_owner_approved_limit(
    conn: sqlite3.Connection,
) -> None:
    """O9-6: the default is 40 messages per call; 41 messages need two parts."""
    _bucket_id, message_ids = _relevance_scenario(conn, message_count=41)
    classifier = PartClassifier()

    run_relevance_pass(conn, classifier)

    assert [len(request.messages) for request in classifier.requests] == [40, 1]
    assert classifier.requests[0].messages[-1].message_id == message_ids[39]


def test_relevance_part_failure_fails_the_attempt_and_writes_no_decisions(
    conn: sqlite3.Connection,
) -> None:
    """Plan §4.8: any part failure fails the attempt; no partial decisions."""
    bucket_id, _message_ids = _relevance_scenario(conn, message_count=3)
    classifier = PartClassifier(fail_on_call=1)

    result = run_relevance_pass(conn, classifier, max_messages_per_call=2)

    assert result.outcomes[0].status is RelevanceOutcome.RETRYING
    assert len(classifier.requests) == 2  # part 1 answered, part 2 failed
    attempts = conn.execute(
        "SELECT outcome FROM processing_attempt WHERE bucket_id = ?",
        (bucket_id,),
    ).fetchall()
    assert [str(row["outcome"]) for row in attempts] == ["FAILED_RETRYABLE"]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM relevance_decision WHERE bucket_id = ?",
        (bucket_id,),
    ).fetchone()["n"] == 0
    unclassified = conn.execute(
        "SELECT COUNT(*) AS n FROM source_message"
        " WHERE id IN (SELECT message_id FROM bucket_message WHERE bucket_id = ?)"
        "   AND flag_unclassified = 1",
        (bucket_id,),
    ).fetchone()["n"]
    assert int(unclassified) == 3

    artifact = conn.execute(
        "SELECT masked_request FROM relevance_attempt_input"
        " WHERE attempt_id = (SELECT id FROM processing_attempt WHERE bucket_id = ?)",
        (bucket_id,),
    ).fetchone()
    assert artifact is not None
    stored = json.loads(str(artifact["masked_request"]))
    assert len(stored["parts"]) == 2  # the parts actually sent to the adapter


def test_relevance_part_provenance_must_agree(conn: sqlite3.Connection) -> None:
    """Plan §4.8: every part's provenance must agree before decisions merge."""
    bucket_id, _message_ids = _relevance_scenario(conn, message_count=3)
    classifier = PartClassifier(versions=["rel-v1", "rel-v2"])

    result = run_relevance_pass(conn, classifier, max_messages_per_call=2)

    assert result.outcomes[0].status is RelevanceOutcome.RETRYING
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM relevance_decision WHERE bucket_id = ?",
        (bucket_id,),
    ).fetchone()["n"] == 0


def test_relevance_artifact_stores_the_parts_array(conn: sqlite3.Connection) -> None:
    """Plan §4.8: the 0013 artifact is ``{"parts": [...]}``, one per part."""
    bucket_id, message_ids = _relevance_scenario(conn, message_count=5)
    classifier = PartClassifier()

    result = run_relevance_pass(conn, classifier, max_messages_per_call=2)
    attempt_id = result.outcomes[0].attempt_ids[0]
    row = conn.execute(
        "SELECT masked_request, request_sha256 FROM relevance_attempt_input"
        " WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    assert row is not None
    stored = json.loads(str(row["masked_request"]))
    assert list(stored) == ["parts"]
    assert len(stored["parts"]) == 3
    assert [entry["message_id"] for part in stored["parts"] for entry in part["messages"]] == (
        message_ids
    )
    assert all(part["bucket_id"] == bucket_id for part in stored["parts"])
    # The artifact is exactly the canonical serialization of the parts sent.
    expected = relevance._serialize_masked_request_parts(classifier.requests)
    assert str(row["masked_request"]) == expected
    assert str(row["request_sha256"]) == relevance._request_sha256(expected)


def test_relevance_part_limit_is_validated_before_any_state_change(
    conn: sqlite3.Connection,
) -> None:
    """NFR-MNT-01: a configured limit is a validated positive integer."""
    _bucket_id, _message_ids = _relevance_scenario(conn, message_count=1)
    classifier = PartClassifier()

    for bad in (0, -1, True, 2.5):
        with pytest.raises(ValueError):
            run_relevance_pass(conn, classifier, max_messages_per_call=bad)

    assert classifier.requests == []
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM processing_attempt"
    ).fetchone()["n"] == 0


# --- proposal parts -------------------------------------------------------


def test_proposal_parts_partition_candidates_and_keep_full_context(
    conn: sqlite3.Connection,
) -> None:
    """Plan §4.8: candidates partitioned, whole bucket context in every part."""
    bucket_id, message_ids = _proposal_scenario(conn, message_count=5)
    generator = PartGenerator()

    result = run_proposal_pass(conn, generator, max_candidates_per_call=2)

    assert result.outcomes[0].status is ProposalOutcome.GENERATED
    assert [len(request.candidate_messages) for request in generator.requests] == [
        2,
        2,
        1,
    ]
    candidate_ids = [
        [message.message_id for message in request.candidate_messages]
        for request in generator.requests
    ]
    assert [mid for part in candidate_ids for mid in part] == message_ids
    # Full bucket context to every part, in the same order.
    for request in generator.requests:
        assert [message.message_id for message in request.context_messages] == (
            message_ids
        )
    rows = conn.execute(
        "SELECT primary_message_id FROM proposal WHERE bucket_id = ?"
        " ORDER BY primary_message_id",
        (bucket_id,),
    ).fetchall()
    assert [int(row["primary_message_id"]) for row in rows] == message_ids


def test_proposal_default_part_size_is_the_owner_approved_limit(
    conn: sqlite3.Connection,
) -> None:
    """O9-6: the default is 20 candidates per call; 21 need two parts."""
    _bucket_id, message_ids = _proposal_scenario(conn, message_count=21)
    generator = PartGenerator()

    run_proposal_pass(conn, generator)

    assert [len(request.candidate_messages) for request in generator.requests] == [
        20,
        1,
    ]
    assert generator.requests[0].candidate_messages[-1].message_id == message_ids[19]


def test_proposal_part_failure_writes_no_proposals(conn: sqlite3.Connection) -> None:
    """Plan §4.8: any part failure fails the attempt; nothing partial persists."""
    bucket_id, _message_ids = _proposal_scenario(conn, message_count=3)
    generator = PartGenerator(fail_on_call=1)

    result = run_proposal_pass(conn, generator, max_candidates_per_call=2)

    assert result.outcomes[0].status is ProposalOutcome.RETRYING
    assert len(generator.requests) == 2
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM proposal WHERE bucket_id = ?",
        (bucket_id,),
    ).fetchone()["n"] == 0
    attempts = conn.execute(
        "SELECT outcome FROM processing_attempt WHERE bucket_id = ?"
        " AND stage IN ('PROPOSAL_GENERATION', 'PROPOSAL_FALLBACK_GENERATION')",
        (bucket_id,),
    ).fetchall()
    assert [str(row["outcome"]) for row in attempts] == ["FAILED_RETRYABLE"]
    artifact = conn.execute(
        "SELECT masked_request FROM proposal_attempt_input WHERE attempt_id = ?",
        (result.outcomes[0].attempt_ids[0],),
    ).fetchone()
    assert artifact is not None
    stored = json.loads(str(artifact["masked_request"]))
    assert len(stored["parts"]) == 2


def test_proposal_part_provenance_must_agree(conn: sqlite3.Connection) -> None:
    """One attempt row carries one provenance: parts must agree."""
    bucket_id, _message_ids = _proposal_scenario(conn, message_count=3)
    generator = PartGenerator(versions=["gen-v1", "gen-v2"])

    result = run_proposal_pass(conn, generator, max_candidates_per_call=2)

    assert result.outcomes[0].status is ProposalOutcome.RETRYING
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM proposal WHERE bucket_id = ?",
        (bucket_id,),
    ).fetchone()["n"] == 0


def test_proposal_artifact_stores_parts_and_the_shared_handle_map(
    conn: sqlite3.Connection,
) -> None:
    """Plan §4.8: 0014 stores the ``parts`` array plus the bucket-wide map."""
    _bucket_id, message_ids = _proposal_scenario(conn, message_count=3)
    _mention(conn, message_ids[0], "7001", 0)
    generator = PartGenerator()

    result = run_proposal_pass(conn, generator, max_candidates_per_call=2)
    attempt_id = result.outcomes[0].attempt_ids[0]
    row = conn.execute(
        "SELECT masked_request, request_sha256 FROM proposal_attempt_input"
        " WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    assert row is not None
    stored = json.loads(str(row["masked_request"]))
    assert list(stored) == ["mention_handle_map", "parts"]
    assert stored["mention_handle_map"] == {"@m1": "7001"}
    assert len(stored["parts"]) == 2
    assert [
        entry["message_id"] for part in stored["parts"] for entry in part["candidate_messages"]
    ] == message_ids
    for part in stored["parts"]:
        assert len(part["context_messages"]) == 3

    expected_hash = proposals._hash_text(
        proposals.serialize_proposal_request_parts(
            generator.requests, include_handle_map=False
        )
    )
    assert str(row["request_sha256"]) == expected_hash


def test_proposal_candidate_in_another_part_is_rejected_not_duplicated(
    conn: sqlite3.Connection,
) -> None:
    """Plan §4.8: a candidate cannot appear in two parts; a foreign item is
    rejected as today (PRIMARY_NOT_CANDIDATE) and valid siblings persist."""
    bucket_id, message_ids = _proposal_scenario(conn, message_count=2)

    def drafts_for(
        request: ProposalGenerationRequest, index: int
    ) -> tuple[ProposalDraft, ...]:
        if index == 0:
            # One valid item for this part's candidate, one item for the
            # candidate that belongs to the other part.
            return (_open_draft(request.candidate_messages[0].message_id),
                    _open_draft(message_ids[1]))
        return ()

    generator = PartGenerator(drafts_for=drafts_for)
    result = run_proposal_pass(conn, generator, max_candidates_per_call=1)

    assert result.outcomes[0].status is ProposalOutcome.GENERATED
    assert [len(request.candidate_messages) for request in generator.requests] == [
        1,
        1,
    ]
    rows = conn.execute(
        "SELECT primary_message_id FROM proposal WHERE bucket_id = ?",
        (bucket_id,),
    ).fetchall()
    assert [int(row["primary_message_id"]) for row in rows] == [message_ids[0]]
    artifact = conn.execute(
        "SELECT rejected_items FROM proposal_attempt_input WHERE attempt_id = ?",
        (result.outcomes[0].attempt_ids[0],),
    ).fetchone()
    assert artifact is not None
    codes = [entry["reason_code"] for entry in json.loads(str(artifact["rejected_items"]))]
    assert codes == ["PRIMARY_NOT_CANDIDATE"]


def test_proposal_part_limit_is_validated_before_any_state_change(
    conn: sqlite3.Connection,
) -> None:
    """NFR-MNT-01: a configured limit is a validated positive integer."""
    _bucket_id, _message_ids = _proposal_scenario(conn, message_count=1)
    generator = PartGenerator()

    for bad in (0, -1, True, 2.5):
        with pytest.raises(ValueError):
            run_proposal_pass(conn, generator, max_candidates_per_call=bad)

    assert generator.requests == []
    assert conn.execute("SELECT COUNT(*) AS n FROM proposal").fetchone()["n"] == 0
