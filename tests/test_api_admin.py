"""Admin read routes (W4; FR-AUT-01/04, FR-UI-05, NFR-REL-06).

These routes are Admin-only at the matrix level, record a denial before the 403,
and return read-model payloads only: no configuration value, no message content
and no roster personal data. ``/admin/config`` is the redacted public view.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend.config import Config  # noqa: E402
from tests.api_fixtures import (  # noqa: E402
    ADMIN_ROLL,
    AH_ROLL,
    VOLUNTEER_ROLL,
    ApiHarness,
    build_harness,
    make_proposal,
    make_task,
    query,
    seed_world,
)

SECRET_VALUE = "not-a-real-discord-token-0000"


@pytest.fixture()
def harness(tmp_path: Path) -> ApiHarness:
    built = build_harness(tmp_path)
    seed_world(built.database)
    return built


def _headers(harness: ApiHarness, roll: str) -> dict[str, str]:
    return {"authorization": f"Bearer {harness.sign_in(roll)}"}


@pytest.mark.parametrize(
    "path",
    [
        "/admin/denials",
        "/admin/attempts",
        "/admin/config",
        "/admin/evaluation/export",
    ],
)
def test_a_non_admin_is_a_recorded_403(harness: ApiHarness, path: str) -> None:
    headers = _headers(harness, AH_ROLL)

    response = harness.call("GET", path, headers=headers)

    assert response.status == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    rows = query(
        harness.database,
        "SELECT * FROM authorization_denial WHERE actor_person_id = 3"
        " AND reason = 'NOT_ROLE'",
    )
    assert len(rows) == 1


def test_an_admin_reads_the_denial_ledger(harness: ApiHarness) -> None:
    # Cause one recorded denial through a real route first.
    harness.call("GET", "/approvals", headers=_headers(harness, VOLUNTEER_ROLL))

    response = harness.call(
        "GET", "/admin/denials", headers=_headers(harness, ADMIN_ROLL)
    )

    assert response.status == 200
    body = response.json()
    assert body["limit"] >= 1
    operations = [row["operation"] for row in body["denials"]]
    assert "APPROVAL_LIST" in operations
    first = body["denials"][0]
    assert set(first) == {
        "id",
        "actor_person_id",
        "operation",
        "subject_type",
        "subject_id",
        "reason",
        "attempted_at",
    }


def test_an_admin_reads_the_processing_attempts(harness: ApiHarness) -> None:
    make_task(harness.database)

    response = harness.call(
        "GET", "/admin/attempts", headers=_headers(harness, ADMIN_ROLL)
    )

    assert response.status == 200
    body = response.json()
    stages = [row["stage"] for row in body["attempts"]]
    assert "proposal_generation" in stages
    assert set(body["attempts"][0]) == {
        "id",
        "stage",
        "bucket_id",
        "model_id",
        "model_version",
        "config_version",
        "started_at",
        "finished_at",
        "outcome",
        "has_error",
    }


def test_config_is_redacted_and_never_leaks_a_value(tmp_path: Path) -> None:
    built = build_harness(tmp_path, config=Config({"DISCORD_BOT_TOKEN": SECRET_VALUE}))
    seed_world(built.database)

    response = built.call(
        "GET", "/admin/config", headers=_headers(built, ADMIN_ROLL)
    )

    assert response.status == 200
    payload = response.json()["config"]
    assert payload["DISCORD_BOT_TOKEN"] == "<redacted>"
    assert set(payload.values()) == {"<redacted>"}
    assert SECRET_VALUE not in response.body.decode("utf-8")


def test_a_bad_admin_page_is_a_422(harness: ApiHarness) -> None:
    headers = _headers(harness, ADMIN_ROLL)
    for query_string in ("limit=0", "limit=-1", "limit=99999"):
        response = harness.call(
            "GET", f"/admin/denials?{query_string}", headers=headers
        )
        assert response.status == 422
        assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_the_evaluation_export_carries_a_rejected_proposal(
    harness: ApiHarness,
) -> None:
    proposal_id = make_proposal(
        harness.database,
        sender_person_id=3,
        assignees=(("Volunteer One", "RESOLVED", 4, "MENTION"),),
    )
    rejected = harness.call(
        "POST",
        f"/proposals/{proposal_id}/reject",
        headers=_headers(harness, AH_ROLL),
        json_body={"note": "Not needed"},
    )
    assert rejected.status == 200

    response = harness.call(
        "GET", "/admin/evaluation/export", headers=_headers(harness, ADMIN_ROLL)
    )

    assert response.status == 200
    proposals = response.json()["proposals"]
    exported = next(
        item for item in proposals if item["proposal_id"] == proposal_id
    )
    assert exported["review_state"] == "REJECTED"
    assert exported["decision"] == "REJECTED"
    assert set(exported) == {
        "proposal_id",
        "ptype",
        "review_state",
        "decision",
        "decided_by",
        "decided_at",
        "note",
        "revisions",
        "generated_final_delta",
    }


def test_identity_headers_never_grant_admin(harness: ApiHarness) -> None:
    token = harness.sign_in(VOLUNTEER_ROLL)
    response = harness.call(
        "GET",
        "/admin/denials",
        headers={
            "authorization": f"Bearer {token}",
            "x-person": "1",
            "x-role": "Admin",
        },
    )
    assert response.status == 403
