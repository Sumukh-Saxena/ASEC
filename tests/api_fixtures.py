"""Shared stdlib ASGI caller and synthetic API harness (W2).

There is no ``httpx``: the app is driven by handing it a plain ASGI scope and
collecting the ``http.response.start`` / ``http.response.body`` messages, which
is all the framework's own test client does at the bottom. The one real-socket
check lives in ``tests/test_api_server_smoke.py`` and uses uvicorn plus
``urllib``.

Every value here is synthetic. An OTP code exists only in memory, as the
recording sender's output; no fixture writes one to disk, a log or a response.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.api import create_app
from backend.config import Config
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.notification_delivery import RecordingOtpSender

#: One fixed instant; tests move it explicitly and nothing sleeps.
NOW = "2026-11-13T09:00:00+00:00"

ADMIN_ROLL = "R-ADMIN"
HEAD_ROLL = "R-HEAD"
VOLUNTEER_ROLL = "R-VOL"
INACTIVE_ROLL = "R-INACTIVE"
NO_DISCORD_ROLL = "R-NODIS"
UNKNOWN_ROLL = "R-NOBODY"

VOLUNTEER_DISCORD = "100000000000000004"

#: A roll number that never exists in the fixture roster.
NOBODY = UNKNOWN_ROLL


@dataclass(frozen=True)
class ApiResponse:
    """One collected ASGI response, plus any exception raised after it."""

    status: int
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""
    raised: BaseException | None = None

    def header(self, name: str) -> str | None:
        """Return the first header value with this name, case-insensitively."""
        lowered = name.lower()
        for key, value in self.headers:
            if key.lower() == lowered:
                return value
        return None

    def json(self) -> Any:
        """Decode the body as JSON; the caller asserted the status first."""
        return json.loads(self.body.decode("utf-8"))


@dataclass
class Clock:
    """The injected clock: one value, moved by the test when it means to."""

    value: str = NOW

    def __call__(self) -> str:
        return self.value

    def advance(self, seconds: float) -> None:
        instant = datetime.fromisoformat(self.value)
        self.value = (instant + timedelta(seconds=seconds)).astimezone(UTC).isoformat()


def call(
    app: Any,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    body: bytes | None = None,
    query: str = "",
    content_length: int | None = None,
) -> ApiResponse:
    """Drive one request through the ASGI app without a network socket."""
    payload = body
    if json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
    raw: dict[str, str] = {"accept": "application/json"}
    if payload is not None:
        raw["content-type"] = "application/json"
    if headers is not None:
        raw.update({key.lower(): value for key, value in headers.items()})
    if content_length is not None:
        raw["content-length"] = str(content_length)
    elif payload is not None:
        raw["content-length"] = str(len(payload))

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query.encode("utf-8"),
        "root_path": "",
        "headers": [
            (key.encode("latin-1"), value.encode("latin-1"))
            for key, value in raw.items()
        ],
        "client": ("127.0.0.1", 54321),
        "server": ("testserver", 80),
    }
    return asyncio.run(_asgi_call(app, scope, b"" if payload is None else payload))


async def _asgi_call(
    app: Any, scope: dict[str, Any], payload: bytes
) -> ApiResponse:
    messages: list[dict[str, Any]] = []
    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    raised: BaseException | None = None
    try:
        await app(scope, receive, send)
    except BaseException as error:
        # ``ServerErrorMiddleware`` sends the handler's response and then
        # re-raises, so the response is collected and the error is kept for the
        # test that expects it.
        raised = error

    status = 0
    headers: list[tuple[str, str]] = []
    chunks: list[bytes] = []
    for message in messages:
        if message["type"] == "http.response.start":
            status = int(message["status"])
            headers = [
                (key.decode("latin-1"), value.decode("latin-1"))
                for key, value in message.get("headers", [])
            ]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    if status == 0 and raised is None:
        raise AssertionError("the ASGI app sent no response at all")
    return ApiResponse(
        status=status,
        headers=tuple(headers),
        body=b"".join(chunks),
        raised=raised,
    )


def seed_database(database: Path) -> None:
    """Create one migrated database with a small synthetic reporting tree."""
    connection = connect(database)
    try:
        apply_migrations(connection)
        connection.execute("INSERT INTO vertical (id, name) VALUES (1, 'Logistics')")
        connection.execute("INSERT INTO vertical (id, name) VALUES (2, 'Decor')")
        _add_person(
            connection, 1, "Admin One", "Admin", ADMIN_ROLL, "100000000000000001", 2
        )
        _add_person(
            connection, 2, "Head One", "Head", HEAD_ROLL, "100000000000000002", 1
        )
        _add_person(
            connection,
            3,
            "AH One",
            "AH",
            "R-AH",
            "100000000000000003",
            1,
            reports_to=2,
        )
        _add_person(
            connection,
            4,
            "Volunteer One",
            "Volunteer",
            VOLUNTEER_ROLL,
            VOLUNTEER_DISCORD,
            1,
            reports_to=3,
        )
        _add_person(
            connection,
            5,
            "Inactive One",
            "Volunteer",
            INACTIVE_ROLL,
            "100000000000000005",
            1,
            reports_to=3,
            active=0,
        )
        _add_person(
            connection, 6, "No Discord", "Volunteer", NO_DISCORD_ROLL, None, 1
        )
    finally:
        connection.close()


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
        " (id, name, discord_user_id, phone, role, reports_to, active,"
        " roll_number)"
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


def build_app(
    database: Path,
    *,
    clock: Clock,
    sender: RecordingOtpSender,
    config: Config | None = None,
    connection_factory: Callable[[], sqlite3.Connection] | None = None,
) -> Any:
    """Build one app over ``database`` with an injected clock and sender."""
    factory = (
        connection_factory
        if connection_factory is not None
        else (lambda: connect(database))
    )
    return create_app(
        connection_factory=factory,
        now=clock,
        config=Config({}) if config is None else config,
        otp_sender=sender,
    )


@dataclass
class ApiHarness:
    """A migrated database, one app and the pieces a test needs to drive it."""

    app: Any
    clock: Clock
    sender: RecordingOtpSender
    database: Path

    def call(self, method: str, path: str, **kwargs: Any) -> ApiResponse:
        return call(self.app, method, path, **kwargs)

    def request_code(self, roll_number: str = VOLUNTEER_ROLL) -> ApiResponse:
        """Ask for a code through the route and return the 202 response."""
        return self.call("POST", "/auth/otp", json_body={"roll_number": roll_number})

    def last_code(self) -> str:
        """The code the recording sender just received (in memory only)."""
        if not self.sender.sent:
            raise AssertionError("no OTP was handed to the sender")
        return self.sender.sent[-1][1]

    def verify(self, code: str, roll_number: str = VOLUNTEER_ROLL) -> ApiResponse:
        return self.call(
            "POST",
            "/auth/verify",
            json_body={"roll_number": roll_number, "code": code},
        )

    def sign_in(self, roll_number: str = VOLUNTEER_ROLL) -> str:
        """Run the real two-step sign-in and return the issued token."""
        accepted = self.request_code(roll_number)
        if accepted.status != 202:
            raise AssertionError(f"unexpected OTP status {accepted.status}")
        verified = self.verify(self.last_code(), roll_number)
        if verified.status != 200:
            raise AssertionError(f"unexpected verify status {verified.status}")
        return str(verified.json()["token"])

    def me(self, token: str) -> ApiResponse:
        return self.call("GET", "/me", headers={"authorization": f"Bearer {token}"})

    def logout(self, token: str) -> ApiResponse:
        return self.call(
            "POST",
            "/auth/logout",
            headers={"authorization": f"Bearer {token}"},
        )


def build_harness(
    tmp_path: Path,
    *,
    sender: RecordingOtpSender | None = None,
    config: Config | None = None,
    database_name: str = "api.db",
) -> ApiHarness:
    """Seed a database and build the app over it."""
    database = tmp_path / database_name
    seed_database(database)
    clock = Clock()
    recording = RecordingOtpSender() if sender is None else sender
    app = build_app(database, clock=clock, sender=recording, config=config)
    return ApiHarness(app=app, clock=clock, sender=recording, database=database)
