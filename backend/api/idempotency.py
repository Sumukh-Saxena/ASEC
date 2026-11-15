"""Idempotency keys for mutating routes (owner A2, W3, migration 0025).

The contract the table and this module implement together:

* the key is a header value (``Idempotency-Key``), 16-64 characters from
  ``A-Z a-z 0-9 . _ : -``; a malformed value is a 422 and does no work;
* the stored row is scoped to ``(person, route, key)``, so one caller can never
  replay another caller's response and one key may be reused on a different
  route;
* ``request_hash`` is a SHA-256 over method, path and the raw request body, so
  the same key on a different request is a 422 ``IDEMPOTENCY_KEY_REUSED``;
* only a 2xx response is stored, and replay returns exactly the stored status
  and body without calling the service again;
* an expired row is treated as absent, and :func:`cleanup_expired` deletes
  expired rows in bounded batches (it is a function, not a scheduler).

No key, request body or response body is ever logged here.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from backend.api.errors import ApiError
from backend.config import IdempotencySettings
from backend.db.connection import transaction

#: The one header name the API reads for this contract.
KEY_HEADER = "Idempotency-Key"

#: The table's CHECK uses the same character class and window.
_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]+")


class IdempotencyKeyReused(Exception):
    """The key already stored a different request for this person and route."""


@dataclass(frozen=True)
class StoredResponse:
    """One replayable response: the exact status and JSON text stored."""

    status: int
    body: str


def parse_key(raw: str, settings: IdempotencySettings) -> str:
    """Return the key, or raise a 422 for a malformed one (before any work)."""
    if not _KEY_PATTERN.fullmatch(raw):
        raise ApiError("INVALID_REQUEST")
    if not settings.min_length <= len(raw) <= settings.max_length:
        raise ApiError("INVALID_REQUEST")
    return raw


def request_hash(method: str, path: str, body: bytes) -> str:
    """SHA-256 over the canonical request identity, as lowercase hex.

    The path is included, so the same key on ``/tasks/1/agreement`` and
    ``/tasks/2/agreement`` with an identical body is a reuse, not a replay.
    The route key is part of the stored uniqueness instead, not this digest.
    """
    digest = hashlib.sha256()
    digest.update(method.upper().encode("ascii"))
    digest.update(b"\n")
    digest.update(path.encode("utf-8"))
    digest.update(b"\n")
    digest.update(body)
    return digest.hexdigest()


def _expiry(now: str, settings: IdempotencySettings) -> str:
    instant = datetime.fromisoformat(now)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return (instant + timedelta(hours=settings.key_hours)).isoformat()


def lookup(
    connection: sqlite3.Connection,
    *,
    person_id: int,
    route_key: str,
    key: str,
    request_hash: str,
    now: str,
) -> StoredResponse | None:
    """Return the stored response for a live row, or None when absent/expired.

    A live row with a different request digest is a reuse and raises
    :class:`IdempotencyKeyReused`. Nothing is written here.
    """
    row = connection.execute(
        "SELECT request_hash, response_status, response_body FROM idempotency_key"
        " WHERE person_id = ? AND route_key = ? AND key = ? AND expires_at > ?",
        (person_id, route_key, key, now),
    ).fetchone()
    if row is None:
        return None
    if str(row["request_hash"]) != request_hash:
        raise IdempotencyKeyReused(route_key)
    return StoredResponse(status=int(row["response_status"]), body=str(row["response_body"]))


def store(
    connection: sqlite3.Connection,
    *,
    person_id: int,
    route_key: str,
    key: str,
    request_hash: str,
    status: int,
    body: str,
    now: str,
    settings: IdempotencySettings,
) -> bool:
    """Store one successful response; a non-2xx is never stored.

    Returns True when this call inserted the row. A concurrent request that
    won the unique key keeps its row and this call does nothing.
    """
    if not 200 <= status < 300:
        return False
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO idempotency_key (person_id, route_key, key,"
            " request_hash, response_status, response_body, created_at,"
            " expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (person_id, route_key, key) DO NOTHING",
            (
                person_id,
                route_key,
                key,
                request_hash,
                status,
                body,
                now,
                _expiry(now, settings),
            ),
        )
        return cursor.rowcount == 1


def cleanup_expired(
    connection: sqlite3.Connection, *, now: str, batch_limit: int
) -> int:
    """Delete at most ``batch_limit`` expired rows; return how many.

    Bounded on purpose: a caller (or an operator's cron, never this module)
    repeats the call until it returns 0. Live rows are never touched.
    """
    if batch_limit < 1:
        raise ValueError("batch_limit must be positive")
    with transaction(connection):
        cursor = connection.execute(
            "DELETE FROM idempotency_key WHERE id IN ("
            " SELECT id FROM idempotency_key WHERE expires_at <= ?"
            " ORDER BY expires_at, id LIMIT ?)",
            (now, batch_limit),
        )
        return int(cursor.rowcount)
