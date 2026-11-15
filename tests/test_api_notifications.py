"""Notification and push-endpoint routes (W4; FR-NOT-07, NFR-SEC-03).

A person sees only their own notifications and may mark only their own rows
read; another person's row is a recorded denial, never a silent no-op. The push
endpoint routes are person-scoped and the registration route is
idempotency-key capable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from tests.api_fixtures import (  # noqa: E402
    VOLUNTEER2_ROLL,
    VOLUNTEER_ROLL,
    ApiHarness,
    build_harness,
    count,
    make_notification,
    make_task,
    query,
    seed_world,
)

PUSH_TOKEN = "fcm-token-0123456789abcdef"
KEY = "push-key-0123456789abcdef"


@pytest.fixture()
def harness(tmp_path: Path) -> ApiHarness:
    built = build_harness(tmp_path)
    seed_world(built.database)
    return built


def _headers(harness: ApiHarness, roll: str) -> dict[str, str]:
    return {"authorization": f"Bearer {harness.sign_in(roll)}"}


def _notification(harness: ApiHarness, *, recipient: int = 4) -> tuple[int, int]:
    task_id = make_task(harness.database)
    notification_id = make_notification(
        harness.database, recipient_person_id=recipient, task_id=task_id
    )
    return task_id, notification_id


def test_a_person_sees_only_their_own_notifications(harness: ApiHarness) -> None:
    _task_id, notification_id = _notification(harness)
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call("GET", "/notifications", headers=headers)

    assert response.status == 200
    body = response.json()
    ids = [item["notification_id"] for item in body["notifications"]]
    assert ids == [notification_id]
    item = body["notifications"][0]
    assert item["event_type"] == "WORK_COMPLETED"
    assert item["read_at"] is None
    assert "ph-" not in response.body.decode("utf-8")


def test_another_person_sees_an_empty_list(harness: ApiHarness) -> None:
    _notification(harness)
    response = harness.call(
        "GET", "/notifications", headers=_headers(harness, VOLUNTEER2_ROLL)
    )
    assert response.status == 200
    assert response.json()["notifications"] == []


def test_unread_only_filters_after_a_read(harness: ApiHarness) -> None:
    _task_id, notification_id = _notification(harness)
    headers = _headers(harness, VOLUNTEER_ROLL)

    assert harness.call(
        "GET", "/notifications?unread_only=true", headers=headers
    ).json()["notifications"]
    marked = harness.call(
        "POST", f"/notifications/{notification_id}/read", headers=headers
    )
    assert marked.status == 200
    assert marked.json() == {"notification_id": notification_id, "read": True}
    assert harness.call(
        "GET", "/notifications?unread_only=true", headers=headers
    ).json()["notifications"] == []


def test_a_second_read_is_not_an_error(harness: ApiHarness) -> None:
    _task_id, notification_id = _notification(harness)
    headers = _headers(harness, VOLUNTEER_ROLL)

    first = harness.call(
        "POST", f"/notifications/{notification_id}/read", headers=headers
    )
    second = harness.call(
        "POST", f"/notifications/{notification_id}/read", headers=headers
    )

    assert first.json()["read"] is True
    assert second.status == 200
    assert second.json()["read"] is False


def test_reading_another_persons_notification_is_a_recorded_403(
    harness: ApiHarness,
) -> None:
    _task_id, notification_id = _notification(harness)

    response = harness.call(
        "POST",
        f"/notifications/{notification_id}/read",
        headers=_headers(harness, VOLUNTEER2_ROLL),
    )

    assert response.status == 403
    rows = query(
        harness.database,
        "SELECT * FROM authorization_denial WHERE operation = 'NOTIFICATION_READ'",
    )
    assert len(rows) == 1
    assert rows[0]["actor_person_id"] == 7
    assert rows[0]["reason"] == "NOT_RECIPIENT"
    assert count(harness.database, "notification_read") == 0


def test_a_missing_notification_is_a_404(harness: ApiHarness) -> None:
    response = harness.call(
        "POST",
        "/notifications/9999/read",
        headers=_headers(harness, VOLUNTEER_ROLL),
    )
    assert response.status == 404


def test_registration_creates_one_active_endpoint(harness: ApiHarness) -> None:
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call(
        "POST",
        "/push-endpoints",
        headers=headers,
        json_body={"token": PUSH_TOKEN, "provider": "FCM"},
    )

    assert response.status == 201
    endpoint_id = response.json()["endpoint_id"]
    rows = query(
        harness.database,
        "SELECT person_id, provider, active FROM push_endpoint WHERE id = ?",
        (endpoint_id,),
    )
    assert len(rows) == 1
    assert rows[0]["person_id"] == 4
    assert rows[0]["active"] == 1


def test_a_second_registration_refreshes_the_same_row(
    harness: ApiHarness,
) -> None:
    headers = _headers(harness, VOLUNTEER_ROLL)
    body = {"token": PUSH_TOKEN}

    first = harness.call("POST", "/push-endpoints", headers=headers, json_body=body)
    second = harness.call("POST", "/push-endpoints", headers=headers, json_body=body)

    assert first.status == 201
    assert second.json()["endpoint_id"] == first.json()["endpoint_id"]
    assert count(harness.database, "push_endpoint") == 1


def test_another_persons_token_is_a_409(harness: ApiHarness) -> None:
    harness.call(
        "POST",
        "/push-endpoints",
        headers=_headers(harness, VOLUNTEER_ROLL),
        json_body={"token": PUSH_TOKEN},
    )

    response = harness.call(
        "POST",
        "/push-endpoints",
        headers=_headers(harness, VOLUNTEER2_ROLL),
        json_body={"token": PUSH_TOKEN},
    )

    assert response.status == 409
    assert response.json()["error"]["code"] == "CONFLICT"
    assert count(harness.database, "push_endpoint") == 1


def test_a_blank_token_is_a_422(harness: ApiHarness) -> None:
    response = harness.call(
        "POST",
        "/push-endpoints",
        headers=_headers(harness, VOLUNTEER_ROLL),
        json_body={"token": ""},
    )
    assert response.status == 422
    assert count(harness.database, "push_endpoint") == 0


def test_deactivation_turns_the_row_off_and_is_idempotent(
    harness: ApiHarness,
) -> None:
    headers = _headers(harness, VOLUNTEER_ROLL)
    created = harness.call(
        "POST", "/push-endpoints", headers=headers, json_body={"token": PUSH_TOKEN}
    )
    endpoint_id = created.json()["endpoint_id"]

    first = harness.call(
        "DELETE", f"/push-endpoints/{endpoint_id}", headers=headers
    )
    second = harness.call(
        "DELETE", f"/push-endpoints/{endpoint_id}", headers=headers
    )

    assert first.status == 200
    assert first.json() == {"endpoint_id": endpoint_id, "active": False}
    assert second.status == 200
    rows = query(
        harness.database,
        "SELECT active FROM push_endpoint WHERE id = ?",
        (endpoint_id,),
    )
    assert rows[0]["active"] == 0


def test_a_replayed_registration_returns_the_stored_response(
    harness: ApiHarness,
) -> None:
    headers = {**_headers(harness, VOLUNTEER_ROLL), "idempotency-key": KEY}
    body = {"token": PUSH_TOKEN}

    first = harness.call("POST", "/push-endpoints", headers=headers, json_body=body)
    second = harness.call("POST", "/push-endpoints", headers=headers, json_body=body)

    assert first.status == 201
    assert second.status == 201
    assert second.body == first.body
    assert count(harness.database, "push_endpoint") == 1
    assert count(harness.database, "idempotency_key") == 1


def test_identity_headers_never_change_the_notification_owner(
    harness: ApiHarness,
) -> None:
    _task_id, notification_id = _notification(harness)
    token = harness.sign_in(VOLUNTEER2_ROLL)
    response = harness.call(
        "POST",
        f"/notifications/{notification_id}/read",
        headers={
            "authorization": f"Bearer {token}",
            "x-person": "4",
            "x-role": "Admin",
        },
    )
    assert response.status == 403
    assert count(harness.database, "notification_read") == 0


def test_unauthenticated_notification_reads_are_401(harness: ApiHarness) -> None:
    response = harness.call("GET", "/notifications")
    assert response.status == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
