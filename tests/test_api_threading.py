"""Blocking handlers run in the framework's thread pool (W3 deviation).

Plan §4.2.4 originally ran every handler on the event loop. Session B requires
plain ``def`` handlers and dependencies so blocking SQLite and sender calls run
off the loop, with the per-request connection opened, used and closed on the
same thread (never ``check_same_thread=False``). These tests prove both: one
blocked sender does not stop another request, and no connection object is
touched from a second thread.
"""

from __future__ import annotations

import ast
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from backend.config import Config  # noqa: E402
from backend.db.connection import connect  # noqa: E402
from backend.notification_delivery import RecordingOtpSender  # noqa: E402
from tests.api_fixtures import (  # noqa: E402
    ADMIN_ROLL,
    VOLUNTEER_DISCORD,
    VOLUNTEER_ROLL,
    Clock,
    build_app,
    call,
    seed_database,
    seed_world,
)


class _ThreadCheckingConnection:
    """A connection proxy that fails if any use lands on another thread."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.opened_on = threading.get_ident()
        self.used_on: set[int] = set()

    def _check(self) -> None:
        current = threading.get_ident()
        self.used_on.add(current)
        if current != self.opened_on:
            raise AssertionError(
                "a SQLite connection was used from a thread other than the one"
                " that opened it"
            )

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        self._check()
        return self._connection.execute(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        self._check()
        return self._connection.executemany(*args, **kwargs)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        self._check()
        return self._connection.executescript(*args, **kwargs)

    def cursor(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        self._check()
        return self._connection.cursor(*args, **kwargs)

    def commit(self) -> None:
        self._check()
        self._connection.commit()

    def rollback(self) -> None:
        self._check()
        self._connection.rollback()

    def close(self) -> None:
        self._check()
        self._connection.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _SlowSender(RecordingOtpSender):
    """Blocks only for the volunteer's Discord id, so setup can proceed."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def send(self, *, discord_id: str, code: str) -> None:
        if discord_id == VOLUNTEER_DISCORD:
            self.entered.set()
            if not self.release.wait(timeout=20):
                raise AssertionError("the slow sender was never released")
        super().send(discord_id=discord_id, code=code)


def _app(
    tmp_path: Path, sender: RecordingOtpSender
) -> tuple[Any, list[_ThreadCheckingConnection], Path]:
    database = tmp_path / "threads.db"
    seed_database(database)
    seed_world(database)
    opened: list[_ThreadCheckingConnection] = []
    lock = threading.Lock()

    def factory() -> _ThreadCheckingConnection:
        proxy = _ThreadCheckingConnection(connect(database))
        with lock:
            opened.append(proxy)
        return proxy

    app = build_app(
        database,
        clock=Clock(),
        sender=sender,
        config=Config({}),
        connection_factory=factory,
    )
    return app, opened, database


def _sign_in(app: Any, roll: str) -> str:
    accepted = call(app, "POST", "/auth/otp", json_body={"roll_number": roll})
    assert accepted.status == 202
    sender = app.state.otp_sender
    code = sender.sent[-1][1]
    verified = call(
        app,
        "POST",
        "/auth/verify",
        json_body={"roll_number": roll, "code": code},
    )
    assert verified.status == 200
    return str(verified.json()["token"])


def test_a_blocked_sender_does_not_block_another_request(tmp_path: Path) -> None:
    sender = _SlowSender()
    app, opened, _database = _app(tmp_path, sender)
    admin_token = _sign_in(app, ADMIN_ROLL)

    blocked_result: dict[str, Any] = {}

    def blocked_request() -> None:
        blocked_result["response"] = call(
            app,
            "POST",
            "/auth/otp",
            json_body={"roll_number": VOLUNTEER_ROLL},
        )

    worker = threading.Thread(target=blocked_request, daemon=True)
    worker.start()
    assert sender.entered.wait(timeout=10), "the fake sender was never reached"

    started = time.monotonic()
    other = call(
        app,
        "GET",
        "/me",
        headers={"authorization": f"Bearer {admin_token}"},
    )
    elapsed = time.monotonic() - started

    assert other.status == 200
    assert elapsed < 10
    assert not sender.release.is_set()
    sender.release.set()
    worker.join(timeout=20)
    assert not worker.is_alive()
    assert blocked_result["response"].status == 202


def test_no_connection_object_is_used_from_another_thread(tmp_path: Path) -> None:
    sender = _SlowSender()
    app, opened, _database = _app(tmp_path, sender)
    admin_token = _sign_in(app, ADMIN_ROLL)

    worker = threading.Thread(
        target=lambda: call(
            app,
            "POST",
            "/auth/otp",
            json_body={"roll_number": VOLUNTEER_ROLL},
        ),
        daemon=True,
    )
    worker.start()
    assert sender.entered.wait(timeout=10)
    assert call(
        app, "GET", "/me", headers={"authorization": f"Bearer {admin_token}"}
    ).status == 200
    sender.release.set()
    worker.join(timeout=20)

    assert opened, "the app opened no connection at all"
    for proxy in opened:
        assert proxy.used_on == {proxy.opened_on}
    assert len({id(proxy) for proxy in opened}) == len(opened)


def test_each_concurrent_request_gets_its_own_connection(tmp_path: Path) -> None:
    sender = _SlowSender()
    app, opened, _database = _app(tmp_path, sender)
    admin_token = _sign_in(app, ADMIN_ROLL)
    before = len(opened)

    worker = threading.Thread(
        target=lambda: call(
            app,
            "POST",
            "/auth/otp",
            json_body={"roll_number": VOLUNTEER_ROLL},
        ),
        daemon=True,
    )
    worker.start()
    assert sender.entered.wait(timeout=10)
    assert call(
        app, "GET", "/me", headers={"authorization": f"Bearer {admin_token}"}
    ).status == 200
    sender.release.set()
    worker.join(timeout=20)

    assert len(opened) - before >= 2


def test_route_handlers_are_plain_def_and_never_relax_sqlite_threading() -> None:
    source = Path("backend/api/handlers.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    handlers = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "router"
            for decorator in node.decorator_list
        )
    ]
    assert handlers, "no router handlers were found"
    async_handlers = [
        node.name for node in handlers if isinstance(node, ast.AsyncFunctionDef)
    ]
    assert async_handlers == []
    assert "check_same_thread" not in source
    for path in Path("backend").rglob("*.py"):
        assert "check_same_thread" not in path.read_text(encoding="utf-8")
