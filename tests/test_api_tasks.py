"""Task routes over the existing task service (W3; FR-TSK-*).

Covers FR-TSK-03/04/06/07/08/10/12/16, AC-08/AC-19/AC-20 and FR-AUT-06: every
task operation authorizes against the database, records a denial before the
403, and returns hidden (``PENDING``/``DENIED``) or cancelled tasks as not
found. ``PATCH /tasks/{id}`` carries ``expected_version``; a stale value is a
409 that includes the current task snapshot (FR-TSK-12, AC-19).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from tests.api_fixtures import (  # noqa: E402
    AH_ROLL,
    VOLUNTEER2_ROLL,
    VOLUNTEER_ROLL,
    ApiHarness,
    assign,
    build_harness,
    count,
    make_task,
    query,
    seed_world,
)


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


def _first(values: object) -> object:
    assert isinstance(values, tuple)
    return values[0]


def test_list_tasks_returns_visible_tasks_with_a_bounded_page(
    harness: ApiHarness,
) -> None:
    visible = make_task(harness.database, assignment_mode="OPEN",
                        people_needed_unlimited=1)
    hidden = make_task(harness.database, approval_state="PENDING")
    cancelled = make_task(harness.database, cancellation="CANCELLED")
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call("GET", "/tasks?limit=10", headers=headers)

    assert response.status == 200
    payload = response.json()
    assert payload["limit"] == 10
    ids = [task["id"] for task in payload["tasks"]]
    assert visible in ids
    assert hidden not in ids
    assert cancelled not in ids


@pytest.mark.parametrize("query_string", ["limit=0", "limit=-1", "limit=99999",
                                          "offset=-1", "limit=abc"])
def test_a_bad_page_request_is_a_422(harness: ApiHarness,
                                     query_string: str) -> None:
    headers = _headers(harness, VOLUNTEER_ROLL)
    response = harness.call("GET", f"/tasks?{query_string}", headers=headers)
    assert response.status == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_page_size_is_capped_by_configuration(tmp_path: Path) -> None:
    from backend.config import Config

    built = build_harness(
        tmp_path,
        config=Config({"API_TASKS_PAGE_DEFAULT": "5", "API_TASKS_PAGE_MAX": "7"}),
    )
    seed_world(built.database)
    headers = _headers(built, VOLUNTEER_ROLL)

    assert built.call("GET", "/tasks", headers=headers).json()["limit"] == 5
    assert built.call("GET", "/tasks?limit=7", headers=headers).status == 200
    assert built.call("GET", "/tasks?limit=8", headers=headers).status == 422


def test_get_one_task_returns_the_summary_and_hides_hidden_tasks(
    harness: ApiHarness,
) -> None:
    task_id = make_task(harness.database, assignment_mode="OPEN",
                        people_needed_unlimited=1)
    hidden = make_task(harness.database, approval_state="PENDING")
    cancelled = make_task(harness.database, cancellation="CANCELLED")
    headers = _headers(harness, VOLUNTEER_ROLL)

    allowed = harness.call("GET", f"/tasks/{task_id}", headers=headers)
    assert allowed.status == 200
    assert allowed.json()["task"]["id"] == task_id

    for denied_id in (hidden, cancelled):
        refused = harness.call("GET", f"/tasks/{denied_id}", headers=headers)
        assert refused.status == 404
        assert refused.json()["error"]["code"] == "NOT_FOUND"


def test_a_volunteer_claims_an_open_task_in_their_vertical(
    harness: ApiHarness,
) -> None:
    task_id = make_task(
        harness.database,
        assignment_mode="OPEN",
        people_needed_unlimited=1,
    )
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call("POST", f"/tasks/{task_id}/claims", headers=headers)

    assert response.status == 201
    body = response.json()
    assert body["task_id"] == task_id
    assert isinstance(body["assignment_id"], int)
    assert count(harness.database, "task_assignee", "task_id = ?", (task_id,)) == 1


def test_a_non_volunteer_claim_is_a_403_and_a_recorded_denial(
    harness: ApiHarness,
) -> None:
    task_id = make_task(
        harness.database,
        assignment_mode="OPEN",
        people_needed_unlimited=1,
    )
    headers = _headers(harness, AH_ROLL)

    response = harness.call("POST", f"/tasks/{task_id}/claims", headers=headers)

    assert response.status == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    rows = _denials(harness.database, "TASK_CLAIM")
    assert len(rows) == 1
    assert rows[0]["actor_person_id"] == 3
    assert rows[0]["subject_type"] == "TASK"
    assert rows[0]["subject_id"] == task_id
    assert rows[0]["reason"] == "NOT_ROLE"
    assert count(harness.database, "task_assignee") == 0


def test_ac20_a_volunteer_cannot_claim_in_another_vertical(
    harness: ApiHarness,
) -> None:
    task_id = make_task(
        harness.database,
        source_vertical_id=2,
        responsible_vertical_id=2,
        assignment_mode="OPEN",
        people_needed_unlimited=1,
    )
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call("POST", f"/tasks/{task_id}/claims", headers=headers)

    assert response.status == 403
    rows = _denials(harness.database, "TASK_CLAIM")
    assert len(rows) == 1
    assert rows[0]["reason"] == "NOT_IN_VERTICAL"
    assert rows[0]["subject_id"] == task_id


def test_an_assignee_accepts_their_own_agreement(harness: ApiHarness) -> None:
    task_id = make_task(harness.database)
    assign(harness.database, task_id, 4, agreement="AWAITING")
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call(
        "POST",
        f"/tasks/{task_id}/agreement",
        headers=headers,
        json_body={"agreement": "ACCEPTED"},
    )

    assert response.status == 200
    assert response.json()["agreement"] == "ACCEPTED"
    rows = query(
        harness.database,
        "SELECT agreement FROM task_assignee WHERE task_id = ? AND person_id = 4",
        (task_id,),
    )
    assert rows[0]["agreement"] == "ACCEPTED"


def test_deciding_for_another_assignee_without_admin_is_a_recorded_403(
    harness: ApiHarness,
) -> None:
    task_id = make_task(harness.database)
    assign(harness.database, task_id, 4, agreement="AWAITING")
    headers = _headers(harness, VOLUNTEER2_ROLL)

    response = harness.call(
        "POST",
        f"/tasks/{task_id}/agreement",
        headers=headers,
        json_body={"agreement": "ACCEPTED", "assignee_person_id": 4},
    )

    assert response.status == 403
    rows = _denials(harness.database, "TASK_AGREEMENT")
    assert len(rows) == 1
    assert rows[0]["reason"] == "NOT_ASSIGNEE"


def test_an_accepted_assignee_advances_work_status(harness: ApiHarness) -> None:
    task_id = make_task(harness.database)
    assign(harness.database, task_id, 4, agreement="ACCEPTED")
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call(
        "POST",
        f"/tasks/{task_id}/status",
        headers=headers,
        json_body={"status": "IN_PROGRESS"},
    )

    assert response.status == 200
    assert response.json()["work_status"] == "IN_PROGRESS"


def test_status_override_without_admin_is_a_recorded_403(
    harness: ApiHarness,
) -> None:
    task_id = make_task(harness.database)
    assign(harness.database, task_id, 4, agreement="ACCEPTED")
    headers = _headers(harness, VOLUNTEER2_ROLL)

    response = harness.call(
        "POST",
        f"/tasks/{task_id}/status",
        headers=headers,
        json_body={"status": "IN_PROGRESS", "assignee_person_id": 4},
    )

    assert response.status == 403
    assert _denials(harness.database, "TASK_STATUS")[0]["reason"] == "NOT_ASSIGNEE"


def test_patch_edits_a_task_and_a_stale_version_returns_the_snapshot(
    harness: ApiHarness,
) -> None:
    task_id = make_task(harness.database)
    headers = _headers(harness, AH_ROLL)

    updated = harness.call(
        "PATCH",
        f"/tasks/{task_id}",
        headers=headers,
        json_body={"expected_version": 1, "description": "Bring the spare generator"},
    )
    assert updated.status == 200
    assert updated.json()["task"]["version"] == 2

    stale = harness.call(
        "PATCH",
        f"/tasks/{task_id}",
        headers=headers,
        json_body={"expected_version": 1, "description": "Again"},
    )
    assert stale.status == 409
    body = stale.json()
    assert body["error"]["code"] == "CONFLICT"
    assert body["task"]["id"] == task_id
    assert body["task"]["version"] == 2
    assert body["task"]["description"] == "Bring the spare generator"


def test_patch_by_a_non_owner_is_a_recorded_403(harness: ApiHarness) -> None:
    task_id = make_task(harness.database)
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call(
        "PATCH",
        f"/tasks/{task_id}",
        headers=headers,
        json_body={"expected_version": 1, "description": "Mine now"},
    )

    assert response.status == 403
    rows = _denials(harness.database, "TASK_EDIT")
    assert len(rows) == 1
    # FR-AUT-04 row 6 gives Volunteer "—" for task management, so the route
    # gate refuses the role before the service would examine ownership.
    assert rows[0]["reason"] == "NOT_ROLE"


def test_reassign_adds_an_assignee_with_the_expected_version(
    harness: ApiHarness,
) -> None:
    task_id = make_task(harness.database)
    headers = _headers(harness, AH_ROLL)

    response = harness.call(
        "POST",
        f"/tasks/{task_id}/reassign",
        headers=headers,
        json_body={"expected_version": 1, "add": [4]},
    )

    assert response.status == 200
    assert response.json()["task"]["version"] == 2
    assert count(harness.database, "task_assignee", "task_id = ?", (task_id,)) == 1


def test_reassign_by_a_non_owner_is_a_recorded_403(harness: ApiHarness) -> None:
    task_id = make_task(harness.database)
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call(
        "POST",
        f"/tasks/{task_id}/reassign",
        headers=headers,
        json_body={"expected_version": 1, "add": [7]},
    )

    assert response.status == 403
    # FR-AUT-04 row 6: Volunteer has no task-management cell (AC-20).
    assert _denials(harness.database, "TASK_REASSIGN")[0]["reason"] == "NOT_ROLE"


def test_cancel_by_the_creator_marks_the_task_cancelled(
    harness: ApiHarness,
) -> None:
    task_id = make_task(harness.database)
    headers = _headers(harness, AH_ROLL)

    response = harness.call(
        "POST",
        f"/tasks/{task_id}/cancel",
        headers=headers,
        json_body={"reason": "The event moved"},
    )

    assert response.status == 200
    assert response.json()["cancelled"] is True
    rows = query(harness.database, "SELECT cancellation FROM task WHERE id = ?",
                 (task_id,))
    assert rows[0]["cancellation"] == "CANCELLED"


def test_cancel_by_a_non_owner_is_a_recorded_403(harness: ApiHarness) -> None:
    task_id = make_task(harness.database)
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call(
        "POST",
        f"/tasks/{task_id}/cancel",
        headers=headers,
        json_body={"reason": "Not mine"},
    )

    assert response.status == 403
    # FR-AUT-04 row 6: Volunteer has no task-management cell (AC-20).
    assert _denials(harness.database, "TASK_CANCEL")[0]["reason"] == "NOT_ROLE"


def test_identity_headers_never_change_a_task_actor(harness: ApiHarness) -> None:
    """The actor comes from the bearer session, never from ``X-Person``/``X-Role``."""
    task_id = make_task(harness.database)
    token = harness.sign_in(VOLUNTEER_ROLL)
    response = harness.call(
        "PATCH",
        f"/tasks/{task_id}",
        headers={
            "authorization": f"Bearer {token}",
            "x-person": "3",
            "x-role": "Admin",
        },
        json_body={"expected_version": 1, "description": "Spoof attempt"},
    )

    assert response.status == 403
    assert query(
        harness.database,
        "SELECT description FROM task WHERE id = ?",
        (task_id,),
    )[0]["description"] == "Arrange the generator"
    assert _denials(harness.database, "TASK_EDIT")[0]["actor_person_id"] == 4


def test_unknown_body_fields_are_rejected_not_treated_as_identity(
    harness: ApiHarness,
) -> None:
    task_id = make_task(harness.database)
    headers = _headers(harness, VOLUNTEER_ROLL)
    response = harness.call(
        "PATCH",
        f"/tasks/{task_id}",
        headers=headers,
        json_body={"expected_version": 1, "description": "x", "person_id": 3},
    )
    assert response.status == 422


def test_unauthenticated_task_reads_are_401(harness: ApiHarness) -> None:
    response = harness.call("GET", "/tasks")
    assert response.status == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
