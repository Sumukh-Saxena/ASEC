"""OTP sign-in and bearer sessions (W1; FR-ROS-03, FR-AUT-01/05/07, NFR-SEC-06).

One roster person signs in with their institute roll number. The one-time code
is a Discord DM from the ASEC bot to the Discord id the roster holds (O11-1),
so the destination is never a value typed at sign-in.

Boundary rules this module enforces:

* Unknown, inactive and valid roll numbers take the same path: the code is
  generated and hashed before the roster lookup, so no branch returns early and
  every request does the same PBKDF2 work. Only a valid active person with a
  roster Discord id reaches ``sender.send``.
* A failed or unconfigured sender is recorded as a sanitised outcome and never
  raises: sign-in cannot reveal whether a DM arrived.
* The 6-digit code comes from :mod:`secrets` and is stored only as
  PBKDF2-HMAC-SHA256 with a random 16-byte salt. It is never logged, stored in
  clear, put in a ``repr``, an exception string or a recorded attempt.
* A session token is ``secrets.token_urlsafe(32)``, returned once and stored
  only as its SHA-256 hash. The role and verticals always come from the current
  roster row, never from the caller (FR-AUT-05), and an inactive person fails on
  the next request (FR-AUT-07).
* ``now`` is injected by the caller; this module never reads the wall clock and
  never sleeps (NFR-MNT-01).

Rows are append-only evidence. Nothing here deletes a session, challenge or
attempt; retention is the DR-07 procedure recorded with O12-7.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backend.bucketing import parse_event_time
from backend.config import AuthSettings
from backend.db.connection import transaction
from backend.notification_delivery import OtpSender, OtpSenderNotConfigured

logger = logging.getLogger("asec.auth")

#: PBKDF2-HMAC-SHA256 work factor for a stored OTP hash. A 6-digit code lives
#: five minutes and allows five attempts, so this is deliberately cheap enough
#: for the sign-in path while still making an offline guess expensive.
PBKDF2_ITERATIONS = 210_000

#: Stable reasons recorded in ``auth_attempt.reason``. They are the only text
#: this module writes there; no code, roll number or provider message is copied.
OTP_REQUEST_KIND = "OTP_REQUEST"
OTP_DELIVERY_KIND = "OTP_DELIVERY"
VERIFY_KIND = "VERIFY"

REASON_CHALLENGE_CREATED = "CHALLENGE_CREATED"
REASON_UNKNOWN_ROLL_NUMBER = "UNKNOWN_ROLL_NUMBER"
REASON_INACTIVE_PERSON = "INACTIVE_PERSON"
REASON_NO_DISCORD_ID = "NO_DISCORD_ID"
REASON_RATE_LIMITED = "RATE_LIMITED"
REASON_SENT = "SENT"
REASON_SENDER_FAILED = "SENDER_FAILED"
REASON_SENDER_UNCONFIGURED = "SENDER_UNCONFIGURED"
REASON_INVALID_CODE = "INVALID_CODE"
REASON_EXPIRED = "EXPIRED"
REASON_ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
REASON_SIGNED_IN = "SIGNED_IN"
REASON_AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
REASON_INVALID_REQUEST = "INVALID_REQUEST"

_DUMMY_SALT = b"\x00" * 16


class AuthError(Exception):
    """A sign-in or session check failed.

    ``reason`` is a stable code from the set above. The string form carries the
    reason only: it never carries the code, the token or a roster value.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class AuthenticatedPerson:
    """The caller as the current roster row describes them (FR-AUT-05)."""

    person_id: int
    role: str
    vertical_ids: tuple[int, ...]


@dataclass(frozen=True, repr=False)
class Session:
    """One issued session. ``token`` is returned once and never stored in clear."""

    token: str
    person_id: int
    issued_at: str
    expires_at: str

    def __repr__(self) -> str:
        # The token is deliberately absent: this object is safe to log.
        return (
            f"Session(person_id={self.person_id!r}, issued_at={self.issued_at!r},"
            f" expires_at={self.expires_at!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class _Person:
    person_id: int
    active: bool
    discord_id: str | None


def request_otp(
    connection: sqlite3.Connection,
    *,
    roll_number: str,
    sender: OtpSender,
    now: str,
    settings: AuthSettings,
) -> None:
    """Issue one OTP challenge for ``roll_number`` and hand the code to ``sender``.

    The public outcome is always the same: no return value, no raised error for
    an unknown, inactive, Discord-less or rate-limited person, and none for a
    sender failure. Only the private ``auth_attempt`` ledger records what
    happened, and it records a stable code rather than any roster value.
    """
    normalized = roll_number.strip()
    if normalized == "":
        raise AuthError(REASON_INVALID_REQUEST)
    instant = _instant(now)
    attempted_at = _canonical(instant)
    roll_hash = _roll_number_hash(normalized)

    # Same work on every path: the code exists and is hashed before any lookup.
    code = _new_code()
    salt = secrets.token_bytes(16)
    code_hash = _hash_code(code, salt, PBKDF2_ITERATIONS)

    person: _Person | None = None
    with transaction(connection):
        person = _person_by_roll_number(connection, normalized)
        recent = _recent_attempts(
            connection,
            roll_hash,
            OTP_REQUEST_KIND,
            attempted_at,
            settings.rate_window_seconds,
        )
        if person is None:
            outcome = REASON_UNKNOWN_ROLL_NUMBER
        elif not person.active:
            outcome = REASON_INACTIVE_PERSON
        elif person.discord_id is None:
            outcome = REASON_NO_DISCORD_ID
        elif recent >= settings.otp_request_limit:
            outcome = REASON_RATE_LIMITED
        else:
            outcome = REASON_CHALLENGE_CREATED
            _consume_open_challenges(connection, person.person_id, attempted_at)
            connection.execute(
                "INSERT INTO otp_challenge"
                " (person_id, code_salt, code_hash, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    person.person_id,
                    salt.hex(),
                    code_hash.hex(),
                    attempted_at,
                    _canonical(
                        instant + timedelta(seconds=settings.otp_ttl_seconds)
                    ),
                ),
            )
        _record_attempt(
            connection,
            roll_hash,
            OTP_REQUEST_KIND,
            attempted_at,
            succeeded=outcome == REASON_CHALLENGE_CREATED,
            reason=outcome,
        )

    if (
        outcome != REASON_CHALLENGE_CREATED
        or person is None
        or person.discord_id is None
    ):
        logger.info("otp request outcome=%s", outcome)
        return

    detail = _deliver(sender, person.discord_id, code)
    logger.info("otp delivery outcome=%s", detail)
    with transaction(connection):
        _record_attempt(
            connection,
            roll_hash,
            OTP_DELIVERY_KIND,
            attempted_at,
            succeeded=detail == REASON_SENT,
            reason=detail,
        )


def verify_otp(
    connection: sqlite3.Connection,
    *,
    roll_number: str,
    code: str,
    now: str,
    settings: AuthSettings,
) -> Session:
    """Verify one code and return a new session, or raise :class:`AuthError`.

    Every rejection (unknown roll number, inactive person, no challenge,
    expired, exhausted, wrong code) raises ``INVALID_CODE`` so the caller cannot
    tell which case it was. A challenge counts one attempt per verification,
    including the successful one; the sixth attempt is refused even with the
    right code. Exceeding the configured failure window raises
    ``RATE_LIMITED``.
    """
    normalized = roll_number.strip()
    candidate = code.strip()
    if normalized == "" or candidate == "":
        raise AuthError(REASON_INVALID_REQUEST)
    instant = _instant(now)
    attempted_at = _canonical(instant)
    roll_hash = _roll_number_hash(normalized)

    pending: AuthError | None = None
    session: Session | None = None
    with transaction(connection):
        failures = _recent_attempts(
            connection,
            roll_hash,
            VERIFY_KIND,
            attempted_at,
            settings.rate_window_seconds,
            failures_only=True,
        )
        person = _person_by_roll_number(connection, normalized)
        challenge = (
            None
            if person is None
            else _open_challenge(connection, person.person_id)
        )

        if failures >= settings.verify_failure_limit:
            outcome = REASON_RATE_LIMITED
            pending = AuthError(REASON_RATE_LIMITED)
        elif person is None:
            # Same hashing work as a real comparison; no early return.
            _hash_code(candidate, _DUMMY_SALT, PBKDF2_ITERATIONS)
            outcome = REASON_UNKNOWN_ROLL_NUMBER
            pending = AuthError(REASON_INVALID_CODE)
        elif not person.active:
            _hash_code(candidate, _DUMMY_SALT, PBKDF2_ITERATIONS)
            outcome = REASON_INACTIVE_PERSON
            pending = AuthError(REASON_INVALID_CODE)
        elif challenge is None:
            _hash_code(candidate, _DUMMY_SALT, PBKDF2_ITERATIONS)
            outcome = REASON_INVALID_CODE
            pending = AuthError(REASON_INVALID_CODE)
        elif _instant(str(challenge["expires_at"])) <= instant:
            _increment_attempts(connection, int(challenge["id"]))
            outcome = REASON_EXPIRED
            pending = AuthError(REASON_INVALID_CODE)
        elif int(challenge["attempts"]) >= settings.otp_max_attempts:
            outcome = REASON_ATTEMPTS_EXHAUSTED
            pending = AuthError(REASON_INVALID_CODE)
        else:
            salt = bytes.fromhex(str(challenge["code_salt"]))
            computed = _hash_code(candidate, salt, PBKDF2_ITERATIONS)
            matches = hmac.compare_digest(computed.hex(), str(challenge["code_hash"]))
            if not matches:
                _increment_attempts(connection, int(challenge["id"]))
                outcome = REASON_INVALID_CODE
                pending = AuthError(REASON_INVALID_CODE)
            else:
                _increment_attempts(connection, int(challenge["id"]))
                connection.execute(
                    "UPDATE otp_challenge SET consumed_at = ? WHERE id = ?",
                    (attempted_at, int(challenge["id"])),
                )
                session = _create_session(
                    connection,
                    person_id=person.person_id,
                    instant=instant,
                    settings=settings,
                )
                outcome = REASON_SIGNED_IN

        _record_attempt(
            connection,
            roll_hash,
            VERIFY_KIND,
            attempted_at,
            succeeded=session is not None,
            reason=outcome,
        )

    if pending is not None:
        raise pending
    if session is None:
        # Unreachable: every non-success branch sets ``pending``. A runtime
        # guard instead of ``assert`` because ``-O`` removes asserts (W9 H3).
        raise AuthError(REASON_AUTHENTICATION_FAILED)
    return session


def authenticate(
    connection: sqlite3.Connection, token: str, *, now: str
) -> AuthenticatedPerson:
    """Resolve a bearer token to the current roster person.

    An unknown, expired or revoked token and an inactive person all raise the
    same ``AUTHENTICATION_FAILED`` error. The role and vertical ids are read
    from the database on every call (FR-AUT-05), so a deactivation ends the
    session on the next request (FR-AUT-07).
    """
    stripped = token.strip()
    if stripped == "":
        raise AuthError(REASON_AUTHENTICATION_FAILED)
    instant = _instant(now)
    row = connection.execute(
        "SELECT s.person_id, s.expires_at, s.revoked_at, p.role, p.active"
        " FROM auth_session AS s"
        " JOIN person AS p ON p.id = s.person_id"
        " WHERE s.token_hash = ?",
        (_token_hash(stripped),),
    ).fetchone()
    if row is None:
        raise AuthError(REASON_AUTHENTICATION_FAILED)
    if row["revoked_at"] is not None:
        raise AuthError(REASON_AUTHENTICATION_FAILED)
    if _instant(str(row["expires_at"])) <= instant:
        raise AuthError(REASON_AUTHENTICATION_FAILED)
    if int(row["active"]) != 1:
        raise AuthError(REASON_AUTHENTICATION_FAILED)
    person_id = int(row["person_id"])
    verticals = tuple(
        int(vertical["vertical_id"])
        for vertical in connection.execute(
            "SELECT vertical_id FROM person_vertical"
            " WHERE person_id = ? ORDER BY vertical_id",
            (person_id,),
        ).fetchall()
    )
    return AuthenticatedPerson(
        person_id=person_id,
        role=str(row["role"]),
        vertical_ids=verticals,
    )


def revoke_session(
    connection: sqlite3.Connection, token: str, *, now: str
) -> bool:
    """Revoke one session; ``True`` when this call revoked it.

    Signing out twice is not an error: the second call reports ``False``.
    """
    stripped = token.strip()
    if stripped == "":
        return False
    revoked_at = _canonical(_instant(now))
    with transaction(connection):
        cursor = connection.execute(
            "UPDATE auth_session SET revoked_at = ?"
            " WHERE token_hash = ? AND revoked_at IS NULL",
            (revoked_at, _token_hash(stripped)),
        )
        return cursor.rowcount > 0


def _deliver(sender: OtpSender, discord_id: str, code: str) -> str:
    """Send the DM outside every transaction; never raise, never echo values."""
    try:
        sender.send(discord_id=discord_id, code=code)
    except OtpSenderNotConfigured:
        return REASON_SENDER_UNCONFIGURED
    except Exception:  # noqa: BLE001 - a failed DM must not fail sign-in
        return REASON_SENDER_FAILED
    return REASON_SENT


def _create_session(
    connection: sqlite3.Connection,
    *,
    person_id: int,
    instant: datetime,
    settings: AuthSettings,
) -> Session:
    token = secrets.token_urlsafe(32)
    issued_at = _canonical(instant)
    expires_at = _canonical(
        instant + timedelta(seconds=settings.session_lifetime_seconds)
    )
    connection.execute(
        "INSERT INTO auth_session"
        " (person_id, token_hash, issued_at, expires_at) VALUES (?, ?, ?, ?)",
        (person_id, _token_hash(token), issued_at, expires_at),
    )
    return Session(
        token=token,
        person_id=person_id,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def _person_by_roll_number(
    connection: sqlite3.Connection, roll_number: str
) -> _Person | None:
    row = connection.execute(
        "SELECT id, active, discord_user_id FROM person WHERE roll_number = ?",
        (roll_number,),
    ).fetchone()
    if row is None:
        return None
    discord_id = row["discord_user_id"]
    return _Person(
        person_id=int(row["id"]),
        active=int(row["active"]) == 1,
        discord_id=None if discord_id is None else str(discord_id),
    )


def _open_challenge(
    connection: sqlite3.Connection, person_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM otp_challenge"
        " WHERE person_id = ? AND consumed_at IS NULL"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (person_id,),
    ).fetchone()


def _consume_open_challenges(
    connection: sqlite3.Connection, person_id: int, consumed_at: str
) -> None:
    """One active challenge per person: a new request ends the previous one."""
    connection.execute(
        "UPDATE otp_challenge SET consumed_at = ?"
        " WHERE person_id = ? AND consumed_at IS NULL",
        (consumed_at, person_id),
    )


def _increment_attempts(connection: sqlite3.Connection, challenge_id: int) -> None:
    connection.execute(
        "UPDATE otp_challenge SET attempts = attempts + 1 WHERE id = ?",
        (challenge_id,),
    )


def _recent_attempts(
    connection: sqlite3.Connection,
    roll_hash: str,
    kind: str,
    now: str,
    window_seconds: int,
    *,
    failures_only: bool = False,
) -> int:
    """Count this roll number's attempts inside the window (canonical UTC text)."""
    cutoff = _canonical(_instant(now) - timedelta(seconds=window_seconds))
    sql = (
        "SELECT COUNT(*) FROM auth_attempt"
        " WHERE roll_number_hash = ? AND kind = ? AND attempted_at >= ?"
    )
    if failures_only:
        sql += " AND succeeded = 0"
    row = connection.execute(sql, (roll_hash, kind, cutoff)).fetchone()
    return 0 if row is None else int(row[0])


def _record_attempt(
    connection: sqlite3.Connection,
    roll_hash: str,
    kind: str,
    attempted_at: str,
    *,
    succeeded: bool,
    reason: str,
) -> None:
    connection.execute(
        "INSERT INTO auth_attempt"
        " (roll_number_hash, kind, attempted_at, succeeded, reason)"
        " VALUES (?, ?, ?, ?, ?)",
        (roll_hash, kind, attempted_at, 1 if succeeded else 0, reason),
    )


def _new_code() -> str:
    """A 6-digit code from :mod:`secrets`, including leading zeros."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_code(code: str, salt: bytes, iterations: int) -> bytes:
    """PBKDF2-HMAC-SHA256 over the UTF-8 code (the only stored form)."""
    return hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt, iterations)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _roll_number_hash(roll_number: str) -> str:
    return hashlib.sha256(roll_number.encode("utf-8")).hexdigest()


def _instant(value: str) -> datetime:
    return parse_event_time(value)


def _canonical(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat()
