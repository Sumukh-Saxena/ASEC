"""The five API routes: sign-in, session use and sign-out (W2).

Covers FR-ROS-03 (roll-number sign-in), FR-AUT-01 (the service authorizes),
FR-AUT-05 (current role and verticals from the roster on every request) and
FR-AUT-07 (deactivation ends the session on the next request), plus
NFR-SEC-01 for the response shapes: a 202 never reveals whether a roll number
exists, and a token appears only in the one verify response that issues it.

Every roll number, Discord id and code here is synthetic. The code is read from
the recording sender, never printed, and never asserted against a log or a row.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from backend.config import Config  # noqa: E402
from backend.notification_delivery import RecordingOtpSender  # noqa: E402
from tests.api_fixtures import (  # noqa: E402
    INACTIVE_ROLL,
    NO_DISCORD_ROLL,
    UNKNOWN_ROLL,
    VOLUNTEER_DISCORD,
    VOLUNTEER_ROLL,
    ApiHarness,
    Clock,
    build_app,
    build_harness,
    call,
    seed_database,
)

BASE = "/auth"


@pytest.fixture()
def harness(tmp_path: Path) -> ApiHarness:
    return build_harness(tmp_path)


def _attempts(database: Path, kind: str) -> list[str]:
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT reason FROM auth_attempt WHERE kind = ? ORDER BY id", (kind,)
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        connection.close()


def _wrong_code(correct: str) -> str:
    """A well-formed code that is guaranteed not to be the issued one."""
    return "000000" if correct != "000000" else "111111"


def _schema_routes(app: Any) -> dict[str, tuple[str, ...]]:
    """The route table as the app itself describes it (no private internals)."""
    schema = app.openapi()
    return {
        str(path): tuple(sorted(str(name) for name in operations))
        for path, operations in schema.get("paths", {}).items()
    }


def test_route_table_is_exactly_the_classified_routes(
    harness: ApiHarness,
) -> None:
    """No docs, no OpenAPI, no debug route: every path has a matrix row."""
    schema = _schema_routes(harness.app)
    assert {
        "/healthz": ("get",),
        f"{BASE}/otp": ("post",),
        f"{BASE}/verify": ("post",),
        f"{BASE}/logout": ("post",),
        "/me": ("get",),
        "/tasks": ("get",),
        "/tasks/{task_id}": ("get", "patch"),
        "/tasks/{task_id}/claims": ("post",),
        "/tasks/{task_id}/agreement": ("post",),
        "/tasks/{task_id}/status": ("post",),
        "/tasks/{task_id}/reassign": ("post",),
        "/tasks/{task_id}/cancel": ("post",),
        "/proposals/{proposal_id}": ("get",),
        "/proposals/{proposal_id}/edits": ("post",),
        "/proposals/{proposal_id}/accept": ("post",),
        "/proposals/{proposal_id}/reject": ("post",),
        "/approvals": ("get",),
        "/approvals/{request_id}": ("get",),
        "/approvals/{request_id}/decision": ("post",),
        "/notifications": ("get",),
        "/notifications/{notification_id}/read": ("post",),
        "/push-endpoints": ("post",),
        "/push-endpoints/{endpoint_id}": ("delete",),
        "/admin/denials": ("get",),
        "/admin/attempts": ("get",),
        "/admin/config": ("get",),
        "/admin/evaluation/export": ("get",),
        "/admin/backlog": ("get",),
        "/dashboard/volunteer": ("get",),
        "/dashboard/ah": ("get",),
        "/dashboard/head": ("get",),
        "/dashboard/admin": ("get",),
    } == schema


def test_docs_redoc_and_openapi_are_not_served(harness: ApiHarness) -> None:
    """No unclassified route exists, including the framework's own pages."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = harness.call("GET", path)
        assert response.status == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


def test_healthz_is_public_and_never_touches_the_database(
    tmp_path: Path,
) -> None:
    """Liveness stays green even when no connection can be opened."""
    database = tmp_path / "api.db"
    seed_database(database)

    def refuse() -> sqlite3.Connection:
        raise AssertionError("healthz must not open a connection")

    app = build_app(
        database,
        clock=Clock(),
        sender=RecordingOtpSender(),
        connection_factory=refuse,
    )
    response = call(app, "GET", "/healthz")
    assert response.status == 200
    assert response.json() == {"status": "ok"}
    assert response.header("x-request-id")


def test_otp_request_is_indistinguishable_for_every_outcome(
    harness: ApiHarness,
) -> None:
    """Unknown, inactive, Discord-less and valid all get one identical 202."""
    responses = [
        harness.request_code(UNKNOWN_ROLL),
        harness.request_code(INACTIVE_ROLL),
        harness.request_code(NO_DISCORD_ROLL),
        harness.request_code(VOLUNTEER_ROLL),
    ]
    assert {response.status for response in responses} == {202}
    assert len({response.body for response in responses}) == 1
    assert responses[0].json() == {"status": "accepted"}

    # The difference exists only in the private ledger, never in the response.
    assert _attempts(harness.database, "OTP_REQUEST") == [
        "UNKNOWN_ROLL_NUMBER",
        "INACTIVE_PERSON",
        "NO_DISCORD_ID",
        "CHALLENGE_CREATED",
    ]
    assert _attempts(harness.database, "OTP_DELIVERY") == ["SENT"]


def test_otp_request_delivers_only_to_the_roster_discord_id(
    harness: ApiHarness,
) -> None:
    """O11-1: the code goes to the roster Discord id, and nowhere else."""
    response = harness.request_code(VOLUNTEER_ROLL)
    assert response.status == 202
    assert harness.sender.sent == ((VOLUNTEER_DISCORD, harness.last_code()),)


def test_otp_request_rate_limit_keeps_the_same_public_response(
    harness: ApiHarness,
) -> None:
    """The fourth request in the window is refused privately, still 202."""
    for _ in range(3):
        assert harness.request_code(VOLUNTEER_ROLL).status == 202
    sent_before = len(harness.sender.sent)
    response = harness.request_code(VOLUNTEER_ROLL)
    assert response.status == 202
    assert response.json() == {"status": "accepted"}
    assert len(harness.sender.sent) == sent_before
    assert _attempts(harness.database, "OTP_REQUEST")[-1] == "RATE_LIMITED"


@pytest.mark.parametrize("roll_number", ["", "   "])
def test_otp_request_rejects_an_empty_roll_number(
    harness: ApiHarness, roll_number: str
) -> None:
    """An empty or whitespace value is a 422, never a silent 202."""
    response = harness.request_code(roll_number)
    assert response.status == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_verify_issues_one_token_and_me_returns_the_current_roster_row(
    harness: ApiHarness,
) -> None:
    """A valid code returns the token once; /me reads role and verticals again."""
    harness.request_code(VOLUNTEER_ROLL)
    response = harness.verify(harness.last_code())

    assert response.status == 200
    payload = response.json()
    assert set(payload) == {"token", "expires_at", "person"}
    assert payload["expires_at"] == "2026-11-20T09:00:00+00:00"
    assert payload["person"] == {
        "id": 4,
        "role": "Volunteer",
        "vertical_ids": [1],
    }
    token = str(payload["token"])

    me = harness.me(token)
    assert me.status == 200
    # /me adds the caller's own name and contact details (NFR-SEC-03: self is
    # always visible); /verify keeps the identity triple.
    assert me.json()["person"] == {
        **payload["person"],
        "name": "Volunteer One",
        "phone": "ph-4",
    }


def test_issued_token_is_stored_hashed_only(harness: ApiHarness) -> None:
    """The database holds a SHA-256 of the token, never the token itself."""
    token = harness.sign_in()
    connection = sqlite3.connect(harness.database)
    try:
        rows = connection.execute("SELECT token_hash FROM auth_session").fetchall()
    finally:
        connection.close()
    hashes = [str(row[0]) for row in rows]
    assert hashes == [hashlib.sha256(token.encode()).hexdigest()]
    assert token not in hashes


@pytest.mark.parametrize("candidate", ["000000", "123456", "999999", "nope"])
def test_verify_rejects_a_wrong_code_with_one_indistinguishable_401(
    harness: ApiHarness, candidate: str
) -> None:
    """A wrong code is always 401 UNAUTHENTICATED and says nothing else."""
    harness.request_code(VOLUNTEER_ROLL)
    correct = harness.last_code()
    wrong = candidate if candidate != correct else _wrong_code(correct)
    response = harness.verify(wrong)
    assert response.status == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert correct.encode() not in response.body


def test_verify_rejects_an_unknown_roll_number(harness: ApiHarness) -> None:
    response = harness.verify("123456", UNKNOWN_ROLL)
    assert response.status == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_verify_rejects_an_expired_code(harness: ApiHarness) -> None:
    """The 5-minute TTL is policy, so time is moved, not slept."""
    harness.request_code(VOLUNTEER_ROLL)
    code = harness.last_code()
    harness.clock.advance(301)
    response = harness.verify(code)
    assert response.status == 401
    assert _attempts(harness.database, "VERIFY") == ["EXPIRED"]


def test_verify_rejects_an_exhausted_challenge(harness: ApiHarness) -> None:
    """Five attempts are allowed; the sixth is refused even with the code."""
    harness.request_code(VOLUNTEER_ROLL)
    correct = harness.last_code()
    wrong = _wrong_code(correct)
    for _ in range(5):
        assert harness.verify(wrong).status == 401
    response = harness.verify(correct)
    assert response.status == 401
    assert _attempts(harness.database, "VERIFY")[-1] == "ATTEMPTS_EXHAUSTED"


def test_verify_rejects_a_reused_code(harness: ApiHarness) -> None:
    """A code is single-use: the session exists, the challenge is consumed."""
    harness.request_code(VOLUNTEER_ROLL)
    code = harness.last_code()
    assert harness.verify(code).status == 200
    assert harness.verify(code).status == 401


def test_a_new_request_invalidates_the_previous_code(
    harness: ApiHarness,
) -> None:
    """One active challenge per person, so the older code stops working."""
    harness.request_code(VOLUNTEER_ROLL)
    first = harness.last_code()
    harness.request_code(VOLUNTEER_ROLL)
    second = harness.last_code()
    assert first != second
    assert harness.verify(first).status == 401
    assert harness.verify(second).status == 200


def test_verify_rate_limit_returns_429(tmp_path: Path) -> None:
    """Too many failures in the window is the one public 429."""
    harness = build_harness(
        tmp_path, config=Config({"AUTH_VERIFY_FAILURE_LIMIT": "2"})
    )
    harness.request_code(VOLUNTEER_ROLL)
    correct = harness.last_code()
    wrong = _wrong_code(correct)
    assert harness.verify(wrong).status == 401
    assert harness.verify(wrong).status == 401
    response = harness.verify(correct)
    assert response.status == 429
    assert response.json()["error"]["code"] == "RATE_LIMITED"
    assert _attempts(harness.database, "VERIFY")[-1] == "RATE_LIMITED"


def test_me_returns_the_current_role_and_verticals(harness: ApiHarness) -> None:
    """FR-AUT-05: the roster is read again, not cached in the session."""
    token = harness.sign_in()
    connection = sqlite3.connect(harness.database)
    try:
        connection.execute("UPDATE person SET role = 'Head' WHERE id = 4")
        connection.execute(
            "INSERT INTO person_vertical (person_id, vertical_id) VALUES (4, 2)"
        )
        connection.commit()
    finally:
        connection.close()

    response = harness.me(token)
    assert response.status == 200
    assert response.json() == {
        "person": {
            "id": 4,
            "name": "Volunteer One",
            "role": "Head",
            "vertical_ids": [1, 2],
            "phone": "ph-4",
        }
    }


def test_deactivation_ends_an_existing_session(harness: ApiHarness) -> None:
    """FR-AUT-07: a still-valid token for an inactive person is a 401."""
    token = harness.sign_in()
    assert harness.me(token).status == 200
    connection = sqlite3.connect(harness.database)
    try:
        connection.execute("UPDATE person SET active = 0 WHERE id = 4")
        connection.commit()
    finally:
        connection.close()
    response = harness.me(token)
    assert response.status == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_logout_revokes_the_session_and_a_second_logout_is_a_401(
    harness: ApiHarness,
) -> None:
    """Logout is 204 with no body; the token stops working immediately."""
    token = harness.sign_in()
    response = harness.logout(token)
    assert response.status == 204
    assert response.body == b""
    assert harness.me(token).status == 401
    assert harness.logout(token).status == 401


def test_an_expired_session_is_rejected(harness: ApiHarness) -> None:
    """The 7-day session lifetime is policy, so the clock moves again."""
    token = harness.sign_in()
    harness.clock.advance(7 * 24 * 60 * 60 + 1)
    assert harness.me(token).status == 401


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic abc",
        "Bearer one two",
        "Token abc",
    ],
)
def test_malformed_authorization_headers_are_a_401(
    harness: ApiHarness, header: str | None
) -> None:
    """Only exactly ``Bearer <token>`` is accepted; nothing is echoed."""
    headers = {} if header is None else {"authorization": header}
    response = harness.call("GET", "/me", headers=headers)
    assert response.status == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_an_issued_code_never_appears_in_any_response(
    harness: ApiHarness,
) -> None:
    """No response body carries a code, not even a failing verify."""
    harness.request_code(VOLUNTEER_ROLL)
    code = harness.last_code()
    responses = [
        harness.verify(_wrong_code(code)),
        harness.request_code(UNKNOWN_ROLL),
    ]
    for response in responses:
        assert code.encode() not in response.body


def test_no_code_or_roll_number_reaches_the_attempt_ledger(
    harness: ApiHarness,
) -> None:
    """The ledger records reasons only: no code, no roll number, no Discord id."""
    harness.request_code(VOLUNTEER_ROLL)
    code = harness.last_code()
    harness.verify(code)
    connection = sqlite3.connect(harness.database)
    try:
        dump = "\n".join(
            str(value)
            for row in connection.execute("SELECT * FROM auth_attempt").fetchall()
            for value in row
        )
    finally:
        connection.close()
    assert code not in dump
    assert VOLUNTEER_ROLL not in dump
    assert VOLUNTEER_DISCORD not in dump


def test_a_request_is_logged_with_metadata_but_never_the_token(
    harness: ApiHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """One access line: method, path, status, request id, actor, latency."""
    token = harness.sign_in()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="asec.api"):
        response = harness.me(token)

    assert response.status == 200
    text = caplog.text
    assert "method=GET path=/me status=200" in text
    assert "request_id=" in text
    assert "actor=4" in text
    assert "duration_ms=" in text
    assert token not in text


def test_a_refused_request_logs_no_actor_and_no_token(
    harness: ApiHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """An unauthenticated call logs ``actor=-`` and nothing from the header."""
    presented = "not-a-real-token-0000"
    with caplog.at_level(logging.INFO, logger="asec.api"):
        response = harness.call(
            "GET", "/me", headers={"authorization": f"Bearer {presented}"}
        )

    assert response.status == 401
    assert "status=401" in caplog.text
    assert "actor=-" in caplog.text
    assert presented not in caplog.text
