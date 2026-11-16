"""Hierarchy visibility for personal data and message content (W6).

NFR-SEC-03: phone numbers and message content are visible only for people
below the caller in the roster hierarchy — Admin everyone, Head everyone in
their vertical, AH the volunteers who report to them, Volunteer only self —
with three exceptions: (a) a task's creator sees its assignees' contact
details, (b) the reviewer sees the proposal's messages, (c) an approver sees
the task description and the requester's note but not the source chat.

The unit cases pin :mod:`backend.visibility`; the route cases pin what the
serializers actually emit, because permission must remove a field rather than
return ``null``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend import visibility  # noqa: E402
from backend.auth import AuthenticatedPerson  # noqa: E402
from backend.db.connection import connect  # noqa: E402
from tests.api_fixtures import (  # noqa: E402
    ADMIN_ROLL,
    AH_ROLL,
    HEAD_ROLL,
    VOLUNTEER_ROLL,
    ApiHarness,
    assign,
    build_harness,
    make_approval,
    make_notification,
    make_proposal,
    make_task,
    query,
    seed_team,
    seed_world,
)


@pytest.fixture()
def harness(tmp_path: Path) -> ApiHarness:
    built = build_harness(tmp_path)
    seed_world(built.database)
    seed_team(built.database)
    return built


def _headers(harness: ApiHarness, roll: str) -> dict[str, str]:
    return {"authorization": f"Bearer {harness.sign_in(roll)}"}


def _actor(person_id: int, role: str, vertical_ids: tuple[int, ...]) -> AuthenticatedPerson:
    return AuthenticatedPerson(
        person_id=person_id, role=role, vertical_ids=vertical_ids
    )


def _fields(
    harness: ApiHarness,
    actor: AuthenticatedPerson,
    target_person_id: int,
    *,
    task_id: int | None = None,
) -> frozenset[str]:
    connection = connect(harness.database)
    try:
        return visibility.visible_fields(
            connection, actor, target_person_id, task_id=task_id
        )
    finally:
        connection.close()


def _can_view(
    harness: ApiHarness, actor: AuthenticatedPerson, target_person_id: int
) -> bool:
    connection = connect(harness.database)
    try:
        return visibility.can_view_person(connection, actor, target_person_id)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# The hierarchy rule itself
# ---------------------------------------------------------------------------


def test_admin_sees_everyone_with_contact_details(harness: ApiHarness) -> None:
    admin = _actor(1, "Admin", (2,))
    assert _can_view(harness, admin, 4)
    assert _can_view(harness, admin, 10)
    assert "phone" in _fields(harness, admin, 10)


def test_head_sees_everyone_in_their_vertical_only(harness: ApiHarness) -> None:
    head = _actor(2, "Head", (1,))
    for person_id in (3, 4, 7, 9, 11):
        assert _can_view(harness, head, person_id), person_id
        assert "phone" in _fields(harness, head, person_id)
    for person_id in (8, 10):
        assert not _can_view(harness, head, person_id)
        assert _fields(harness, head, person_id) == visibility.BASE_FIELDS
        assert "phone" not in _fields(harness, head, person_id)


def test_ah_sees_only_the_volunteers_who_report_to_them(harness: ApiHarness) -> None:
    ah = _actor(3, "AH", (1,))
    for person_id in (4, 7):
        assert _can_view(harness, ah, person_id), person_id
        assert "phone" in _fields(harness, ah, person_id)
    # A volunteer reporting to the other AH, the other AH and the Head above.
    for person_id in (11, 9, 2):
        assert not _can_view(harness, ah, person_id)
        assert _fields(harness, ah, person_id) == visibility.BASE_FIELDS
        assert "phone" not in _fields(harness, ah, person_id)


def test_a_volunteer_sees_only_self(harness: ApiHarness) -> None:
    volunteer = _actor(4, "Volunteer", (1,))
    assert _can_view(harness, volunteer, 4)
    assert "phone" in _fields(harness, volunteer, 4)
    assert not _can_view(harness, volunteer, 7)
    assert _fields(harness, volunteer, 7) == visibility.BASE_FIELDS
    assert "phone" not in _fields(harness, volunteer, 7)


def test_an_unknown_target_exposes_nothing_even_to_an_admin(
    harness: ApiHarness,
) -> None:
    admin = _actor(1, "Admin", (2,))
    assert not _can_view(harness, admin, 999)
    assert _fields(harness, admin, 999) == frozenset()


# ---------------------------------------------------------------------------
# What the routes actually emit
# ---------------------------------------------------------------------------


def test_me_returns_the_callers_own_name_and_phone(harness: ApiHarness) -> None:
    body = harness.call(
        "GET", "/me", headers=_headers(harness, VOLUNTEER_ROLL)
    ).json()
    assert body["person"]["id"] == 4
    assert body["person"]["name"] == "Volunteer One"
    assert body["person"]["phone"] == "ph-4"


def test_the_head_dashboard_phones_everyone_in_the_vertical_only(
    harness: ApiHarness,
) -> None:
    body = harness.call(
        "GET", "/dashboard/head", headers=_headers(harness, HEAD_ROLL)
    ).json()
    people = {person["id"]: person for person in body["people"]}
    assert set(people) == {3, 4, 6, 7, 9, 11}  # active, vertical 1, not self
    assert people[4]["phone"] == "ph-4"
    assert people[11]["phone"] == "ph-11"
    assert 10 not in people  # vertical 2
    assert 5 not in people  # deactivated


def test_the_ah_dashboard_phones_only_direct_reports(harness: ApiHarness) -> None:
    body = harness.call(
        "GET", "/dashboard/ah", headers=_headers(harness, AH_ROLL)
    ).json()
    volunteers = {person["id"]: person for person in body["volunteers"]}
    assert set(volunteers) == {4, 7}
    assert volunteers[4]["phone"] == "ph-4"
    assert 11 not in volunteers  # reports to AH Two


def test_exception_a_lets_a_task_creator_see_an_assignees_phone(
    harness: ApiHarness,
) -> None:
    """NFR-SEC-03(a): the creator sees the contact details of its assignees."""
    task = make_task(
        harness.database,
        creator_id=3,
        source_vertical_id=1,
        responsible_vertical_id=1,
    )
    assign(harness.database, task, 10, agreement="AWAITING")

    body = harness.call(
        "GET", "/dashboard/ah", headers=_headers(harness, AH_ROLL)
    ).json()
    created = {item["task"]["id"]: item for item in body["created_tasks"]}
    assert set(created) == {task}
    assignee = created[task]["assignees"][0]
    assert assignee["person"]["id"] == 10
    assert assignee["person"]["phone"] == "ph-10"
    # The hierarchy alone would have hidden it: AH One does not see id 10.
    assert not _can_view(harness, _actor(3, "AH", (1,)), 10)


def test_exception_a_does_not_extend_to_a_non_creator(
    harness: ApiHarness,
) -> None:
    """A Head who sees the task but is not its creator gets no phone."""
    task = make_task(
        harness.database,
        creator_id=3,
        source_vertical_id=1,
        responsible_vertical_id=1,
    )
    assign(harness.database, task, 10, agreement="AWAITING")

    body = harness.call(
        "GET", "/dashboard/head", headers=_headers(harness, HEAD_ROLL)
    ).json()
    tasks = {item["task"]["id"]: item for item in body["tasks"]}
    assignee = tasks[task]["assignees"][0]
    assert assignee["person"]["id"] == 10
    assert "phone" not in assignee["person"]
    assert assignee["person"]["name"] == "Volunteer Three"


def test_exception_b_the_reviewer_sees_content_and_nobody_else_does(
    harness: ApiHarness,
) -> None:
    proposal = make_proposal(harness.database, sender_person_id=2)

    reviewer = harness.call(
        "GET", f"/proposals/{proposal}", headers=_headers(harness, HEAD_ROLL)
    )
    other = harness.call(
        "GET", f"/proposals/{proposal}", headers=_headers(harness, AH_ROLL)
    )

    assert reviewer.status == 200
    assert reviewer.json()["primary_message"]["content"] == "primary line"
    assert "context_messages" in reviewer.json()
    assert other.status == 403
    assert "primary_message" not in other.json()


def test_exception_c_the_approver_sees_description_and_note_not_the_chat(
    harness: ApiHarness,
) -> None:
    task = make_task(
        harness.database,
        creator_id=3,
        source_vertical_id=2,
        responsible_vertical_id=1,
        approval_state="PENDING",
    )
    request = make_approval(
        harness.database, task, requested_by=3, recipients=(2,)
    )

    response = harness.call(
        "GET", f"/approvals/{request}", headers=_headers(harness, HEAD_ROLL)
    )
    assert response.status == 200
    approval = response.json()["approval"]
    assert approval["task_description"] == "Arrange the generator"
    assert approval["request_message"] == "Please approve the cross-vertical task"
    assert "content" not in approval
    assert "context_messages" not in approval
    assert "phone" not in approval


def test_hidden_and_cancelled_tasks_never_reach_a_dashboard(
    harness: ApiHarness,
) -> None:
    live = make_task(harness.database, creator_id=3)
    assign(harness.database, live, 4, agreement="ACCEPTED")
    hidden = make_task(harness.database, creator_id=3, approval_state="PENDING")
    assign(harness.database, hidden, 4, agreement="ACCEPTED")
    cancelled = make_task(harness.database, creator_id=3, cancellation="CANCELLED")
    assign(harness.database, cancelled, 4, agreement="ACCEPTED")
    make_notification(harness.database, recipient_person_id=4, task_id=live)

    body = harness.call(
        "GET", "/dashboard/volunteer", headers=_headers(harness, VOLUNTEER_ROLL)
    ).json()
    own_ids = {task["id"] for task in body["tasks"]}
    assert live in own_ids
    assert hidden not in own_ids
    assert cancelled not in own_ids


@pytest.mark.parametrize(
    ("path", "roll", "subject_type"),
    [
        ("/dashboard/volunteer", AH_ROLL, "TASK"),
        ("/dashboard/ah", HEAD_ROLL, "TASK"),
        ("/dashboard/head", VOLUNTEER_ROLL, "TASK"),
        ("/dashboard/admin", VOLUNTEER_ROLL, "ADMIN"),
        ("/admin/backlog", HEAD_ROLL, "ADMIN"),
    ],
)
def test_a_role_cannot_read_another_roles_dashboard(
    harness: ApiHarness, path: str, roll: str, subject_type: str
) -> None:
    response = harness.call("GET", path, headers=_headers(harness, roll))
    assert response.status == 403
    rows = query(
        harness.database,
        "SELECT * FROM authorization_denial ORDER BY id DESC LIMIT 1",
    )
    assert rows
    assert rows[0]["reason"] == "NOT_ROLE"
    assert rows[0]["subject_type"] == subject_type


@pytest.mark.parametrize(
    "path",
    [
        "/dashboard/volunteer",
        "/dashboard/ah",
        "/dashboard/head",
        "/dashboard/admin",
        "/admin/backlog",
    ],
)
def test_an_admin_may_read_every_dashboard(harness: ApiHarness, path: str) -> None:
    response = harness.call("GET", path, headers=_headers(harness, ADMIN_ROLL))
    assert response.status == 200, (path, response.body)


def _keys(value: object) -> set[str]:
    """Every mapping key anywhere in one decoded JSON payload."""
    if isinstance(value, dict):
        return set(value) | {
            key for item in value.values() for key in _keys(item)
        }
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def test_a_dashboard_never_carries_message_content(harness: ApiHarness) -> None:
    make_proposal(harness.database, sender_person_id=2)
    for path, roll in (
        ("/dashboard/volunteer", VOLUNTEER_ROLL),
        ("/dashboard/ah", AH_ROLL),
        ("/dashboard/head", HEAD_ROLL),
    ):
        body = harness.call("GET", path, headers=_headers(harness, roll)).json()
        keys = _keys(body)
        assert "content" not in keys, path
        assert "primary_message" not in keys, path
        assert "context_messages" not in keys, path
