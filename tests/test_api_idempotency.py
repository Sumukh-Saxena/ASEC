"""Idempotency keys on mutating routes (owner A2, migration 0025).

Covers replay of a stored 2xx, a reused key with a different request, the
person and route parts of the unique key, expiry, the "a 4xx is never stored"
rule, and the bounded cleanup function. The key is a header value; it is never
logged and never returned.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend.api import idempotency  # noqa: E402
from backend.config import Config, IdempotencySettings  # noqa: E402
from backend.db.connection import connect  # noqa: E402
from tests.api_fixtures import (  # noqa: E402
    AH_ROLL,
    VOLUNTEER2_ROLL,
    VOLUNTEER_ROLL,
    ApiHarness,
    assign,
    build_harness,
    count,
    make_proposal,
    make_task,
    query,
    seed_world,
)

KEY = "api-key-0123456789abcdef"


@pytest.fixture()
def harness(tmp_path: Path) -> ApiHarness:
    built = build_harness(tmp_path)
    seed_world(built.database)
    return built


def _headers(harness: ApiHarness, roll: str, key: str | None = KEY) -> dict[str, str]:
    headers = {"authorization": f"Bearer {harness.sign_in(roll)}"}
    if key is not None:
        headers["idempotency-key"] = key
    return headers


def _open_task(harness: ApiHarness) -> int:
    return make_task(
        harness.database,
        assignment_mode="OPEN",
        people_needed_unlimited=1,
    )


def test_the_key_is_optional_and_absence_changes_nothing(harness: ApiHarness) -> None:
    task_id = _open_task(harness)
    headers = _headers(harness, VOLUNTEER_ROLL, key=None)

    first = harness.call("POST", f"/tasks/{task_id}/claims", headers=headers)
    second = harness.call("POST", f"/tasks/{task_id}/claims", headers=headers)

    assert first.status == 201
    assert second.status == 409
    assert count(harness.database, "idempotency_key") == 0


@pytest.mark.parametrize(
    "key",
    ["too-short", "has spaces in the key value", "bad/slash/0123456789abcdef", "x" * 65],
)
def test_a_malformed_key_is_a_422_and_does_no_work(
    harness: ApiHarness, key: str
) -> None:
    task_id = _open_task(harness)
    headers = _headers(harness, VOLUNTEER_ROLL, key=key)

    response = harness.call("POST", f"/tasks/{task_id}/claims", headers=headers)

    assert response.status == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert count(harness.database, "task_assignee") == 0
    assert count(harness.database, "idempotency_key") == 0


def test_a_replay_returns_the_exact_stored_response_without_acting_again(
    harness: ApiHarness,
) -> None:
    task_id = _open_task(harness)
    headers = _headers(harness, VOLUNTEER_ROLL)

    first = harness.call("POST", f"/tasks/{task_id}/claims", headers=headers)
    second = harness.call("POST", f"/tasks/{task_id}/claims", headers=headers)

    assert first.status == 201
    assert second.status == 201
    assert second.body == first.body
    assert count(harness.database, "task_assignee") == 1
    assert count(harness.database, "idempotency_key") == 1
    stored = query(
        harness.database, "SELECT response_status, response_body FROM idempotency_key"
    )[0]
    assert stored["response_status"] == 201
    assert stored["response_body"] == first.body.decode("utf-8")


def test_the_same_key_with_a_different_request_is_a_422_reuse(
    harness: ApiHarness,
) -> None:
    first_task = make_task(harness.database)
    second_task = make_task(harness.database)
    assign(harness.database, first_task, 4, agreement="AWAITING")
    assign(harness.database, second_task, 4, agreement="AWAITING")
    headers = _headers(harness, VOLUNTEER_ROLL)

    first = harness.call(
        "POST",
        f"/tasks/{first_task}/agreement",
        headers=headers,
        json_body={"agreement": "ACCEPTED"},
    )
    reused = harness.call(
        "POST",
        f"/tasks/{second_task}/agreement",
        headers=headers,
        json_body={"agreement": "ACCEPTED"},
    )

    assert first.status == 200
    assert reused.status == 422
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert query(
        harness.database,
        "SELECT agreement FROM task_assignee WHERE task_id = ?",
        (second_task,),
    )[0]["agreement"] == "AWAITING"


def test_another_person_never_replays_the_first_persons_key(
    harness: ApiHarness,
) -> None:
    task_id = _open_task(harness)

    first = harness.call(
        "POST",
        f"/tasks/{task_id}/claims",
        headers=_headers(harness, VOLUNTEER_ROLL),
    )
    second = harness.call(
        "POST",
        f"/tasks/{task_id}/claims",
        headers=_headers(harness, VOLUNTEER2_ROLL),
    )

    assert first.status == 201
    assert second.status == 201
    assert second.body != first.body
    assert count(harness.database, "task_assignee", "task_id = ?", (task_id,)) == 2
    assert count(harness.database, "idempotency_key") == 2


def test_the_same_key_on_a_different_route_is_independent(
    harness: ApiHarness,
) -> None:
    task_id = _open_task(harness)
    headers = _headers(harness, VOLUNTEER_ROLL)

    claimed = harness.call("POST", f"/tasks/{task_id}/claims", headers=headers)
    advanced = harness.call(
        "POST",
        f"/tasks/{task_id}/status",
        headers=headers,
        json_body={"status": "IN_PROGRESS"},
    )

    assert claimed.status == 201
    assert advanced.status == 200
    assert count(harness.database, "idempotency_key") == 2


def test_an_expired_key_is_treated_as_new(harness: ApiHarness) -> None:
    proposal_id = make_proposal(
        harness.database,
        sender_person_id=3,
        assignees=(("Volunteer One", "RESOLVED", 4, "MENTION"),),
    )
    headers = _headers(harness, AH_ROLL)
    body = {"expected_revision_number": 0}

    first = harness.call(
        "POST", f"/proposals/{proposal_id}/accept", headers=headers, json_body=body
    )
    replay = harness.call(
        "POST", f"/proposals/{proposal_id}/accept", headers=headers, json_body=body
    )
    harness.clock.advance(25 * 60 * 60)
    after_expiry = harness.call(
        "POST", f"/proposals/{proposal_id}/accept", headers=headers, json_body=body
    )

    assert first.status == 200
    assert replay.body == first.body
    assert after_expiry.status == 200
    assert after_expiry.json()["created"] is False


def test_a_failed_request_is_not_stored_so_a_retry_can_succeed(
    harness: ApiHarness,
) -> None:
    task_id = make_task(harness.database)
    headers = _headers(harness, VOLUNTEER_ROLL)

    refused = harness.call(
        "POST",
        f"/tasks/{task_id}/status",
        headers=headers,
        json_body={"status": "IN_PROGRESS"},
    )
    assert refused.status == 409
    assert count(harness.database, "idempotency_key") == 0

    assign(harness.database, task_id, 4, agreement="ACCEPTED")
    retried = harness.call(
        "POST",
        f"/tasks/{task_id}/status",
        headers=headers,
        json_body={"status": "IN_PROGRESS"},
    )

    assert retried.status == 200
    assert count(harness.database, "idempotency_key") == 1


def test_reads_ignore_the_header_even_when_malformed(harness: ApiHarness) -> None:
    headers = _headers(harness, VOLUNTEER_ROLL, key="short")
    response = harness.call("GET", "/tasks", headers=headers)
    assert response.status == 200


def test_cleanup_deletes_expired_rows_in_a_bounded_batch(
    harness: ApiHarness,
) -> None:
    task_ids = [_open_task(harness) for _ in range(3)]
    headers = _headers(harness, VOLUNTEER_ROLL)
    for index, task_id in enumerate(task_ids):
        response = harness.call(
            "POST",
            f"/tasks/{task_id}/claims",
            headers={**headers, "idempotency-key": f"{KEY}-{index}"},
        )
        assert response.status == 201
    assert count(harness.database, "idempotency_key") == 3

    harness.clock.advance(25 * 60 * 60)
    connection = connect(harness.database)
    try:
        now = harness.clock.value
        assert idempotency.cleanup_expired(
            connection, now=now, batch_limit=2
        ) == 2
        assert count(harness.database, "idempotency_key") == 1
        assert idempotency.cleanup_expired(
            connection, now=now, batch_limit=2
        ) == 1
        assert idempotency.cleanup_expired(
            connection, now=now, batch_limit=2
        ) == 0
    finally:
        connection.close()


def test_cleanup_keeps_live_rows(harness: ApiHarness) -> None:
    task_id = _open_task(harness)
    response = harness.call(
        "POST",
        f"/tasks/{task_id}/claims",
        headers=_headers(harness, VOLUNTEER_ROLL),
    )
    assert response.status == 201

    connection = connect(harness.database)
    try:
        assert idempotency.cleanup_expired(
            connection, now=harness.clock.value, batch_limit=10
        ) == 0
    finally:
        connection.close()
    stored = query(harness.database, "SELECT response_body FROM idempotency_key")[0]
    assert json.loads(stored["response_body"])["task_id"] == task_id


def test_store_never_writes_a_non_2xx_row(harness: ApiHarness) -> None:
    """The guard is defence in depth; the contract is "only a 2xx is stored"."""
    connection = connect(harness.database)
    try:
        stored = idempotency.store(
            connection,
            person_id=4,
            route_key="TASK_CLAIM",
            key=KEY,
            request_hash="a" * 64,
            status=409,
            body='{"error":"CONFLICT"}',
            now=harness.clock.value,
            settings=IdempotencySettings(),
        )
    finally:
        connection.close()
    assert stored is False
    assert count(harness.database, "idempotency_key") == 0


def test_a_custom_key_length_window_is_configuration(tmp_path: Path) -> None:
    built = build_harness(
        tmp_path,
        config=Config(
            {
                "API_IDEMPOTENCY_KEY_MIN_LENGTH": "20",
                "API_IDEMPOTENCY_KEY_MAX_LENGTH": "30",
            }
        ),
    )
    seed_world(built.database)
    task_id = _open_task(built)
    headers = _headers(built, VOLUNTEER_ROLL, key="x" * 19)
    assert built.call(
        "POST", f"/tasks/{task_id}/claims", headers=headers
    ).status == 422
    headers = _headers(built, VOLUNTEER_ROLL, key="x" * 20)
    assert built.call(
        "POST", f"/tasks/{task_id}/claims", headers=headers
    ).status == 201
