"""The two system pause switches (owner decision C, Phase 12 D2).

Processing and delivery pause independently, and ingestion is not one of the
switches. The service re-checks the caller's current roster role even though the
route gate already does, and every write is one row per control so a repeated
pause is idempotent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.authorization import AuthorizationError
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.system_control import (
    DELIVERY,
    PROCESSING,
    InvalidControlError,
    SystemControlAuthorizationError,
    is_delivery_paused,
    is_processing_paused,
    pause,
    read_state,
    resume,
)
from backend.worker import run_pipeline_tick
from tests.notification_fixtures import seed_roster

_NOW = "2026-11-18T10:00:00+00:00"


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "system.db")
    apply_migrations(connection)
    seed_roster(connection)
    yield connection
    connection.close()


def test_both_switches_default_to_resumed(conn: sqlite3.Connection) -> None:
    state = read_state(conn)
    assert state.processing_paused is False
    assert state.delivery_paused is False
    assert is_processing_paused(conn) is False
    assert is_delivery_paused(conn) is False


def test_processing_and_delivery_pause_independently(conn: sqlite3.Connection) -> None:
    paused = pause(
        conn, control=PROCESSING, actor_person_id=6, now=_NOW
    )
    assert paused.processing_paused is True
    assert paused.delivery_paused is False

    both = pause(conn, control=DELIVERY, actor_person_id=6, now=_NOW)
    assert both.processing_paused is True
    assert both.delivery_paused is True

    processed = resume(conn, control=PROCESSING, actor_person_id=6, now=_NOW)
    assert processed.processing_paused is False
    assert processed.delivery_paused is True

    resumed = resume(conn, control=DELIVERY, actor_person_id=6, now=_NOW)
    assert resumed.processing_paused is False
    assert resumed.delivery_paused is False


def test_a_repeated_pause_keeps_one_row_and_stays_paused(
    conn: sqlite3.Connection,
) -> None:
    pause(conn, control=PROCESSING, actor_person_id=6, now=_NOW)
    pause(conn, control=PROCESSING, actor_person_id=6, now=_NOW)
    rows = conn.execute(
        "SELECT name, paused FROM system_control ORDER BY name"
    ).fetchall()
    assert [(row["name"], row["paused"]) for row in rows] == [("PROCESSING", 1)]


def test_a_non_admin_cannot_change_a_switch_and_the_denial_is_recorded(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(SystemControlAuthorizationError) as caught:
        pause(conn, control=PROCESSING, actor_person_id=1, now=_NOW)
    assert caught.value.reason == "NOT_ROLE"
    row = conn.execute(
        "SELECT * FROM authorization_denial ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["operation"] == "SYSTEM_PAUSE"
    assert row["reason"] == "NOT_ROLE"
    assert is_processing_paused(conn) is False


def test_an_inactive_admin_cannot_change_a_switch(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE person SET active = 0 WHERE id = 6")
    with pytest.raises(AuthorizationError):
        resume(conn, control=DELIVERY, actor_person_id=6, now=_NOW)
    assert is_delivery_paused(conn) is False


def test_an_unknown_switch_is_rejected_without_a_write(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(InvalidControlError):
        pause(conn, control="EVERYTHING", actor_person_id=6, now=_NOW)
    assert conn.execute("SELECT COUNT(*) AS n FROM system_control").fetchone()["n"] == 0


def test_a_paused_processing_switch_stops_the_pipeline_tick(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (1, 'chan-1')"
    )
    conn.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " event_timestamp, received_at, content)"
        " VALUES (1, 'discord', 'm-1', 1, ?, ?, 'content')",
        (_NOW, _NOW),
    )
    pause(conn, control=PROCESSING, actor_person_id=6, now=_NOW)

    result = run_pipeline_tick(conn, now=_NOW)

    assert result.groups == ()
    assert result.assigned == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM bucket").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM source_group_clock").fetchone()[
        "n"
    ] == 0


def test_resuming_processing_makes_the_next_tick_run(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (1, 'chan-1')"
    )
    pause(conn, control=PROCESSING, actor_person_id=6, now=_NOW)
    resume(conn, control=PROCESSING, actor_person_id=6, now=_NOW)

    result = run_pipeline_tick(conn, now=_NOW)

    assert len(result.groups) == 1
