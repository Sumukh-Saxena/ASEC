"""W1 tests: stdlib JSON HTTP adapter wire contract (plan §4.1/§4.2).

All network access goes through an injected fake transport, so the suite stays
offline. The wire contract is owner-approved as written (O10-1): function,
contract_version, model_id, prompt_version, input; response arrays plus
provenance.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from backend.ai.http_client import (
    AdapterContractError,
    HttpModelAdapter,
    HttpResponse,
    HttpStatusError,
    ProvenanceMismatchError,
    TransportFailure,
)
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.proposals import (
    ProposalDraft,
    ProposalGenerationRequest,
    ProposalMessage,
    ProposalResponse,
)
from backend.relevance import (
    STAGE_RELEVANCE_CLASSIFICATION,
    STAGE_RELEVANCE_FALLBACK_CLASSIFICATION,
    ContextMessage,
    MessageDecision,
    RelevanceOutcome,
    RelevanceRequest,
    RelevanceResponse,
    run_relevance_pass,
)

TS = "2026-01-01T10:00:00+00:00"
MODEL_ID = "small-4b"
PROMPT_VERSION = "rel-prompt-1"


class FakeTransport:
    """Scripted transport: each request consumes the next response or raises."""

    def __init__(self, responses: Sequence[HttpResponse | Exception]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpResponse:
        self.requests.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        if not self._responses:
            raise AssertionError("unexpected request: no scripted response left")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _json_response(payload: Mapping[str, Any], status: int = 200) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={},
        body=json.dumps(payload).encode("utf-8"),
    )


def _adapter(
    transport: Any,
    *,
    api_key: str | None = None,
    max_attempts: int = 1,
    model_id: str = MODEL_ID,
    prompt_version: str = PROMPT_VERSION,
    sleep: RecordingSleep | None = None,
) -> HttpModelAdapter:
    return HttpModelAdapter(
        base_url="http://model.test/",
        api_key=api_key,
        model_id=model_id,
        prompt_version=prompt_version,
        timeout_seconds=5.0,
        max_attempts=max_attempts,
        transport=transport,
        sleep=sleep if sleep is not None else RecordingSleep(),
    )


def _relevance_request(
    message_ids: tuple[int, ...] = (11, 12),
    *,
    content: str = "kuch karo yaar",
    mention_handles: tuple[str, ...] = ("@m1",),
) -> RelevanceRequest:
    return RelevanceRequest(
        bucket_id=3,
        group_handle="g1",
        vertical_name="Tech",
        instructions="CLASSIFICATION_POLICY: doubt policy",
        messages=tuple(
            ContextMessage(
                message_id=message_id,
                sender_person_id=7,
                sender_name="Neha",
                event_timestamp=TS,
                reply_to_message_id=None,
                masked_content=content,
                mention_handles=mention_handles,
            )
            for message_id in message_ids
        ),
    )


def _relevance_body(
    message_ids: tuple[int, ...] = (11, 12),
    *,
    model_id: str = MODEL_ID,
    model_version: str = "small-4b-v1",
    prompt_version: str = PROMPT_VERSION,
) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "decisions": [
            {"message_id": message_id, "decision": "RELEVANT"}
            for message_id in message_ids
        ],
    }


def _proposal_request() -> ProposalGenerationRequest:
    message = ProposalMessage(
        message_id=11,
        sender_name="Neha",
        event_timestamp=TS,
        reply_reference=None,
        content="need volunteers for stalls",
        mention_handles=("@m1",),
    )
    return ProposalGenerationRequest(
        bucket_id=3,
        vertical_name="Tech",
        candidate_messages=(message,),
        context_messages=(message,),
        instructions="PROPOSAL_POLICY: draft only",
    )


def _proposal_body(
    *,
    model_id: str = MODEL_ID,
    model_version: str = "small-4b-v1",
    prompt_version: str = "prop-prompt-1",
) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "proposals": [
            {
                "primary_message_id": 11,
                "ptype": "TASK",
                "description": "Set up the stall",
                "assignment_mode": "EXPLICIT",
                "proposed_assignees": ["@m1"],
                "people_needed": 2,
                "people_needed_unlimited": 0,
                "task_type": "INTERNAL",
                "suggested_responsible_vertical": "Tech",
                "affected_verticals": ["Tech"],
                "attributes": [["venue", "Main hall"]],
            }
        ],
    }


# --- W1: approved wire contract -------------------------------------------


def test_classify_sends_the_approved_wire_contract() -> None:
    transport = FakeTransport([_json_response(_relevance_body())])
    adapter = _adapter(transport)

    response = adapter.classify(_relevance_request())

    assert isinstance(response, RelevanceResponse)
    assert response.model_id == MODEL_ID
    assert response.model_version == "small-4b-v1"
    assert response.prompt_version == PROMPT_VERSION
    assert response.decisions == (
        MessageDecision(message_id=11, decision="RELEVANT"),
        MessageDecision(message_id=12, decision="RELEVANT"),
    )
    (request,) = transport.requests
    assert request["url"] == "http://model.test/v1/relevance"
    assert request["timeout"] == 5.0
    payload = json.loads(request["body"])
    assert set(payload) == {
        "function",
        "contract_version",
        "model_id",
        "prompt_version",
        "input",
    }
    assert payload["function"] == "relevance"
    assert payload["contract_version"] == "1"
    assert payload["model_id"] == MODEL_ID
    assert payload["prompt_version"] == PROMPT_VERSION
    assert payload["input"] == {
        "bucket_id": 3,
        "group_handle": "g1",
        "vertical_name": "Tech",
        "instructions": "CLASSIFICATION_POLICY: doubt policy",
        "messages": [
            {
                "message_id": 11,
                "sender_person_id": 7,
                "sender_name": "Neha",
                "event_timestamp": TS,
                "reply_to_message_id": None,
                "masked_content": "kuch karo yaar",
                "mention_handles": ["@m1"],
            },
            {
                "message_id": 12,
                "sender_person_id": 7,
                "sender_name": "Neha",
                "event_timestamp": TS,
                "reply_to_message_id": None,
                "masked_content": "kuch karo yaar",
                "mention_handles": ["@m1"],
            },
        ],
    }


def test_generate_sends_the_approved_wire_contract() -> None:
    transport = FakeTransport([_json_response(_proposal_body())])
    adapter = _adapter(transport)

    response = adapter.generate(_proposal_request())

    assert isinstance(response, ProposalResponse)
    assert response.model_id == MODEL_ID
    assert response.model_version == "small-4b-v1"
    assert response.prompt_version == "prop-prompt-1"
    assert response.proposals == (
        ProposalDraft(
            primary_message_id=11,
            ptype="TASK",
            description="Set up the stall",
            assignment_mode="EXPLICIT",
            proposed_assignees=("@m1",),
            people_needed=2,
            people_needed_unlimited=0,
            task_type="INTERNAL",
            suggested_responsible_vertical="Tech",
            affected_verticals=("Tech",),
            attributes=(("venue", "Main hall"),),
        ),
    )
    (request,) = transport.requests
    assert request["url"] == "http://model.test/v1/proposals"
    payload = json.loads(request["body"])
    assert set(payload) == {
        "function",
        "contract_version",
        "model_id",
        "prompt_version",
        "input",
    }
    assert payload["function"] == "proposal"
    assert payload["contract_version"] == "1"
    assert payload["input"] == {
        "bucket_id": 3,
        "vertical_name": "Tech",
        "instructions": "PROPOSAL_POLICY: draft only",
        "candidate_messages": [
            {
                "message_id": 11,
                "sender_name": "Neha",
                "event_timestamp": TS,
                "reply_reference": None,
                "content": "need volunteers for stalls",
                "mention_handles": ["@m1"],
            }
        ],
        "context_messages": [
            {
                "message_id": 11,
                "sender_name": "Neha",
                "event_timestamp": TS,
                "reply_reference": None,
                "content": "need volunteers for stalls",
                "mention_handles": ["@m1"],
            }
        ],
    }


def test_authorization_header_carries_the_key_but_the_body_does_not() -> None:
    transport = FakeTransport([_json_response(_relevance_body())])
    adapter = _adapter(transport, api_key="test-secret-key")

    adapter.classify(_relevance_request())

    (request,) = transport.requests
    assert request["headers"]["Authorization"] == "Bearer test-secret-key"
    assert b"test-secret-key" not in request["body"]


def test_adapter_is_stateless_between_calls() -> None:
    transport = FakeTransport(
        [
            _json_response(_relevance_body()),
            _json_response(_relevance_body()),
        ]
    )
    adapter = _adapter(transport)

    first = adapter.classify(_relevance_request())
    second = adapter.classify(_relevance_request())

    assert first == second
    assert len(transport.requests) == 2
    assert transport.requests[0]["body"] == transport.requests[1]["body"]


def test_provenance_is_echoed_verbatim() -> None:
    transport = FakeTransport(
        [
            _json_response(
                _relevance_body(
                    model_id=MODEL_ID,
                    model_version="v-2026-11",
                    prompt_version="rel-prompt-9",
                )
            )
        ]
    )
    adapter = _adapter(transport)

    response = adapter.classify(_relevance_request())

    assert response.model_version == "v-2026-11"
    assert response.prompt_version == "rel-prompt-9"


# --- W3: failure mapping, retries and fallback order ----------------------


def _raw_response(body: bytes, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, headers={}, body=body)


class EchoDecisionsTransport:
    """Return one RELEVANT decision per message in the request body."""

    def __init__(self, model_id: str) -> None:
        self._model_id = model_id
        self.requests: list[dict[str, Any]] = []

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpResponse:
        self.requests.append(
            {"url": url, "headers": dict(headers), "body": body, "timeout": timeout}
        )
        payload = json.loads(body)
        message_ids = [
            message["message_id"] for message in payload["input"]["messages"]
        ]
        response = {
            "model_id": self._model_id,
            "model_version": f"{self._model_id}-v1",
            "prompt_version": payload["prompt_version"],
            "decisions": [
                {"message_id": message_id, "decision": "RELEVANT"}
                for message_id in message_ids
            ],
        }
        return _json_response(response)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = connect(":memory:")
    apply_migrations(connection)
    yield connection
    connection.close()


def _seed_closed_bucket(conn: sqlite3.Connection) -> tuple[int, list[int]]:
    conn.execute("INSERT INTO vertical (id, name) VALUES (1, 'Tech')")
    conn.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (1, 'chan-1', 1)"
    )
    cursor = conn.execute(
        "INSERT INTO bucket (group_id, start_event_timestamp, end_event_timestamp,"
        " parameters_used, processing_state)"
        " VALUES (1, ?, ?, '{\"window_w\": 10, \"burst_gap_g\": 5,"
        " \"max_duration_m\": 30, \"close_grace_c\": 3}', 'CLOSED')",
        (TS, TS),
    )
    bucket_id = int(cursor.lastrowid or 0)
    message_ids: list[int] = []
    for index in (1, 2):
        cursor = conn.execute(
            "INSERT INTO source_message"
            " (source, source_message_id, group_id, event_timestamp,"
            "  received_at, content)"
            " VALUES ('discord', ?, 1, ?, ?, 'need help with stalls')",
            (f"mid-{index}", TS, TS),
        )
        message_id = int(cursor.lastrowid or 0)
        conn.execute(
            "INSERT INTO bucket_message (bucket_id, message_id, group_id)"
            " VALUES (?, ?, 1)",
            (bucket_id, message_id),
        )
        message_ids.append(message_id)
    return bucket_id, message_ids


def test_malformed_json_is_a_contract_error_not_an_empty_result() -> None:
    transport = FakeTransport([_raw_response(b"not-json")])
    adapter = _adapter(transport)

    with pytest.raises(AdapterContractError):
        adapter.classify(_relevance_request())

    assert len(transport.requests) == 1


def test_wrong_response_array_is_a_contract_error() -> None:
    transport = FakeTransport([_json_response(_proposal_body())])
    adapter = _adapter(transport)

    with pytest.raises(AdapterContractError):
        adapter.classify(_relevance_request())


def test_extra_response_keys_are_a_contract_error() -> None:
    body = _relevance_body()
    body["token"] = "should-not-be-here"
    transport = FakeTransport([_json_response(body)])
    adapter = _adapter(transport)

    with pytest.raises(AdapterContractError):
        adapter.classify(_relevance_request())


def test_extra_decision_keys_are_a_contract_error() -> None:
    body = _relevance_body()
    body["decisions"][0]["extra"] = "nope"
    transport = FakeTransport([_json_response(body)])
    adapter = _adapter(transport)

    with pytest.raises(AdapterContractError):
        adapter.classify(_relevance_request())


def test_4xx_is_not_retried() -> None:
    transport = FakeTransport([_raw_response(b"bad request", status=400)])
    sleep = RecordingSleep()
    adapter = _adapter(transport, max_attempts=3, sleep=sleep)

    with pytest.raises(HttpStatusError) as raised:
        adapter.classify(_relevance_request())

    assert raised.value.status == 400
    assert len(transport.requests) == 1
    assert sleep.calls == []


def test_5xx_is_retried_then_succeeds() -> None:
    transport = FakeTransport(
        [
            _raw_response(b"unavailable", status=503),
            _json_response(_relevance_body()),
        ]
    )
    sleep = RecordingSleep()
    adapter = _adapter(transport, max_attempts=3, sleep=sleep)

    response = adapter.classify(_relevance_request())

    assert len(response.decisions) == 2
    assert len(transport.requests) == 2
    assert sleep.calls == [0.5]


def test_5xx_exhausts_the_bounded_attempts() -> None:
    transport = FakeTransport(
        [
            _raw_response(b"unavailable", status=503),
            _raw_response(b"unavailable", status=503),
            _raw_response(b"unavailable", status=503),
        ]
    )
    sleep = RecordingSleep()
    adapter = _adapter(transport, max_attempts=3, sleep=sleep)

    with pytest.raises(HttpStatusError) as raised:
        adapter.classify(_relevance_request())

    assert raised.value.status == 503
    assert len(transport.requests) == 3
    assert sleep.calls == [0.5, 1.0]


def test_429_is_retried_with_a_bounded_backoff() -> None:
    transport = FakeTransport(
        [
            HttpResponse(status=429, headers={"Retry-After": "2"}, body=b""),
            _json_response(_relevance_body()),
        ]
    )
    sleep = RecordingSleep()
    adapter = _adapter(transport, max_attempts=2, sleep=sleep)

    response = adapter.classify(_relevance_request())

    assert len(response.decisions) == 2
    assert len(transport.requests) == 2
    assert len(sleep.calls) == 1
    assert 0 < sleep.calls[0] <= 8.0


def test_timeout_is_retried_then_succeeds() -> None:
    transport = FakeTransport(
        [TimeoutError("slow model"), _json_response(_relevance_body())]
    )
    sleep = RecordingSleep()
    adapter = _adapter(transport, max_attempts=2, sleep=sleep)

    response = adapter.classify(_relevance_request())

    assert len(response.decisions) == 2
    assert len(transport.requests) == 2
    assert sleep.calls == [0.5]


def test_timeout_after_attempts_raises_transport_failure() -> None:
    transport = FakeTransport(
        [TimeoutError("slow model"), TimeoutError("slow model")]
    )
    adapter = _adapter(transport, max_attempts=2)

    with pytest.raises(TransportFailure):
        adapter.classify(_relevance_request())

    assert len(transport.requests) == 2


def test_provenance_mismatch_is_a_contract_error_without_retry() -> None:
    transport = FakeTransport(
        [_json_response(_relevance_body(model_id="substituted-model"))]
    )
    adapter = _adapter(transport, max_attempts=3)

    with pytest.raises(ProvenanceMismatchError):
        adapter.classify(_relevance_request())

    assert len(transport.requests) == 1


def test_api_key_never_appears_in_repr_error_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    transport = FakeTransport([_raw_response(b"bad request", status=400)])
    adapter = _adapter(transport, api_key="super-secret-key", max_attempts=1)

    assert "super-secret-key" not in repr(adapter)
    assert MODEL_ID in repr(adapter)
    with pytest.raises(HttpStatusError) as raised:
        adapter.classify(_relevance_request(content="private festival plan"))

    assert "super-secret-key" not in str(raised.value)
    assert "super-secret-key" not in caplog.text
    assert "private festival plan" not in caplog.text


def test_primary_success_never_calls_the_fallback(conn: sqlite3.Connection) -> None:
    bucket_id, message_ids = _seed_closed_bucket(conn)
    primary_transport = EchoDecisionsTransport(MODEL_ID)
    fallback_transport = FakeTransport([])
    primary = _adapter(primary_transport, max_attempts=1)
    fallback = _adapter(
        fallback_transport, max_attempts=1, model_id="fallback-7b"
    )

    result = run_relevance_pass(conn, primary, fallback)

    assert result.outcomes[0].status == RelevanceOutcome.CLASSIFIED
    assert fallback_transport.requests == []
    rows = conn.execute(
        "SELECT stage, outcome FROM processing_attempt"
        " WHERE bucket_id = ? ORDER BY id",
        (bucket_id,),
    ).fetchall()
    assert [row["stage"] for row in rows] == [STAGE_RELEVANCE_CLASSIFICATION]
    decisions = conn.execute(
        "SELECT message_id FROM relevance_decision"
        " WHERE bucket_id = ? ORDER BY message_id",
        (bucket_id,),
    ).fetchall()
    assert [row["message_id"] for row in decisions] == message_ids


def test_fallback_runs_only_after_primary_failure_and_both_attempts_are_recorded(
    conn: sqlite3.Connection,
) -> None:
    bucket_id, _message_ids = _seed_closed_bucket(conn)
    primary_transport = FakeTransport([_raw_response(b"unavailable", status=503)])
    fallback_transport = EchoDecisionsTransport("fallback-7b")
    primary = _adapter(primary_transport, max_attempts=1)
    fallback = _adapter(
        fallback_transport, max_attempts=1, model_id="fallback-7b"
    )

    result = run_relevance_pass(conn, primary, fallback)

    assert result.outcomes[0].status == RelevanceOutcome.CLASSIFIED
    assert len(primary_transport.requests) == 1
    assert len(fallback_transport.requests) == 1
    rows = conn.execute(
        "SELECT stage, outcome FROM processing_attempt"
        " WHERE bucket_id = ? ORDER BY id",
        (bucket_id,),
    ).fetchall()
    assert [row["stage"] for row in rows] == [
        STAGE_RELEVANCE_CLASSIFICATION,
        STAGE_RELEVANCE_FALLBACK_CLASSIFICATION,
    ]
    assert rows[0]["outcome"] == "FAILED_RETRYABLE"
    assert rows[1]["outcome"] == "SUCCEEDED"

