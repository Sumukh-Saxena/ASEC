"""Proposal review routes over the existing review service (W3; FR-REV-*).

The reviewer view is the one place message content leaves the backend (Phase 6
reviewer view, NFR-SEC-03 exception (b)). Authoring, accepting and rejecting
call the existing service and keep its denial recorder.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from tests.api_fixtures import (  # noqa: E402
    AH_ROLL,
    VOLUNTEER_ROLL,
    ApiHarness,
    build_harness,
    count,
    make_proposal,
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


def _proposal(harness: ApiHarness) -> int:
    return make_proposal(
        harness.database,
        sender_person_id=3,
        assignees=(("Volunteer One", "RESOLVED", 4, "MENTION"),),
    )


def test_reviewer_sees_the_phase_six_view_with_message_content(
    harness: ApiHarness,
) -> None:
    proposal_id = _proposal(harness)
    headers = _headers(harness, AH_ROLL)

    response = harness.call("GET", f"/proposals/{proposal_id}", headers=headers)

    assert response.status == 200
    body = response.json()
    assert body["proposal_id"] == proposal_id
    assert body["primary_message"]["content"] == "primary line"
    assert body["draft"]["description"] == "Arrange the generator"
    assert body["draft"]["assignees"][0]["person_id"] == 4


def test_a_non_reviewer_cannot_read_and_the_denial_is_recorded(
    harness: ApiHarness,
) -> None:
    proposal_id = _proposal(harness)
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call("GET", f"/proposals/{proposal_id}", headers=headers)

    assert response.status == 403
    rows = _denials(harness.database, "PROPOSAL_VIEW")
    assert len(rows) == 1
    assert rows[0]["actor_person_id"] == 4
    assert rows[0]["subject_type"] == "PROPOSAL"
    assert rows[0]["subject_id"] == proposal_id
    assert rows[0]["reason"] == "NOT_ROLE"


def test_a_missing_proposal_is_404(harness: ApiHarness) -> None:
    headers = _headers(harness, AH_ROLL)
    response = harness.call("GET", "/proposals/9999", headers=headers)
    assert response.status == 404


def test_an_edit_appends_a_revision_for_the_reviewer(harness: ApiHarness) -> None:
    proposal_id = _proposal(harness)
    headers = _headers(harness, AH_ROLL)

    response = harness.call(
        "POST",
        f"/proposals/{proposal_id}/edits",
        headers=headers,
        json_body={"expected_revision_number": 0, "description": "Bring a generator"},
    )

    assert response.status == 200
    body = response.json()
    assert body["revision_number"] == 1
    assert body["changed_fields"] == ["description"]
    assert count(
        harness.database, "proposal_revision", "proposal_id = ?", (proposal_id,)
    ) == 1


def test_an_edit_by_a_non_reviewer_is_a_recorded_403(
    harness: ApiHarness,
) -> None:
    proposal_id = _proposal(harness)
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call(
        "POST",
        f"/proposals/{proposal_id}/edits",
        headers=headers,
        json_body={"expected_revision_number": 0, "description": "Mine"},
    )

    assert response.status == 403
    assert _denials(harness.database, "PROPOSAL_EDIT")[0]["reason"] == "NOT_ROLE"


def test_accepting_materializes_one_task(harness: ApiHarness) -> None:
    proposal_id = _proposal(harness)
    headers = _headers(harness, AH_ROLL)

    response = harness.call(
        "POST",
        f"/proposals/{proposal_id}/accept",
        headers=headers,
        json_body={"expected_revision_number": 0},
    )

    assert response.status == 200
    body = response.json()
    assert body["created"] is True
    assert body["ptype"] == "TASK"
    assert count(
        harness.database, "task", "proposal_id = ?", (proposal_id,)
    ) == 1
    states = query(
        harness.database, "SELECT review_state FROM proposal WHERE id = ?",
        (proposal_id,),
    )
    assert states[0]["review_state"] == "ACCEPTED"


def test_accepting_by_a_non_reviewer_is_a_recorded_403(
    harness: ApiHarness,
) -> None:
    proposal_id = _proposal(harness)
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call(
        "POST",
        f"/proposals/{proposal_id}/accept",
        headers=headers,
        json_body={"expected_revision_number": 0},
    )

    assert response.status == 403
    assert _denials(harness.database, "PROPOSAL_ACCEPT")[0]["reason"] == "NOT_ROLE"
    assert count(harness.database, "task") == 0


def test_rejecting_keeps_the_proposal_and_its_revisions(
    harness: ApiHarness,
) -> None:
    proposal_id = _proposal(harness)
    headers = _headers(harness, AH_ROLL)

    response = harness.call(
        "POST",
        f"/proposals/{proposal_id}/reject",
        headers=headers,
        json_body={"note": "Not needed"},
    )

    assert response.status == 200
    body = response.json()
    assert body["created"] is True
    assert body["note"] == "Not needed"
    state = query(
        harness.database, "SELECT review_state FROM proposal WHERE id = ?",
        (proposal_id,),
    )[0]["review_state"]
    assert state == "REJECTED"


def test_rejecting_by_a_non_reviewer_is_a_recorded_403(
    harness: ApiHarness,
) -> None:
    proposal_id = _proposal(harness)
    headers = _headers(harness, VOLUNTEER_ROLL)

    response = harness.call(
        "POST",
        f"/proposals/{proposal_id}/reject",
        headers=headers,
        json_body={"note": "No"},
    )

    assert response.status == 403
    assert _denials(harness.database, "PROPOSAL_REJECT")[0]["reason"] == "NOT_ROLE"
    assert query(
        harness.database, "SELECT review_state FROM proposal WHERE id = ?",
        (proposal_id,),
    )[0]["review_state"] == "PENDING"


def test_review_responses_never_include_phone_numbers(harness: ApiHarness) -> None:
    proposal_id = _proposal(harness)
    headers = _headers(harness, AH_ROLL)
    response = harness.call("GET", f"/proposals/{proposal_id}", headers=headers)
    assert response.status == 200
    text = response.body.decode("utf-8")
    assert "ph-" not in text
    assert "phone" not in text


def test_identity_headers_never_make_a_volunteer_a_reviewer(
    harness: ApiHarness,
) -> None:
    proposal_id = _proposal(harness)
    token = harness.sign_in(VOLUNTEER_ROLL)
    response = harness.call(
        "GET",
        f"/proposals/{proposal_id}",
        headers={
            "authorization": f"Bearer {token}",
            "x-person": "3",
            "x-role": "Admin",
        },
    )
    assert response.status == 403
    assert query(
        harness.database, "SELECT review_state FROM proposal WHERE id = ?",
        (proposal_id,),
    )[0]["review_state"] == "PENDING"
