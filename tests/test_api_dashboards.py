"""Dashboard payloads for each role (W6; FR-UI-02..05, NFR-REL-06).

Each dashboard is one read-only projection of state the role could already
reach through the task, approval and notification routes; the tests pin the
contents the SRS names, the visibility gate on people, and that hidden or
cancelled tasks never appear. The Admin backlog is checked against the
orchestration service's own backlog reading, and against literal count/age
values seeded in the fixture, because NFR-REL-06 needs both.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend.config import Config  # noqa: E402
from tests.api_fixtures import (  # noqa: E402
    ADMIN_ROLL,
    AH_ROLL,
    HEAD_ROLL,
    NOW,
    VOLUNTEER_ROLL,
    ApiHarness,
    assign,
    build_harness,
    count,
    make_approval,
    make_notification,
    make_proposal,
    make_task,
    query,
    seed_team,
    seed_world,
)

CONFIG_VALUE = "synthetic-config-value-xyz"
CONFIG = Config({"API_PORT": CONFIG_VALUE, "DISCORD_BOT_TOKEN": CONFIG_VALUE})


@pytest.fixture()
def harness(tmp_path: Path) -> ApiHarness:
    built = build_harness(tmp_path, config=CONFIG)
    seed_world(built.database)
    seed_team(built.database)
    return built


def _headers(harness: ApiHarness, roll: str) -> dict[str, str]:
    return {"authorization": f"Bearer {harness.sign_in(roll)}"}


def _execute(
    database: Path, sql: str, parameters: tuple[object, ...] = ()
) -> None:
    """Seed one row/state directly; fixtures only, never the API."""
    connection = sqlite3.connect(database)
    try:
        with connection:
            connection.execute(sql, parameters)
    finally:
        connection.close()


def _settle_tasks(harness: ApiHarness, *task_ids: int) -> None:
    """Mark a fixture task's proposal accepted, as real acceptance would.

    ``make_task`` inserts the task directly, so its proposal would otherwise
    still look pending and appear in the reviewer queue of whoever sent the
    source message. That is a fixture artifact, not a dashboard rule.
    """
    for task_id in task_ids:
        _execute(
            harness.database,
            "UPDATE proposal SET review_state = 'ACCEPTED'"
            " WHERE id = (SELECT proposal_id FROM task WHERE id = ?)",
            (task_id,),
        )


def _seed_backlog(harness: ApiHarness) -> None:
    """One unprocessed bucket dated 120 s before accepted progress."""
    _execute(
        harness.database,
        "UPDATE bucket SET processing_state = 'PROCESSED' WHERE id = 2",
    )
    _execute(
        harness.database,
        "INSERT INTO source_group_progress (group_id, progress_to, updated_at)"
        " VALUES (1, '2026-11-01T10:00:00+00:00', ?)",
        (NOW,),
    )


# ---------------------------------------------------------------------------
# Volunteer (FR-UI-02)
# ---------------------------------------------------------------------------


def test_volunteer_dashboard_has_own_tasks_open_tasks_and_notifications(
    harness: ApiHarness,
) -> None:
    own = make_task(harness.database, creator_id=3)
    assign(harness.database, own, 4, agreement="ACCEPTED")
    claimable = make_task(
        harness.database,
        creator_id=3,
        assignment_mode="OPEN",
        people_needed_unlimited=1,
    )
    other_vertical = make_task(
        harness.database,
        creator_id=8,
        source_vertical_id=2,
        responsible_vertical_id=2,
        assignment_mode="OPEN",
        people_needed_unlimited=1,
    )
    notification = make_notification(
        harness.database, recipient_person_id=4, task_id=own
    )
    make_notification(harness.database, recipient_person_id=7, task_id=own)

    body = harness.call(
        "GET", "/dashboard/volunteer", headers=_headers(harness, VOLUNTEER_ROLL)
    ).json()

    assert {task["id"] for task in body["tasks"]} == {own}
    assert {task["id"] for task in body["open_tasks"]} == {claimable}
    assert other_vertical not in {task["id"] for task in body["open_tasks"]}
    assert [item["notification_id"] for item in body["notifications"]] == [
        notification
    ]


# ---------------------------------------------------------------------------
# AH (FR-UI-03)
# ---------------------------------------------------------------------------


def test_ah_dashboard_has_review_queue_created_tasks_people_and_approvals(
    harness: ApiHarness,
) -> None:
    proposal = make_proposal(harness.database, sender_person_id=3)
    make_proposal(harness.database, sender_person_id=9)

    created = make_task(harness.database, creator_id=3)
    assign(harness.database, created, 4, agreement="ACCEPTED", work_status="IN_PROGRESS")
    assign(harness.database, created, 7, agreement="AWAITING")
    make_task(harness.database, creator_id=2)  # not an AH-created task

    approval_task = make_task(
        harness.database,
        creator_id=3,
        source_vertical_id=2,
        responsible_vertical_id=1,
        approval_state="PENDING",
    )
    request = make_approval(
        harness.database, approval_task, requested_by=3, recipients=(3,)
    )
    other_task = make_task(
        harness.database,
        creator_id=9,
        source_vertical_id=2,
        responsible_vertical_id=1,
        approval_state="PENDING",
    )
    make_approval(harness.database, other_task, requested_by=9, recipients=(9,))
    _settle_tasks(harness, created, approval_task)

    body = harness.call(
        "GET", "/dashboard/ah", headers=_headers(harness, AH_ROLL)
    ).json()

    assert [item["proposal_id"] for item in body["review_queue"]] == [proposal]
    created_tasks = {item["task"]["id"]: item for item in body["created_tasks"]}
    assert set(created_tasks) == {created}
    assignees = {
        item["person"]["id"]: item for item in created_tasks[created]["assignees"]
    }
    assert set(assignees) == {4, 7}
    assert assignees[4]["work_status"] == "IN_PROGRESS"
    assert assignees[7]["agreement"] == "AWAITING"
    assert {person["id"] for person in body["volunteers"]} == {4, 7}
    assert [item["request_id"] for item in body["approvals"]] == [request]


# ---------------------------------------------------------------------------
# Head (FR-UI-04)
# ---------------------------------------------------------------------------


def test_head_dashboard_has_the_vertical_and_affected_cross_vertical_tasks(
    harness: ApiHarness,
) -> None:
    in_vertical = make_task(harness.database, creator_id=3)
    cross = make_task(
        harness.database,
        creator_id=3,
        source_vertical_id=1,
        responsible_vertical_id=2,
    )
    other_vertical = make_task(
        harness.database,
        creator_id=8,
        source_vertical_id=2,
        responsible_vertical_id=2,
    )
    hidden = make_task(
        harness.database, creator_id=2, approval_state="PENDING"
    )
    proposal = make_proposal(harness.database, sender_person_id=2)
    make_proposal(harness.database, sender_person_id=8)

    approval_task = make_task(
        harness.database,
        creator_id=3,
        source_vertical_id=2,
        responsible_vertical_id=1,
        approval_state="PENDING",
    )
    request = make_approval(
        harness.database, approval_task, requested_by=3, recipients=(2,)
    )
    _settle_tasks(
        harness, in_vertical, cross, other_vertical, hidden, approval_task
    )

    body = harness.call(
        "GET", "/dashboard/head", headers=_headers(harness, HEAD_ROLL)
    ).json()

    task_ids = {item["task"]["id"] for item in body["tasks"]}
    assert {in_vertical, cross} <= task_ids
    assert other_vertical not in task_ids
    assert hidden not in task_ids
    assert {item["id"] for item in body["cross_vertical_tasks"]} == {cross}
    assert {person["id"] for person in body["people"]} == {3, 4, 6, 7, 9, 11}
    assert [item["proposal_id"] for item in body["review_queue"]] == [proposal]
    assert [item["request_id"] for item in body["approvals"]] == [request]


# ---------------------------------------------------------------------------
# Admin (FR-UI-05, NFR-REL-06)
# ---------------------------------------------------------------------------


def test_admin_dashboard_has_backlog_attempts_denials_evaluation_and_config_names(
    harness: ApiHarness,
) -> None:
    _seed_backlog(harness)
    make_task(harness.database, creator_id=3)
    labelled = make_proposal(harness.database, sender_person_id=3)
    make_proposal(harness.database, sender_person_id=3)
    _execute(
        harness.database,
        "UPDATE proposal SET review_state = 'REJECTED' WHERE id = ?",
        (labelled,),
    )

    forbidden = harness.call(
        "GET", "/dashboard/admin", headers=_headers(harness, VOLUNTEER_ROLL)
    )
    assert forbidden.status == 403

    body = harness.call(
        "GET", "/dashboard/admin", headers=_headers(harness, ADMIN_ROLL)
    ).json()

    assert body["backlog"]["unprocessed_buckets"] == 1
    assert body["backlog"]["unassigned_messages"] == 0
    assert body["backlog"]["oldest_age_seconds"] == 120
    groups = {group["group_id"]: group for group in body["backlog"]["groups"]}
    assert groups[1]["unprocessed_buckets"] == 1
    assert groups[1]["oldest_unprocessed_bucket_id"] == 1
    assert groups[1]["age_event_seconds"] == 120
    assert groups[2]["unprocessed_buckets"] == 0
    assert groups[2]["age_event_seconds"] is None

    outcomes = {
        row["outcome"]: int(row["n"])
        for row in query(
            harness.database,
            "SELECT outcome, COUNT(*) AS n FROM processing_attempt GROUP BY outcome",
        )
    }
    assert body["attempts"]["total"] == sum(outcomes.values())
    assert body["attempts"]["by_outcome"] == outcomes

    assert body["denials"]["total"] == count(
        harness.database, "authorization_denial"
    )
    assert body["evaluation"] == {"available": True, "labelled_proposals": 1}
    assert body["config"]["keys"] == ["API_PORT", "DISCORD_BOT_TOKEN"]
    assert CONFIG_VALUE not in str(body)


def test_admin_backlog_reports_the_oldest_unprocessed_bucket(
    harness: ApiHarness,
) -> None:
    _seed_backlog(harness)

    body = harness.call(
        "GET", "/admin/backlog", headers=_headers(harness, ADMIN_ROLL)
    ).json()

    assert body["backlog"]["unprocessed_buckets"] == 1
    assert body["backlog"]["oldest_age_seconds"] == 120
    assert body["backlog"]["groups"][0]["oldest_unprocessed_anchor"] == (
        "2026-11-01T09:58:00+00:00"
    )


def test_an_empty_backlog_reports_zero_count_and_no_age(
    harness: ApiHarness,
) -> None:
    _execute(
        harness.database,
        "UPDATE bucket SET processing_state = 'PROCESSED'",
    )

    body = harness.call(
        "GET", "/admin/backlog", headers=_headers(harness, ADMIN_ROLL)
    ).json()

    assert body["backlog"]["unprocessed_buckets"] == 0
    assert body["backlog"]["oldest_age_seconds"] is None


def test_dashboard_people_never_include_an_inactive_or_other_vertical_person(
    harness: ApiHarness,
) -> None:
    body = harness.call(
        "GET", "/dashboard/head", headers=_headers(harness, HEAD_ROLL)
    ).json()
    ids = {person["id"] for person in body["people"]}
    assert 5 not in ids  # deactivated
    assert 8 not in ids  # vertical 2
    assert 10 not in ids  # vertical 2


def test_dashboard_task_reads_stay_within_visible_tasks(
    harness: ApiHarness,
) -> None:
    """Every dashboard task id is also visible through ``/tasks`` (NFR-SEC-03)."""
    task = make_task(harness.database, creator_id=3)
    assign(harness.database, task, 4, agreement="ACCEPTED")

    visible = harness.call(
        "GET", "/tasks", headers=_headers(harness, VOLUNTEER_ROLL)
    ).json()["tasks"]

    body = harness.call(
        "GET", "/dashboard/volunteer", headers=_headers(harness, VOLUNTEER_ROLL)
    ).json()

    exposed = {item["id"] for item in visible}
    assert {item["id"] for item in body["tasks"]} <= exposed
    assert {item["id"] for item in body["open_tasks"]} <= exposed
