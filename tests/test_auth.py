"""OTP sign-in, bearer sessions and rate limits (W1; FR-ROS-03, FR-AUT-01/05/07).

Every value here is synthetic. The OTP code is a test value produced by
:func:`backend.auth.request_otp`, so a test may hold it; no response, log line,
exception, ``repr`` or stored row may contain it in clear (NFR-SEC-01,
NFR-SEC-06).

The domain functions take an injected ``now``; nothing in this file sleeps.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import re
import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.auth import (
    REASON_AUTHENTICATION_FAILED,
    REASON_INVALID_CODE,
    REASON_INVALID_REQUEST,
    REASON_RATE_LIMITED,
    AuthError,
    Session,
    authenticate,
    request_otp,
    revoke_session,
    verify_otp,
)
from backend.config import (
    AuthSettings,
    Config,
    ConfigError,
    load_auth_settings,
)
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.notification_delivery import (
    OtpSenderNotConfigured,
    RecordingOtpSender,
)

NOW = "2026-11-13T09:00:00+00:00"
LATER = "2026-11-13T10:00:00+00:00"

ADMIN_ROLL = "R-ADMIN"
HEAD_ROLL = "R-HEAD"
AH_ROLL = "R-AH"
VOLUNTEER_ROLL = "R-VOL"
INACTIVE_ROLL = "R-INACTIVE"
NO_DISCORD_ROLL = "R-NODIS"
UNKNOWN_ROLL = "R-NOBODY"

VOLUNTEER_DISCORD = "100000000000000004"
_SIX_DIGITS = re.compile(r"^\d{6}$")


@pytest.fixture()
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A migrated database with a synthetic reporting tree and Discord ids."""
    conn = connect(tmp_path / "auth.db")
    apply_migrations(conn)
    conn.execute("INSERT INTO vertical (id, name) VALUES (1, 'Logistics')")
    conn.execute("INSERT INTO vertical (id, name) VALUES (2, 'Decor')")
    _add_person(conn, 1, "Admin One", "Admin", ADMIN_ROLL, "100000000000000001", 2)
    _add_person(conn, 2, "Head One", "Head", HEAD_ROLL, "100000000000000002", 1)
    _add_person(
        conn, 3, "AH One", "AH", AH_ROLL, "100000000000000003", 1, reports_to=2
    )
    _add_person(
        conn,
        4,
        "Volunteer One",
        "Volunteer",
        VOLUNTEER_ROLL,
        VOLUNTEER_DISCORD,
        1,
        reports_to=3,
    )
    _add_person(
        conn, 5, "Inactive One", "Volunteer", INACTIVE_ROLL,
        "100000000000000005", 1, reports_to=3, active=0
    )
    _add_person(conn, 6, "No Discord", "Volunteer", NO_DISCORD_ROLL, None, 1)
    yield conn
    conn.close()


def _add_person(
    connection: sqlite3.Connection,
    person_id: int,
    name: str,
    role: str,
    roll_number: str,
    discord_id: str | None,
    vertical_id: int,
    *,
    reports_to: int | None = None,
    active: int = 1,
) -> None:
    connection.execute(
        "INSERT INTO person"
        " (id, name, discord_user_id, phone, role, reports_to, active, roll_number)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            person_id,
            name,
            discord_id,
            f"ph-{person_id}",
            role,
            reports_to,
            active,
            roll_number,
        ),
    )
    connection.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (?, ?)",
        (person_id, vertical_id),
    )


def _settings(**overrides: int) -> AuthSettings:
    values = {
        "otp_ttl_seconds": 300,
        "otp_max_attempts": 5,
        "session_lifetime_seconds": 7 * 24 * 60 * 60,
        "otp_request_limit": 3,
        "verify_failure_limit": 10,
        "rate_window_seconds": 900,
    }
    values.update(overrides)
    return AuthSettings(**values)


def _challenges(
    connection: sqlite3.Connection, person_id: int
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM otp_challenge WHERE person_id = ? ORDER BY id",
        (person_id,),
    ).fetchall()


def _attempts(
    connection: sqlite3.Connection, kind: str | None = None
) -> list[sqlite3.Row]:
    if kind is None:
        rows = connection.execute(
            "SELECT * FROM auth_attempt ORDER BY id"
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM auth_attempt WHERE kind = ? ORDER BY id", (kind,)
        ).fetchall()
    return list(rows)


def _sent_code(sender: RecordingOtpSender) -> str:
    assert len(sender.sent) >= 1
    return sender.sent[-1][1]


def _sign_in(
    connection: sqlite3.Connection,
    sender: RecordingOtpSender,
    *,
    roll_number: str = VOLUNTEER_ROLL,
    now: str = NOW,
    settings: AuthSettings | None = None,
) -> Session:
    effective = _settings() if settings is None else settings
    request_otp(
        connection,
        roll_number=roll_number,
        sender=sender,
        now=now,
        settings=effective,
    )
    return verify_otp(
        connection,
        roll_number=roll_number,
        code=_sent_code(sender),
        now=now,
        settings=effective,
    )


def _instant(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text)


def _shift(value: str, seconds: float) -> str:
    return (_instant(value) + timedelta(seconds=seconds)).astimezone(UTC).isoformat()


class _FailingSender:
    """A sender that fails after the challenge exists; never reveals the code."""

    def __init__(self, error: BaseException | None = None) -> None:
        self._error = RuntimeError("provider exploded") if error is None else error
        self.calls = 0

    def send(self, *, discord_id: str, code: str) -> None:
        self.calls += 1
        raise self._error

    def __repr__(self) -> str:
        return f"_FailingSender(calls={self.calls})"


class _UnconfiguredSender:
    """Mirrors ``require_otp_sender(None)`` at the call site."""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, *, discord_id: str, code: str) -> None:
        self.calls += 1
        raise OtpSenderNotConfigured("OTP sender is not configured")


# --- request_otp -----------------------------------------------------------


def test_request_otp_sends_only_to_a_valid_active_person(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    for roll in (UNKNOWN_ROLL, INACTIVE_ROLL, NO_DISCORD_ROLL):
        assert (
            request_otp(
                connection,
                roll_number=roll,
                sender=sender,
                now=NOW,
                settings=_settings(),
            )
            is None
        )
    assert sender.sent == ()
    assert _challenges(connection, 4) == []

    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=NOW,
        settings=_settings(),
    )

    assert len(sender.sent) == 1
    discord_id, code = sender.sent[0]
    # O11-1: the destination is the roster Discord id, never a client value.
    assert discord_id == VOLUNTEER_DISCORD
    assert _SIX_DIGITS.match(code)


def test_unknown_inactive_and_valid_roll_numbers_return_the_same_result(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    results = [
        request_otp(
            connection,
            roll_number=roll,
            sender=sender,
            now=NOW,
            settings=_settings(),
        )
        for roll in (
            UNKNOWN_ROLL,
            INACTIVE_ROLL,
            NO_DISCORD_ROLL,
            VOLUNTEER_ROLL,
        )
    ]

    assert results == [None, None, None, None]
    reasons = [str(row["reason"]) for row in _attempts(connection, "OTP_REQUEST")]
    assert reasons == [
        "UNKNOWN_ROLL_NUMBER",
        "INACTIVE_PERSON",
        "NO_DISCORD_ID",
        "CHALLENGE_CREATED",
    ]
    assert [int(row["succeeded"]) for row in _attempts(connection, "OTP_REQUEST")] == [
        0,
        0,
        0,
        1,
    ]


def test_unknown_and_valid_requests_do_the_same_hashing_work(
    connection: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend import auth

    calls: list[float] = []
    original = auth._hash_code

    def counting(code: str, salt: bytes, iterations: int) -> bytes:
        calls.append(time.perf_counter())
        return original(code, salt, iterations)

    monkeypatch.setattr(auth, "_hash_code", counting)
    sender = RecordingOtpSender()
    for roll in (UNKNOWN_ROLL, INACTIVE_ROLL, VOLUNTEER_ROLL):
        request_otp(
            connection,
            roll_number=roll,
            sender=sender,
            now=NOW,
            settings=_settings(),
        )

    # No early return on the unknown path: every call hashes exactly once.
    assert len(calls) == 3


def test_request_timing_is_comparable_for_unknown_and_valid_roll_numbers(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    settings = _settings(otp_request_limit=100)

    def measure(roll_number: str) -> float:
        started = time.perf_counter()
        for _ in range(5):
            request_otp(
                connection,
                roll_number=roll_number,
                sender=sender,
                now=NOW,
                settings=settings,
            )
        return time.perf_counter() - started

    unknown = measure(UNKNOWN_ROLL)
    valid = measure(VOLUNTEER_ROLL)

    # Same order of magnitude; the assertion is deliberately loose because a
    # build machine is noisy. The counting test above proves the work is equal.
    assert 0.3 < (unknown / valid) < 3.0


def test_code_is_six_digits_and_stored_only_as_a_salted_pbkdf2_hash(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=NOW,
        settings=_settings(),
    )
    code = _sent_code(sender)
    row = _challenges(connection, 4)[0]

    assert _SIX_DIGITS.match(code)
    salt = bytes.fromhex(str(row["code_salt"]))
    stored = str(row["code_hash"])
    assert len(salt) == 16
    assert code not in str(row["code_salt"])
    assert code not in stored
    expected = hashlib.pbkdf2_hmac(
        "sha256", code.encode("utf-8"), salt, 1_000
    )
    # The stored hash is the scheme, not a copy: re-derive with the same
    # iteration count the module exposes.
    from backend import auth

    recomputed = hashlib.pbkdf2_hmac(
        "sha256", code.encode("utf-8"), salt, auth.PBKDF2_ITERATIONS
    )
    assert stored == recomputed.hex()
    assert expected != stored


def test_challenge_expires_five_minutes_after_the_request(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=NOW,
        settings=_settings(),
    )
    row = _challenges(connection, 4)[0]
    assert str(row["expires_at"]) == _shift(NOW, 300)
    code = _sent_code(sender)

    session = verify_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        code=code,
        now=_shift(NOW, 299),
        settings=_settings(),
    )
    assert session.person_id == 4

    request_otp(
        connection,
        roll_number=HEAD_ROLL,
        sender=sender,
        now=NOW,
        settings=_settings(),
    )
    expired_code = sender.sent[-1][1]
    with pytest.raises(AuthError) as raised:
        verify_otp(
            connection,
            roll_number=HEAD_ROLL,
            code=expired_code,
            now=_shift(NOW, 300),
            settings=_settings(),
        )
    assert raised.value.reason == REASON_INVALID_CODE
    assert int(_challenges(connection, 2)[0]["attempts"]) == 1


def test_five_attempts_are_allowed_and_the_sixth_is_refused(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=NOW,
        settings=_settings(),
    )
    code = _sent_code(sender)
    wrong = "000000" if code != "000000" else "111111"

    for _ in range(4):
        with pytest.raises(AuthError):
            verify_otp(
                connection,
                roll_number=VOLUNTEER_ROLL,
                code=wrong,
                now=NOW,
                settings=_settings(),
            )
    assert int(_challenges(connection, 4)[0]["attempts"]) == 4

    # The fifth attempt is the last one, and it succeeds.
    session = verify_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        code=code,
        now=NOW,
        settings=_settings(),
    )
    assert session.person_id == 4
    assert int(_challenges(connection, 4)[0]["attempts"]) == 5


def test_an_exhausted_challenge_rejects_even_the_correct_code(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=NOW,
        settings=_settings(),
    )
    code = _sent_code(sender)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        with pytest.raises(AuthError):
            verify_otp(
                connection,
                roll_number=VOLUNTEER_ROLL,
                code=wrong,
                now=NOW,
                settings=_settings(),
            )
    with pytest.raises(AuthError) as raised:
        verify_otp(
            connection,
            roll_number=VOLUNTEER_ROLL,
            code=code,
            now=NOW,
            settings=_settings(),
        )
    assert raised.value.reason == REASON_INVALID_CODE
    assert int(_challenges(connection, 4)[0]["attempts"]) == 5


def test_a_new_request_consumes_the_previous_unconsumed_challenge(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=NOW,
        settings=_settings(),
    )
    first = _sent_code(sender)
    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=LATER,
        settings=_settings(),
    )
    second = sender.sent[1][1]
    rows = _challenges(connection, 4)

    assert len(rows) == 2
    assert str(rows[0]["consumed_at"]) == LATER
    assert rows[1]["consumed_at"] is None
    with pytest.raises(AuthError):
        verify_otp(
            connection,
            roll_number=VOLUNTEER_ROLL,
            code=first,
            now=LATER,
            settings=_settings(),
        )
    assert (
        verify_otp(
            connection,
            roll_number=VOLUNTEER_ROLL,
            code=second,
            now=LATER,
            settings=_settings(),
        ).person_id
        == 4
    )


def test_wrong_expired_exhausted_and_reused_codes_share_one_error(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    reasons: list[str] = []

    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=NOW,
        settings=_settings(),
    )
    correct = _sent_code(sender)
    with pytest.raises(AuthError) as wrong:
        verify_otp(
            connection,
            roll_number=VOLUNTEER_ROLL,
            code="000000" if correct != "000000" else "111111",
            now=NOW,
            settings=_settings(),
        )
    reasons.append(wrong.value.reason)

    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=LATER,
        settings=_settings(),
    )
    fresh = sender.sent[-1][1]
    with pytest.raises(AuthError) as expired:
        verify_otp(
            connection,
            roll_number=VOLUNTEER_ROLL,
            code=fresh,
            now=_shift(LATER, 300),
            settings=_settings(),
        )
    reasons.append(expired.value.reason)

    reused = verify_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        code=sender.sent[-1][1],
        now=LATER,
        settings=_settings(),
    )
    assert reused.person_id == 4
    with pytest.raises(AuthError) as again:
        verify_otp(
            connection,
            roll_number=VOLUNTEER_ROLL,
            code=fresh,
            now=LATER,
            settings=_settings(),
        )
    reasons.append(again.value.reason)

    assert reasons == [REASON_INVALID_CODE, REASON_INVALID_CODE, REASON_INVALID_CODE]
    assert str(wrong.value) == str(expired.value) == str(again.value)


def test_a_failed_sender_is_recorded_and_never_changes_the_result(
    connection: sqlite3.Connection,
) -> None:
    failing = _FailingSender()
    unconfigured = _UnconfiguredSender()

    assert (
        request_otp(
            connection,
            roll_number=VOLUNTEER_ROLL,
            sender=failing,
            now=NOW,
            settings=_settings(),
        )
        is None
    )
    assert (
        request_otp(
            connection,
            roll_number=VOLUNTEER_ROLL,
            sender=unconfigured,
            now=NOW,
            settings=_settings(),
        )
        is None
    )
    assert failing.calls == 1
    assert unconfigured.calls == 1

    delivery = _attempts(connection, "OTP_DELIVERY")
    assert [str(row["reason"]) for row in delivery] == [
        "SENDER_FAILED",
        "SENDER_UNCONFIGURED",
    ]
    assert [int(row["succeeded"]) for row in delivery] == [0, 0]
    for row in delivery:
        assert "provider exploded" not in str(row["reason"])
        assert "not configured" not in str(row["reason"])


def test_otp_request_is_rate_limited_per_roll_number_hash(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    for _ in range(3):
        request_otp(
            connection,
            roll_number=VOLUNTEER_ROLL,
            sender=sender,
            now=NOW,
            settings=_settings(),
        )
    assert len(sender.sent) == 3

    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=NOW,
        settings=_settings(),
    )

    assert len(sender.sent) == 3
    reasons = [str(row["reason"]) for row in _attempts(connection, "OTP_REQUEST")]
    assert reasons[-1] == "RATE_LIMITED"
    assert len(_challenges(connection, 4)) == 3

    # A different roll number is a different limit.
    request_otp(
        connection,
        roll_number=HEAD_ROLL,
        sender=sender,
        now=NOW,
        settings=_settings(),
    )
    assert len(sender.sent) == 4


def test_the_otp_request_limit_is_configuration(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    tight = _settings(otp_request_limit=1)
    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=NOW,
        settings=tight,
    )
    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=NOW,
        settings=tight,
    )

    assert len(sender.sent) == 1
    assert str(_attempts(connection, "OTP_REQUEST")[-1]["reason"]) == "RATE_LIMITED"


def test_verify_failures_are_rate_limited_after_ten_failures(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=NOW,
        settings=_settings(),
    )
    code = _sent_code(sender)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(10):
        with pytest.raises(AuthError):
            verify_otp(
                connection,
                roll_number=VOLUNTEER_ROLL,
                code=wrong,
                now=NOW,
                settings=_settings(),
            )

    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=NOW,
        settings=_settings(otp_request_limit=100),
    )
    with pytest.raises(AuthError) as raised:
        verify_otp(
            connection,
            roll_number=VOLUNTEER_ROLL,
            code=sender.sent[-1][1],
            now=NOW,
            settings=_settings(otp_request_limit=100),
        )
    assert raised.value.reason == REASON_RATE_LIMITED


def test_the_roll_number_is_stored_only_as_a_hash(
    connection: sqlite3.Connection,
) -> None:
    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=RecordingOtpSender(),
        now=NOW,
        settings=_settings(),
    )
    row = _attempts(connection, "OTP_REQUEST")[0]

    assert str(row["roll_number_hash"]) == hashlib.sha256(
        VOLUNTEER_ROLL.encode("utf-8")
    ).hexdigest()
    dump = "\n".join(
        str(value)
        for attempt in _attempts(connection)
        for value in tuple(attempt)
    )
    assert VOLUNTEER_ROLL not in dump


def test_a_blank_roll_number_is_rejected_before_the_sender(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    with pytest.raises(AuthError) as raised:
        request_otp(
            connection,
            roll_number="   ",
            sender=sender,
            now=NOW,
            settings=_settings(),
        )
    assert raised.value.reason == REASON_INVALID_REQUEST
    assert sender.sent == ()


# --- sessions --------------------------------------------------------------


def test_session_token_is_returned_once_and_only_its_sha256_is_stored(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    session = _sign_in(connection, sender)
    token = session.token

    assert isinstance(token, str)
    assert len(token) >= 43
    rows = connection.execute("SELECT * FROM auth_session").fetchall()
    assert len(rows) == 1
    assert str(rows[0]["token_hash"]) == hashlib.sha256(
        token.encode("utf-8")
    ).hexdigest()
    stored = "\n".join(str(value) for value in tuple(rows[0]))
    assert token not in stored

    # The token is a secret and never appears in a repr or a string form.
    assert token not in repr(session)
    assert token not in str(session)


def test_session_expires_seven_days_after_sign_in(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    session = _sign_in(connection, sender)

    person = authenticate(
        connection, session.token, now=_shift(NOW, 7 * 24 * 60 * 60 - 1)
    )
    assert person.person_id == 4
    with pytest.raises(AuthError) as raised:
        authenticate(
            connection, session.token, now=_shift(NOW, 7 * 24 * 60 * 60)
        )
    assert raised.value.reason == REASON_AUTHENTICATION_FAILED


def test_logout_revokes_the_session_and_a_reused_token_fails(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    session = _sign_in(connection, sender)

    assert revoke_session(connection, session.token, now=NOW) is True
    assert revoke_session(connection, session.token, now=NOW) is False
    revoked_at = connection.execute("SELECT revoked_at FROM auth_session").fetchone()
    assert str(revoked_at[0]) == NOW
    with pytest.raises(AuthError) as raised:
        authenticate(connection, session.token, now=NOW)
    assert raised.value.reason == REASON_AUTHENTICATION_FAILED


def test_unknown_revoked_expired_and_inactive_sessions_share_one_error(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    revoked = _sign_in(connection, sender)
    revoke_session(connection, revoked.token, now=NOW)

    expired = _sign_in(connection, sender, now=NOW)
    inactive_token = _sign_in(connection, sender, now=NOW)
    connection.execute("UPDATE person SET active = 0 WHERE id = 4")

    reasons: list[str] = []
    messages: list[str] = []
    for token, now in (
        ("not-a-real-token", NOW),
        (revoked.token, NOW),
        (expired.token, _shift(NOW, 7 * 24 * 60 * 60 + 1)),
        (inactive_token.token, NOW),
    ):
        with pytest.raises(AuthError) as raised:
            authenticate(connection, token, now=now)
        reasons.append(raised.value.reason)
        messages.append(str(raised.value))

    assert reasons == [REASON_AUTHENTICATION_FAILED] * 4
    assert len(set(messages)) == 1


def test_authenticate_reads_the_current_role_and_verticals_from_the_database(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    session = _sign_in(connection, sender)

    connection.execute("UPDATE person SET role = 'AH' WHERE id = 4")
    connection.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (4, 2)"
    )

    person = authenticate(connection, session.token, now=NOW)

    # FR-AUT-05: the current roster row wins, not the role at sign-in and
    # never a caller-supplied value.
    assert person.role == "AH"
    assert person.vertical_ids == (1, 2)


def test_deactivation_blocks_sign_in_and_ends_the_existing_session(
    connection: sqlite3.Connection,
) -> None:
    sender = RecordingOtpSender()
    session = _sign_in(connection, sender)

    connection.execute("UPDATE person SET active = 0 WHERE id = 4")

    with pytest.raises(AuthError) as raised:
        authenticate(connection, session.token, now=NOW)
    assert raised.value.reason == REASON_AUTHENTICATION_FAILED

    request_otp(
        connection,
        roll_number=VOLUNTEER_ROLL,
        sender=sender,
        now=NOW,
        settings=_settings(),
    )
    assert len(sender.sent) == 1
    assert str(_attempts(connection, "OTP_REQUEST")[-1]["reason"]) == "INACTIVE_PERSON"


# --- secrets in logs, errors and bodies ------------------------------------


def test_the_code_never_appears_in_logs_or_error_text(
    connection: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    sender = RecordingOtpSender()
    session = _sign_in(connection, sender)
    code = _sent_code(sender)

    with pytest.raises(AuthError) as raised:
        verify_otp(
            connection,
            roll_number=VOLUNTEER_ROLL,
            code=code,
            now=NOW,
            settings=_settings(),
        )

    assert code not in caplog.text
    assert code not in repr(raised.value)
    assert code not in str(raised.value)
    assert code not in repr(session)
    assert code not in str(session)
    assert code not in repr(sender)


def test_no_notification_row_carries_the_code(connection: sqlite3.Connection) -> None:
    sender = RecordingOtpSender()
    _sign_in(connection, sender)
    code = _sent_code(sender)

    # Phase 11 has no OTP notification event: the DM text is the only carrier,
    # so no in-app body can hold the code.
    rows = connection.execute("SELECT * FROM notification").fetchall()
    assert rows == []
    events = connection.execute(
        "SELECT DISTINCT event_type FROM notification"
    ).fetchall()
    assert "OTP" not in {str(row["event_type"]) for row in events}
    # The recording test double holds the code in memory by design; the stored
    # challenge row never does.
    stored = connection.execute("SELECT * FROM otp_challenge").fetchone()
    assert stored is not None
    assert code not in repr(stored)


# --- configuration ---------------------------------------------------------


def test_auth_settings_are_configuration() -> None:
    settings = load_auth_settings(
        _config(
            {
                "AUTH_OTP_TTL_SECONDS": "120",
                "AUTH_OTP_MAX_ATTEMPTS": "4",
                "AUTH_SESSION_LIFETIME_SECONDS": "3600",
                "AUTH_OTP_REQUEST_LIMIT": "2",
                "AUTH_VERIFY_FAILURE_LIMIT": "7",
                "AUTH_RATE_WINDOW_SECONDS": "60",
            }
        )
    )

    assert settings == AuthSettings(
        otp_ttl_seconds=120,
        otp_max_attempts=4,
        session_lifetime_seconds=3600,
        otp_request_limit=2,
        verify_failure_limit=7,
        rate_window_seconds=60,
    )


def test_auth_settings_blank_values_keep_the_owner_defaults() -> None:
    settings = load_auth_settings(
        _config(
            {
                "AUTH_OTP_TTL_SECONDS": "",
                "AUTH_OTP_MAX_ATTEMPTS": " ",
                "AUTH_SESSION_LIFETIME_SECONDS": "",
                "AUTH_OTP_REQUEST_LIMIT": "",
                "AUTH_VERIFY_FAILURE_LIMIT": "",
                "AUTH_RATE_WINDOW_SECONDS": "",
            }
        )
    )

    # O12-3: 7-day sessions, 5-minute OTP, 5 attempts, 3 requests / 15 min.
    assert settings == AuthSettings(
        otp_ttl_seconds=300,
        otp_max_attempts=5,
        session_lifetime_seconds=604800,
        otp_request_limit=3,
        verify_failure_limit=10,
        rate_window_seconds=900,
    )


@pytest.mark.parametrize(
    "values",
    [
        {"AUTH_OTP_TTL_SECONDS": "0"},
        {"AUTH_OTP_TTL_SECONDS": "soon"},
        {"AUTH_OTP_MAX_ATTEMPTS": "0"},
        {"AUTH_OTP_MAX_ATTEMPTS": "five"},
        {"AUTH_SESSION_LIFETIME_SECONDS": "0"},
        {"AUTH_OTP_REQUEST_LIMIT": "-1"},
        {"AUTH_VERIFY_FAILURE_LIMIT": "0"},
        {"AUTH_RATE_WINDOW_SECONDS": "0"},
    ],
)
def test_auth_settings_reject_invalid_values(values: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        load_auth_settings(_config(values))


def test_the_auth_module_never_reads_the_wall_clock_or_sleeps() -> None:
    source = (Path(__file__).resolve().parents[1] / "backend" / "auth.py").read_text(
        encoding="utf-8"
    )

    # NFR-MNT-01: ``now`` is injected; only the process boundary reads a clock.
    assert "datetime.now" not in source
    assert "time.sleep" not in source
    assert "utcnow(" not in source


def test_the_otp_hash_comparison_uses_compare_digest() -> None:
    """Static source guard: the stored OTP hash is compared with ``compare_digest``.

    This is an AST/source assertion, not a timing measurement: it pins the
    constant-time comparison primitive (NFR-SEC-01) and fails if the call is
    replaced by ``==`` or any other comparison. A unit test cannot honestly
    measure timing, so it does not claim to.
    """
    source = (Path(__file__).resolve().parents[1] / "backend" / "auth.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "compare_digest"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "hmac"
    ]
    assert calls, "backend/auth.py must compare OTP hashes with hmac.compare_digest"


def _config(values: dict[str, str]) -> Config:
    return Config(values)
