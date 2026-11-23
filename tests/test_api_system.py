"""Admin system routes and their effect on the live tick (D2).

The route gate admits Admin only; the response is the two flags, never an actor
id, a person row or a timestamp. Delivery is skipped at the live-tick hook while
ingestion still polls, which is owner decision C's "ingestion always continues".
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend.system_control import (  # noqa: E402
    DELIVERY,
    PROCESSING,
    pause,
)
from backend.worker import run_live_tick  # noqa: E402
from tests.api_fixtures import (  # noqa: E402
    ADMIN_ROLL,
    HEAD_ROLL,
    ApiHarness,
    build_harness,
    query,
)

_T0 = "2026-11-18T10:00:00+00:00"


class _Source:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_messages(self, *, channel_id: str, after: str | None) -> tuple[()]:
        self.calls.append(channel_id)
        return ()

    def fetch_reactions(
        self, *, channel_id: str, message_id: str
    ) -> tuple[()]:
        return ()

    def fetch_latest_page(self, *, channel_id: str) -> tuple[()]:
        return ()


class _Delivery:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


@pytest.fixture()
def harness(tmp_path: Path) -> ApiHarness:
    return build_harness(tmp_path)


def _headers(harness: ApiHarness, roll: str) -> dict[str, str]:
    return {"authorization": f"Bearer {harness.sign_in(roll)}"}


def test_system_read_starts_with_both_switches_resumed(
    harness: ApiHarness,
) -> None:
    response = harness.call(
        "GET", "/admin/system", headers=_headers(harness, ADMIN_ROLL)
    )
    assert response.status == 200
    assert response.json() == {
        "processing_paused": False,
        "delivery_paused": False,
    }


def test_pause_and_resume_are_per_switch_and_visible_on_read(
    harness: ApiHarness,
) -> None:
    headers = _headers(harness, ADMIN_ROLL)
    paused = harness.call(
        "POST",
        "/admin/system/pause",
        headers=headers,
        json_body={"control": PROCESSING},
    )
    assert paused.status == 200
    assert paused.json() == {
        "processing_paused": True,
        "delivery_paused": False,
    }

    both = harness.call(
        "POST",
        "/admin/system/pause",
        headers=headers,
        json_body={"control": DELIVERY},
    )
    assert both.json() == {"processing_paused": True, "delivery_paused": True}

    read = harness.call("GET", "/admin/system", headers=headers)
    assert read.json() == {"processing_paused": True, "delivery_paused": True}

    resumed = harness.call(
        "POST",
        "/admin/system/resume",
        headers=headers,
        json_body={"control": PROCESSING},
    )
    assert resumed.json() == {
        "processing_paused": False,
        "delivery_paused": True,
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/admin/system"),
        ("POST", "/admin/system/pause"),
        ("POST", "/admin/system/resume"),
    ],
)
def test_only_an_admin_may_read_or_change_the_switches(
    harness: ApiHarness, method: str, path: str
) -> None:
    body = {"control": PROCESSING} if method == "POST" else None
    response = harness.call(
        method,
        path,
        headers=_headers(harness, HEAD_ROLL),
        json_body=body,
    )
    assert response.status == 403
    rows = query(
        harness.database,
        "SELECT * FROM authorization_denial ORDER BY id DESC LIMIT 1",
    )
    assert rows
    assert rows[0]["reason"] == "NOT_ROLE"
    assert rows[0]["subject_type"] == "ADMIN"


def test_an_unknown_control_value_is_a_422_and_changes_nothing(
    harness: ApiHarness,
) -> None:
    response = harness.call(
        "POST",
        "/admin/system/pause",
        headers=_headers(harness, ADMIN_ROLL),
        json_body={"control": "EVERYTHING"},
    )
    assert response.status == 422
    assert query(harness.database, "SELECT * FROM system_control") == []


def test_pausing_delivery_skips_the_hook_but_ingestion_still_polls(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    connection = harness.app.state.connection_factory()
    try:
        connection.execute(
            "INSERT INTO source_group (id, discord_channel_id) VALUES (1, 'chan-1')"
        )
        pause(connection, control=DELIVERY, actor_person_id=1, now=_T0)
        source = _Source()
        delivery = _Delivery()

        result = run_live_tick(
            connection,
            source=source,
            now=lambda: _T0,
            delivery_tick=delivery,
        )
    finally:
        connection.close()

    assert source.calls == ["chan-1"]
    assert len(result.polls) == 1
    assert delivery.calls == 0
    assert len(result.tick.groups) == 1
