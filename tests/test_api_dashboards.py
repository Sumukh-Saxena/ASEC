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
    WORLD_AT,
    ApiHarness,
    assign,
    build_harness,
    make_approval,
    make_notification,
    make_proposal,
    make_task,
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


def test_admin_dashboard_is_a_festival_glance_without_internal_ledgers(
    harness: ApiHarness,
) -> None:
    """Owner decision B/D1: cumulative counts only, no attempt/denial/config data."""
    _seed_backlog(harness)
    _execute(
        harness.database,
        "INSERT INTO bucket (id, group_id, start_event_timestamp,"
        " end_event_timestamp, processing_state) VALUES"
        " (3, 1, ?, ?, 'CLOSED'), (4, 1, ?, ?, 'FAILED'),"
        " (5, 2, ?, ?, 'AWAITING_RECOVERY')",
        (WORLD_AT, WORLD_AT, WORLD_AT, WORLD_AT, WORLD_AT, WORLD_AT),
    )

    completed = make_task(harness.database, creator_id=3)
    in_progress = make_task(
        harness.database, creator_id=3, responsible_vertical_id=2
    )
    idle = make_task(harness.database, creator_id=3)
    assign(harness.database, completed, 4, work_status="COMPLETED")
    assign(harness.database, in_progress, 4, work_status="IN_PROGRESS")
    assign(
        harness.database, in_progress, 7, agreement="ACCEPTED",
        work_status="NOT_STARTED",
    )
    assign(harness.database, idle, 4, agreement="AWAITING")

    make_proposal(harness.database, sender_person_id=3)
    accepted = make_proposal(harness.database, sender_person_id=3)
    rejected = make_proposal(harness.database, sender_person_id=3)
    edited = make_proposal(harness.database, sender_person_id=3)
    _execute(
        harness.database,
        "UPDATE proposal SET review_state = 'ACCEPTED' WHERE id = ?",
        (accepted,),
    )
    _execute(
        harness.database,
        "UPDATE proposal SET review_state = 'REJECTED' WHERE id = ?",
        (rejected,),
    )
    _execute(
        harness.database,
        "UPDATE proposal SET review_state = 'EDITED' WHERE id = ?",
        (edited,),
    )

    for index, state in enumerate(
        ("PENDING", "SENDING", "SENT", "FAILED", "EXHAUSTED", "EXHAUSTED"),
        start=1,
    ):
        notification_id = make_notification(
            harness.database, task_id=completed, dedupe_key=f"n{index}"
        )
        _execute(
            harness.database,
            "UPDATE notification SET delivery_state = ? WHERE id = ?",
            (state, notification_id),
        )
    _execute(
        harness.database,
        "INSERT INTO notification_attempt (notification_id, attempt_number,"
        " outcome, recorded_at) VALUES (5, 1, 'NO_ENDPOINT', ?),"
        " (6, 1, 'RENDER_ERROR', ?)",
        (WORLD_AT, WORLD_AT),
    )

    _execute(
        harness.database,
        "INSERT INTO processing_attempt (id, stage, bucket_id, started_at,"
        " finished_at, outcome, model_id, model_version, config_version)"
        " VALUES (900, 'RELEVANCE_CLASSIFICATION', 1, ?, ?, 'SUCCEEDED',"
        " 'model-r', '1.0.0', 'prompt-1'),"
        " (901, 'RELEVANCE_FALLBACK_CLASSIFICATION', 2, ?, ?,"
        " 'FAILED_RETRYABLE', NULL, NULL, NULL)",
        (WORLD_AT, WORLD_AT, WORLD_AT, WORLD_AT),
    )
    _execute(
        harness.database,
        "INSERT INTO relevance_decision (bucket_id, message_id, attempt_id,"
        " decision, model_id, model_version, prompt_version, decided_at)"
        " VALUES (1, 1, 900, 'RELEVANT', 'model-r', '1.0.0', 'prompt-1', ?),"
        " (1, 2, 900, 'IRRELEVANT', 'model-r', '1.0.0', 'prompt-1', ?)",
        (WORLD_AT, WORLD_AT),
    )
    _execute(
        harness.database,
        "UPDATE source_message SET flag_unclassified = 1 WHERE id = 3",
    )
    _execute(
        harness.database,
        "UPDATE source_message SET received_at = '2026-11-13T08:00:00+00:00'"
        " WHERE id = 1",
    )

    forbidden = harness.call(
        "GET", "/dashboard/admin", headers=_headers(harness, VOLUNTEER_ROLL)
    )
    assert forbidden.status == 403

    body = harness.call(
        "GET", "/dashboard/admin", headers=_headers(harness, ADMIN_ROLL)
    ).json()

    assert body["messages"] == {"total": 7, "last_24h": 1}
    assert body["buckets"] == {
        "OPEN": 1,
        "CLOSED": 1,
        "PROCESSED": 1,
        "FAILED": 1,
        "AWAITING_RECOVERY": 1,
    }
    assert body["relevance"] == {
        "relevant": 1,
        "irrelevant": 1,
        "failed": 1,
        "unclassified": 1,
    }
    assert body["proposals"] == {
        "pending_review": 5,
        "accepted": 1,
        "rejected": 1,
    }
    assert body["tasks"]["total"] == 3
    assert body["tasks"]["by_status"] == {
        "NOT_STARTED": 1,
        "IN_PROGRESS": 1,
        "COMPLETED": 1,
    }
    assert body["tasks"]["by_vertical"] == [
        {
            "vertical_id": 1,
            "total": 2,
            "by_status": {
                "NOT_STARTED": 1,
                "IN_PROGRESS": 0,
                "COMPLETED": 1,
            },
        },
        {
            "vertical_id": 2,
            "total": 1,
            "by_status": {
                "NOT_STARTED": 0,
                "IN_PROGRESS": 1,
                "COMPLETED": 0,
            },
        },
    ]
    assert body["notifications"] == {
        "pending": 2,
        "delivered": 1,
        "retrying": 1,
        "exhausted": 1,
        "no_endpoint": 1,
    }
    assert body["backlog"] == {
        "unprocessed_buckets": 4,
        "oldest_age_seconds": 120,
    }
    assert body["system"] == {
        "processing_paused": False,
        "delivery_paused": False,
    }
    for removed in ("attempts", "denials", "evaluation", "config"):
        assert removed not in body
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


# ---------------------------------------------------------------------------
# Count badges, attention counts and the list cap (owner decision E, D4)
# ---------------------------------------------------------------------------


def test_volunteer_dashboard_reports_counts_attention_and_has_more(
    harness: ApiHarness,
) -> None:
    active = make_task(harness.database, creator_id=3)
    assign(
        harness.database, active, 4, agreement="ACCEPTED", work_status="IN_PROGRESS"
    )
    finished = make_task(harness.database, creator_id=3)
    assign(
        harness.database, finished, 4, agreement="ACCEPTED", work_status="COMPLETED"
    )
    claimable = make_task(
        harness.database,
        creator_id=3,
        assignment_mode="OPEN",
        people_needed_unlimited=1,
    )
    read = make_notification(
        harness.database,
        recipient_person_id=4,
        task_id=active,
        dedupe_key="d4-read",
    )
    make_notification(
        harness.database,
        recipient_person_id=4,
        task_id=active,
        dedupe_key="d4-unread",
    )
    _execute(
        harness.database,
        "INSERT INTO notification_read (notification_id, read_at) VALUES (?, ?)",
        (read, NOW),
    )

    body = harness.call(
        "GET", "/dashboard/volunteer", headers=_headers(harness, VOLUNTEER_ROLL)
    ).json()

    assert body["counts"] == {"tasks": 2, "open_tasks": 1, "notifications": 2}
    assert body["attention"] == {
        "unread_notifications": 1,
        "open_tasks": 1,
        "active_tasks": 1,
    }
    assert body["has_more"] == {
        "tasks": False,
        "open_tasks": False,
        "notifications": False,
    }
    assert [task["id"] for task in body["open_tasks"]] == [claimable]
    assert len(body["tasks"]) == 2
    assert len(body["notifications"]) == 2


def test_ah_dashboard_reports_counts_attention_and_has_more(
    harness: ApiHarness,
) -> None:
    make_proposal(harness.database, sender_person_id=3)
    created = make_task(harness.database, creator_id=3)
    assign(
        harness.database, created, 4, agreement="ACCEPTED", work_status="IN_PROGRESS"
    )
    approval_task = make_task(
        harness.database, creator_id=3, approval_state="PENDING"
    )
    make_approval(
        harness.database, approval_task, requested_by=3, recipients=(3,)
    )
    _settle_tasks(harness, created, approval_task)

    body = harness.call(
        "GET", "/dashboard/ah", headers=_headers(harness, AH_ROLL)
    ).json()

    assert body["counts"] == {
        "review_queue": 1,
        "created_tasks": 1,
        "volunteers": 2,
        "approvals": 1,
    }
    assert body["attention"] == {
        "pending_review": 1,
        "approvals": 1,
        "active_created_tasks": 1,
    }
    assert body["has_more"] == {
        "review_queue": False,
        "created_tasks": False,
        "volunteers": False,
        "approvals": False,
    }


def test_head_dashboard_reports_counts_attention_and_has_more(
    harness: ApiHarness,
) -> None:
    task = make_task(harness.database, creator_id=3)
    assign(harness.database, task, 4, agreement="ACCEPTED")

    body = harness.call(
        "GET", "/dashboard/head", headers=_headers(harness, HEAD_ROLL)
    ).json()

    for name in (
        "review_queue",
        "tasks",
        "people",
        "approvals",
        "cross_vertical_tasks",
    ):
        assert body["has_more"][name] is False
        assert body["counts"][name] == len(body[name]), name
    assert set(body["attention"]) == {
        "pending_review",
        "approvals",
        "active_tasks",
    }
    assert body["attention"]["active_tasks"] == 1


def test_the_dashboard_cap_limits_each_list_and_keeps_the_totals(
    tmp_path: Path,
) -> None:
    built = build_harness(
        tmp_path, config=Config({"API_DASHBOARD_LIST_LIMIT": "1"})
    )
    seed_world(built.database)
    seed_team(built.database)
    first = make_task(built.database, creator_id=3)
    assign(built.database, first, 4, agreement="ACCEPTED")
    second = make_task(built.database, creator_id=3)
    assign(built.database, second, 4, agreement="ACCEPTED")
    make_notification(
        built.database, recipient_person_id=4, task_id=first, dedupe_key="d4-cap-1"
    )
    make_notification(
        built.database, recipient_person_id=4, task_id=second, dedupe_key="d4-cap-2"
    )

    body = built.call(
        "GET", "/dashboard/volunteer", headers=_headers(built, VOLUNTEER_ROLL)
    ).json()

    assert len(body["tasks"]) == 1
    assert len(body["notifications"]) == 1
    assert body["counts"] == {"tasks": 2, "open_tasks": 0, "notifications": 2}
    assert body["has_more"] == {
        "tasks": True,
        "open_tasks": False,
        "notifications": True,
    }
