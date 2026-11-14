"""The one real-socket check: uvicorn on an ephemeral loopback port (W2).

Everything else drives the ASGI app in-process. This test proves the app also
works behind a real server and a real HTTP client (``urllib`` from the standard
library, never ``httpx``), and that it binds loopback only.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from tests.api_fixtures import ApiHarness, build_harness  # noqa: E402

START_TIMEOUT_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 10.0


@dataclass
class _Served:
    server: Any
    thread: threading.Thread
    port: int

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(STOP_TIMEOUT_SECONDS)


def _start(app: Any) -> _Served:
    import uvicorn

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn-test", daemon=True)
    thread.start()
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while not server.started:
        if time.monotonic() > deadline:
            server.should_exit = True
            thread.join(STOP_TIMEOUT_SECONDS)
            raise AssertionError("uvicorn did not start in time")
        time.sleep(0.01)
    port = int(server.servers[0].sockets[0].getsockname()[1])
    return _Served(server=server, thread=thread, port=port)


def _get(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status), dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return int(error.code), dict(error.headers), error.read()


def test_healthz_and_auth_over_a_real_socket(tmp_path: Path) -> None:
    """One request that succeeds and one that is refused, over real HTTP."""
    harness: ApiHarness = build_harness(tmp_path)
    served = _start(harness.app)
    try:
        status, headers, body = _get(served.port, "/healthz")
        assert status == 200
        assert json.loads(body) == {"status": "ok"}
        assert headers.get("x-request-id")

        status, _headers, body = _get(served.port, "/me")
        assert status == 401
        payload = json.loads(body)
        assert payload["error"]["code"] == "UNAUTHENTICATED"
        assert payload["error"]["request_id"]
    finally:
        served.stop()
    assert not served.thread.is_alive()


def test_run_server_uses_the_loopback_app_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_server`` is the only uvicorn call, and it takes the built app."""
    from backend.api import server as api_server

    harness = build_harness(tmp_path)
    seen: dict[str, object] = {}

    class FakeUvicorn:
        @staticmethod
        def run(app: Any, *, host: str, port: int, log_level: str) -> None:
            seen.update(app=app, host=host, port=port, log_level=log_level)

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
    api_server.run_server(harness.app, host="127.0.0.1", port=8123)
    assert seen == {
        "app": harness.app,
        "host": "127.0.0.1",
        "port": 8123,
        "log_level": "info",
    }
