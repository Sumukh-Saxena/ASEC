"""Replay importer tests (W4; FR-ING-08, O9-4 amendment, migration 0021).

The importer is the only replay path: JSONL export lines are normalized with
an explicit time zone, mapped from export sender labels and group labels to
Discord identities and existing source groups, given deterministic
content-addressed message IDs when the export carries none, and written
through :func:`backend.ingest.ingest_message` — exactly the live path. The
batch row records provenance and counts; a re-import reports duplicates and
never rewrites stored messages (DR-03).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from backend import replay
from backend.bucketing import BucketParams, assign_messages
from backend.clock import advance_group_clock, read_group_clock
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.replay import ReplayBatch, ReplayError, import_export

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_IST = "+05:30"


def _iso(seconds: int) -> str:
    return (_BASE + timedelta(seconds=seconds)).isoformat()


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "replay.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _write_export(tmp_path: Path, lines: list[dict[str, object]]) -> Path:
    path = tmp_path / "export.jsonl"
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


def _group(connection: sqlite3.Connection, group_id: int, channel: str) -> None:
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id) VALUES (?, ?)",
        (group_id, channel),
    )


def _person(
    connection: sqlite3.Connection, person_id: int, discord_user_id: str
) -> None:
    connection.execute(
        "INSERT INTO person (id, name, discord_user_id, phone, role, active, roll_number)"
        " VALUES (?, ?, ?, ?, 'Head', 1, ?)",
        (
            person_id,
            f"Person {person_id}",
            discord_user_id,
            f"phone-{person_id}",
            f"roll-{person_id}",
        ),
    )


def _messages(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT id, source, source_message_id, group_id, sender_person_id,"
        " sender_discord_user_id, event_timestamp, received_at, content,"
        " flag_unknown_sender FROM source_message ORDER BY id"
    ).fetchall()


def _line(
    *,
    group: str = "OG PR",
    timestamp: str = "2026-01-01T10:00:00",
    sender: str | None = "Alice",
    content: str = "hello",
    **extra: object,
) -> dict[str, object]:
    line: dict[str, object] = {
        "group": group,
        "timestamp": timestamp,
        "sender": sender,
        "content": content,
    }
    line.update(extra)
    return line


def _derived_id(
    group: str, timestamp: str, sender: str, content: str
) -> str:
    seed = f"{group}|{timestamp}|{sender}|{content}".encode()
    return "replay:" + hashlib.sha256(seed).hexdigest()[:32]


# --- complete mapping (R-1) --------------------------------------------------


def test_complete_mapping_resolves_senders_groups_and_feeds_bucketing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    _person(conn, 1, "900000000000000001")
    path = _write_export(
        tmp_path,
        [
            _line(timestamp="2026-01-01T10:00:00"),
            _line(timestamp="2026-01-01T10:01:00", content="second"),
        ],
    )

    batch = import_export(
        conn,
        path,
        time_zone="Asia/Kolkata",
        sender_map={"Alice": "900000000000000001"},
        group_map={"OG PR": "chan-1"},
    )

    assert isinstance(batch, ReplayBatch)
    assert batch.outcome == "COMPLETED"
    assert batch.already_imported is False
    assert batch.counts["inserted"] == 2
    assert batch.counts["unknown_sender"] == 0
    rows = _messages(conn)
    assert len(rows) == 2
    assert [row["group_id"] for row in rows] == [1, 1]
    assert [row["sender_person_id"] for row in rows] == [1, 1]
    assert [row["sender_discord_user_id"] for row in rows] == [
        "900000000000000001",
        "900000000000000001",
    ]
    assert [row["event_timestamp"] for row in rows] == [
        f"2026-01-01T10:00:00{_IST}",
        f"2026-01-01T10:01:00{_IST}",
    ]
    assert all(str(row["source_message_id"]).startswith("replay:") for row in rows)
    assert [row["flag_unknown_sender"] for row in rows] == [0, 0]

    stored = read_group_clock(conn, 1)
    assert stored is not None
    assert stored.mode == "REPLAY"
    assert stored.clock_at == "2026-01-01T04:31:00+00:00"

    # Replay advances the group clock to the latest imported event time, so PB-1
    # settles a message only when ``t + C <= clock``. The last message is at the
    # boundary itself and waits for the next advance; the first is settled.
    assigned = assign_messages(conn, group_id=1, params=BucketParams(10, 5, 30, 3))
    assert len(assigned.assigned) == 1
    assert assigned.assigned[0][0] == rows[0]["id"]

    # One more tick of the same replay clock settles the rest; nothing is lost.
    advance_group_clock(conn, 1, now="2026-01-01T04:32:00+00:00", mode="REPLAY")
    rest = assign_messages(conn, group_id=1, params=BucketParams(10, 5, 30, 3))
    assert len(rest.assigned) == 1
    bases = conn.execute(
        "SELECT time_basis FROM source_message ORDER BY id"
    ).fetchall()
    assert [row["time_basis"] for row in bases] == ["EVENT", "EVENT"]


# --- unmapped senders (R-2, R-3) ---------------------------------------------


def test_empty_sender_map_stores_every_message_and_counts_unknown_senders(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    path = _write_export(
        tmp_path,
        [
            _line(sender="Alice"),
            _line(sender="Bob", content="second"),
        ],
    )

    batch = import_export(conn, path, group_map={"OG PR": "chan-1"})

    assert batch.outcome == "COMPLETED"
    assert batch.counts["inserted"] == 2
    assert batch.counts["unknown_sender"] == 2
    assert batch.counts["unmapped_sender_labels"] == ["Alice", "Bob"]
    rows = _messages(conn)
    assert len(rows) == 2
    assert [row["sender_person_id"] for row in rows] == [None, None]
    assert [row["sender_discord_user_id"] for row in rows] == [None, None]
    assert [row["flag_unknown_sender"] for row in rows] == [1, 1]


def test_partial_sender_map_counts_only_unmapped_lines(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    _person(conn, 1, "900000000000000001")
    path = _write_export(
        tmp_path,
        [
            _line(sender="Alice"),
            _line(sender="Bob", content="second"),
        ],
    )

    batch = import_export(
        conn,
        path,
        sender_map={"Alice": "900000000000000001"},
        group_map={"OG PR": "chan-1"},
    )

    assert batch.counts["unknown_sender"] == 1
    assert batch.counts["unmapped_sender_labels"] == ["Bob"]
    rows = _messages(conn)
    assert [row["sender_person_id"] for row in rows] == [1, None]


# --- group mapping aborts before any write (R-4) -----------------------------


@pytest.mark.parametrize(
    "group_map",
    [{}, {"Other group": "chan-1"}],
)
def test_unmapped_group_aborts_before_any_write(
    conn: sqlite3.Connection, tmp_path: Path, group_map: dict[str, str]
) -> None:
    _group(conn, 1, "chan-1")
    path = _write_export(
        tmp_path, [_line(content="secret content that must not leak")]
    )

    with pytest.raises(ReplayError) as raised:
        import_export(
            conn,
            path,
            sender_map={"Alice": "900000000000000001"},
            group_map=group_map,
        )

    assert "OG PR" in str(raised.value)
    assert "secret content" not in str(raised.value)
    assert _messages(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM replay_batch").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_group").fetchone()[0] == 1


def test_group_mapped_to_an_unknown_channel_aborts_before_any_write(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    path = _write_export(tmp_path, [_line()])

    with pytest.raises(ReplayError, match="OG PR"):
        import_export(conn, path, group_map={"OG PR": "missing-channel"})

    assert _messages(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM replay_batch").fetchone()[0] == 0


# --- deterministic message IDs (R-5, R-6) ------------------------------------


def test_identical_lines_collide_by_design_and_the_second_is_a_duplicate(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    line = _line()
    path = _write_export(tmp_path, [line, dict(line)])

    batch = import_export(conn, path, group_map={"OG PR": "chan-1"})

    assert batch.counts["inserted"] == 1
    assert batch.counts["duplicate"] == 1
    rows = _messages(conn)
    assert len(rows) == 1
    assert rows[0]["source_message_id"] == _derived_id(
        "OG PR", "2026-01-01T10:00:00", "Alice", "hello"
    )


def test_lines_differing_in_one_input_get_distinct_derived_ids(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    _group(conn, 2, "chan-2")
    path = _write_export(
        tmp_path,
        [
            _line(),
            _line(timestamp="2026-01-01T10:00:01"),
            _line(sender="Bob"),
            _line(content="different"),
            _line(group="OG PR 2"),
        ],
    )

    batch = import_export(
        conn,
        path,
        group_map={"OG PR": "chan-1", "OG PR 2": "chan-2"},
    )

    assert batch.counts["inserted"] == 5
    ids = [str(row["source_message_id"]) for row in _messages(conn)]
    assert len(set(ids)) == 5
    assert all(identifier.startswith("replay:") for identifier in ids)


def test_a_line_that_carries_an_id_keeps_it_verbatim(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    path = _write_export(
        tmp_path, [_line(source_message_id="m-42")]
    )

    import_export(conn, path, group_map={"OG PR": "chan-1"})

    assert _messages(conn)[0]["source_message_id"] == "m-42"


# --- idempotent re-import and mapping change (R-7, R-8) ---------------------


def test_reimport_is_idempotent_and_reports_duplicates(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    path = _write_export(
        tmp_path,
        [_line(), _line(content="second"), _line(content="third")],
    )

    first = import_export(conn, path, group_map={"OG PR": "chan-1"})
    second = import_export(conn, path, group_map={"OG PR": "chan-1"})

    assert first.counts["inserted"] == 3
    assert second.already_imported is True
    assert second.counts["inserted"] == 0
    assert second.counts["duplicate"] == 3
    assert len(_messages(conn)) == 3
    assert conn.execute("SELECT COUNT(*) FROM replay_batch").fetchone()[0] == 1


def test_mapping_change_does_not_renumber_or_rewrite_history(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    _person(conn, 1, "900000000000000001")
    _person(conn, 2, "900000000000000002")
    path = _write_export(tmp_path, [_line(), _line(content="second")])

    first = import_export(
        conn,
        path,
        sender_map={"Alice": "900000000000000001"},
        group_map={"OG PR": "chan-1"},
    )
    ids_before = [row["source_message_id"] for row in _messages(conn)]
    second = import_export(
        conn,
        path,
        sender_map={"Alice": "900000000000000002"},
        group_map={"OG PR": "chan-1"},
    )

    assert first.counts["inserted"] == 2
    assert second.already_imported is True
    assert second.counts["duplicate"] == 2
    rows = _messages(conn)
    assert [row["source_message_id"] for row in rows] == ids_before
    assert [row["sender_discord_user_id"] for row in rows] == [
        "900000000000000001",
        "900000000000000001",
    ]
    assert [row["sender_person_id"] for row in rows] == [1, 1]


# --- time zone and indicative counts ----------------------------------------


def test_naive_timestamps_are_localized_with_the_explicit_time_zone(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    path = _write_export(tmp_path, [_line()])

    batch = import_export(
        conn, path, time_zone="Asia/Kolkata", group_map={"OG PR": "chan-1"}
    )

    assert batch.counts["time_zone"] == "Asia/Kolkata"
    assert _messages(conn)[0]["event_timestamp"] == f"2026-01-01T10:00:00{_IST}"
    assert batch.counts["localized_timestamps"] == {
        _derived_id("OG PR", "2026-01-01T10:00:00", "Alice", "hello"): "2026-01-01T10:00:00"
    }


def test_default_time_zone_is_asia_kolkata(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    path = _write_export(tmp_path, [_line()])

    batch = import_export(conn, path, group_map={"OG PR": "chan-1"})

    assert batch.time_zone == "Asia/Kolkata"
    assert _messages(conn)[0]["event_timestamp"] == f"2026-01-01T10:00:00{_IST}"


def test_an_invalid_time_zone_aborts_before_any_write(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    path = _write_export(tmp_path, [_line()])

    with pytest.raises(ReplayError, match="Not/AZone"):
        import_export(
            conn, path, time_zone="Not/AZone", group_map={"OG PR": "chan-1"}
        )

    assert _messages(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM replay_batch").fetchone()[0] == 0


def test_an_aware_timestamp_is_kept_as_supplied(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    path = _write_export(
        tmp_path, [_line(timestamp="2026-01-01T10:00:00+00:00")]
    )

    import_export(conn, path, group_map={"OG PR": "chan-1"})

    assert _messages(conn)[0]["event_timestamp"] == "2026-01-01T10:00:00+00:00"


def test_missing_second_level_timestamps_mark_the_batch_indicative(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    path = _write_export(
        tmp_path,
        [
            _line(timestamp="2026-01-01T10:00"),
            _line(timestamp="2026-01-01T10:01:30", content="exact"),
        ],
    )

    batch = import_export(conn, path, group_map={"OG PR": "chan-1"})

    assert batch.counts["indicative"] is True
    assert batch.counts["missing_seconds"] == 1
    assert batch.counts["inserted"] == 2
    assert _messages(conn)[0]["event_timestamp"] == f"2026-01-01T10:00:00{_IST}"


# --- reactions and mentions (FR-ING-07 evidence) -----------------------------


def test_mentions_and_reactions_are_stored_as_evidence(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    path = _write_export(
        tmp_path,
        [
            _line(
                mentions=["900000000000000002"],
                reactions=[
                    {
                        "reactor": "900000000000000003",
                        "emoji": "thumbs-up",
                        "direction": "ADD",
                    }
                ],
            )
        ],
    )

    batch = import_export(conn, path, group_map={"OG PR": "chan-1"})

    assert batch.counts["mentions"] == 1
    assert batch.counts["reactions"] == 1
    assert conn.execute("SELECT COUNT(*) FROM message_mention").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM message_reaction").fetchone()[0] == 1


# --- batch provenance (0021) -------------------------------------------------


def test_batch_row_records_file_hash_time_zone_and_counts(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    path = _write_export(tmp_path, [_line()])

    batch = import_export(
        conn,
        path,
        group_map={"OG PR": "chan-1"},
        batch_metadata={"origin": "sample"},
    )

    row = conn.execute("SELECT * FROM replay_batch").fetchone()
    assert row is not None
    assert row["file_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert row["time_zone"] == "Asia/Kolkata"
    assert row["outcome"] == "COMPLETED"
    assert row["finished_at"] is not None
    counts = json.loads(str(row["counts_json"]))
    assert counts["inserted"] == 1
    assert counts["metadata"] == {"origin": "sample"}
    assert batch.batch_id == row["id"]


def test_a_failed_import_is_recorded_as_a_failed_batch(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _group(conn, 1, "chan-1")
    path = _write_export(tmp_path, [_line(), _line(content="second")])
    real_ingest = replay.ingest_message
    calls = {"count": 0}

    def flaky_ingest(connection: Any, event: Any) -> Any:
        calls["count"] += 1
        if calls["count"] == 2:
            raise sqlite3.OperationalError("simulated storage failure")
        return real_ingest(connection, event)

    monkeypatch.setattr(replay, "ingest_message", flaky_ingest)

    with pytest.raises(sqlite3.OperationalError):
        import_export(conn, path, group_map={"OG PR": "chan-1"})

    row = conn.execute("SELECT * FROM replay_batch").fetchone()
    assert row is not None
    assert row["outcome"] == "FAILED"
    assert row["finished_at"] is not None
    counts = json.loads(str(row["counts_json"]))
    assert counts["inserted"] == 1
    assert len(_messages(conn)) == 1


def test_replay_refuses_a_group_clock_already_in_live_mode(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _group(conn, 1, "chan-1")
    advance_group_clock(conn, 1, now=_iso(0), mode="LIVE")
    path = _write_export(tmp_path, [_line()])

    with pytest.raises(ReplayError, match="LIVE"):
        import_export(conn, path, group_map={"OG PR": "chan-1"})

    assert _messages(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM replay_batch").fetchone()[0] == 0
