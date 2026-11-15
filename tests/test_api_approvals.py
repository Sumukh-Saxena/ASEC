"""Approval routes over the existing approval service (W4; FR-XV-04, FR-APR-*).

Only a recipient AH/Head (or an Admin) may read or decide a request. The route
matrix refuses a Volunteer before any service call and records the denial; the
service refuses a non-recipient and records its own denial. The first decision
wins: a later call returns the stored decision with ``changed=False``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from tests.api_fixtures import (  # noqa: E402
    ADMIN_ROLL,
    AH_ROLL,
    HEAD_ROLL,
    VOLUNTEER_ROLL,
    ApiHarness,
    build_harness,
    count,
    make_approval,
    make_task,
    query,
    seed_world,
)

KEY = "approval-key-0123456789"


@pytest.fixture()
def harness(tmp_path: Path) -> ApiHarness:
    built = build_harness(tmp_path)
    seed_world(built.database)
    return built


def _headers(harness: ApiHarness, roll: str) -> dict[str, str]:
    return {"authorization": f"Bearer {harness.sign_in(roll)}"}


def _denials(database: Path, operation: str) -> list[sqlite3.Row]:
    return query(
        database,
        "SELECT * FROM authorization_denial WHERE operation = ? ORDER BY id",
        (operation,),
    )


def _pending_task(harness: ApiHarness) -> tuple[int, int]:
    task_id = make_task(
        harness.database,
        source_vertical_id=1,
        responsible_vertical_id=2,
        approval_state="PENDING",
    )
    return task_id, make_approval(harness.database, task_id)


def test_a_recipient_ah_lists_the_pending_request(harness: ApiHarness) -> None:
    task_id, request_id = _pending_task(harness)
    headers = _headers(harness, AH_ROLL)

    response = harness.call("GET", "/approvals", headers=headers)

    assert response.status == 200
    body = response.json()
    ids = [item["request_id"] for item in body["approvals"]]
    assert request_id in ids
    item = next(row for row in body["approvals"] if row["request_id"] == request_id)
    assert item["task_id"] == task_id
    assert item["decision"] is None
    assert "phone" not in response.body.decode("utf-8")
    assert "ph-" not in response.body.decode("utf-8")


def test_a_volunteer_list_is_a_recorded_403(harness: ApiHarness) -> None:
    _pending_task(harness)
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call("GET", "/approvals", headers=headers)

    assert response.status == 403
    rows = _denials(harness.database, "APPROVAL_LIST")
    assert len(rows) == 1
    assert rows[0]["actor_person_id"] == 4
    assert rows[0]["reason"] == "NOT_ROLE"


def test_one_request_is_returned_to_its_recipient(harness: ApiHarness) -> None:
    task_id, request_id = _pending_task(harness)
    headers = _headers(harness, AH_ROLL)

    response = harness.call("GET", f"/approvals/{request_id}", headers=headers)

    assert response.status == 200
    item = response.json()["approval"]
    assert item["request_id"] == request_id
    assert item["task_id"] == task_id
    assert item["requester_person_id"] == 3


def test_a_head_outside_the_responsible_vertical_is_a_recorded_403(
    harness: ApiHarness,
) -> None:
    _task_id, request_id = _pending_task(harness)
    headers = _headers(harness, HEAD_ROLL)

    response = harness.call("GET", f"/approvals/{request_id}", headers=headers)

    assert response.status == 403
    rows = _denials(harness.database, "approval.read")
    assert len(rows) == 1
    assert rows[0]["reason"] == "HEAD_NOT_IN_RESPONSIBLE_VERTICAL"


def test_a_missing_request_is_a_404(harness: ApiHarness) -> None:
    headers = _headers(harness, AH_ROLL)
    response = harness.call("GET", "/approvals/9999", headers=headers)
    assert response.status == 404


def test_an_ah_approves_and_the_task_becomes_approved(harness: ApiHarness) -> None:
    task_id, request_id = _pending_task(harness)
    headers = _headers(harness, AH_ROLL)

    response = harness.call(
        "POST",
        f"/approvals/{request_id}/decision",
        headers=headers,
        json_body={"decision": "APPROVED", "message": "Go ahead"},
    )

    assert response.status == 200
    body = response.json()
    assert body["decision"] == "APPROVED"
    assert body["changed"] is True
    assert body["message"] == "Go ahead"
    state = query(
        harness.database, "SELECT approval_state FROM task WHERE id = ?", (task_id,)
    )[0]["approval_state"]
    assert state == "APPROVED"
    assert count(harness.database, "approval_decision") == 1


def test_the_first_decision_wins(harness: ApiHarness) -> None:
    _task_id, request_id = _pending_task(harness)

    first = harness.call(
        "POST",
        f"/approvals/{request_id}/decision",
        headers=_headers(harness, AH_ROLL),
        json_body={"decision": "APPROVED"},
    )
    second = harness.call(
        "POST",
        f"/approvals/{request_id}/decision",
        headers=_headers(harness, ADMIN_ROLL),
        json_body={"decision": "DENIED"},
    )

    assert first.status == 200
    assert second.status == 200
    assert second.json()["decision"] == "APPROVED"
    assert second.json()["changed"] is False
    assert count(harness.database, "approval_decision") == 1


def test_a_volunteer_decision_is_a_recorded_403(harness: ApiHarness) -> None:
    _task_id, request_id = _pending_task(harness)

    response = harness.call(
        "POST",
        f"/approvals/{request_id}/decision",
        headers=_headers(harness, VOLUNTEER_ROLL),
        json_body={"decision": "APPROVED"},
    )

    assert response.status == 403
    rows = _denials(harness.database, "APPROVAL_DECIDE")
    assert len(rows) == 1
    assert rows[0]["reason"] == "NOT_ROLE"
    assert count(harness.database, "approval_decision") == 0


def test_an_unknown_decision_token_is_a_422(harness: ApiHarness) -> None:
    _task_id, request_id = _pending_task(harness)

    response = harness.call(
        "POST",
        f"/approvals/{request_id}/decision",
        headers=_headers(harness, AH_ROLL),
        json_body={"decision": "MAYBE"},
    )

    assert response.status == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_unknown_body_fields_are_rejected(harness: ApiHarness) -> None:
    _task_id, request_id = _pending_task(harness)

    response = harness.call(
        "POST",
        f"/approvals/{request_id}/decision",
        headers=_headers(harness, AH_ROLL),
        json_body={"decision": "APPROVED", "role": "Admin"},
    )

    assert response.status == 422


def test_a_replayed_decision_returns_the_stored_response(
    harness: ApiHarness,
) -> None:
    _task_id, request_id = _pending_task(harness)
    headers = {**_headers(harness, AH_ROLL), "idempotency-key": KEY}

    first = harness.call(
        "POST",
        f"/approvals/{request_id}/decision",
        headers=headers,
        json_body={"decision": "APPROVED"},
    )
    second = harness.call(
        "POST",
        f"/approvals/{request_id}/decision",
        headers=headers,
        json_body={"decision": "APPROVED"},
    )

    assert first.status == 200
    assert second.status == 200
    assert second.body == first.body
    assert count(harness.database, "approval_decision") == 1
    assert count(harness.database, "idempotency_key") == 1


def test_identity_headers_never_make_a_volunteer_an_approver(
    harness: ApiHarness,
) -> None:
    _task_id, request_id = _pending_task(harness)
    token = harness.sign_in(VOLUNTEER_ROLL)
    response = harness.call(
        "GET",
        f"/approvals/{request_id}",
        headers={
            "authorization": f"Bearer {token}",
            "x-person": "3",
            "x-role": "AH",
        },
    )
    assert response.status == 403
    assert count(harness.database, "approval_decision") == 0
