"""Schema-level tests for the 0022_evaluation migration (Phase 10 W5).

Covers the AI-04/05/07/08 provenance shape from the approved plan section 5:

* ``evaluation_run`` records the function, model/prompt versions, the dataset
  SHA-256, the case count and a non-blank metrics JSON payload.
* ``evaluation_case_result`` is unique per ``(run_id, case_id)`` and its run FK
  is ``ON DELETE RESTRICT``.
* Shape checks reject blank identity fields, negative counts and a ``correct``
  value outside 0/1.
* A fresh database applies 0001-0022 and a re-apply is a no-op.

Append-only write discipline is service-level (the plan adds no triggers, the
same convention as 0004); the application-level half is tested in
``tests/test_evaluation.py``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations

_RUN_SQL = (
    "INSERT INTO evaluation_run"
    " (function, model_id, model_version, prompt_version, dataset_sha256,"
    "  case_count, started_at, finished_at, metrics_json)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_CASE_SQL = (
    "INSERT INTO evaluation_case_result"
    " (run_id, case_id, expected_json, predicted_json, correct)"
    " VALUES (?, ?, ?, ?, ?)"
)


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _versions(connection: sqlite3.Connection) -> list[int]:
    rows = connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()
    return [int(row["version"]) for row in rows]


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _insert_run(connection: sqlite3.Connection, **overrides: Any) -> int:
    values: dict[str, Any] = {
        "function": "RELEVANCE",
        "model_id": "model-a",
        "model_version": "1.0",
        "prompt_version": "prompt-1",
        "dataset_sha256": "a" * 64,
        "case_count": 1,
        "started_at": "2026-01-01T10:00:00+00:00",
        "finished_at": "2026-01-01T10:00:01+00:00",
        "metrics_json": '{"non_production": false}',
    }
    values.update(overrides)
    cursor = connection.execute(
        _RUN_SQL,
        (
            values["function"],
            values["model_id"],
            values["model_version"],
            values["prompt_version"],
            values["dataset_sha256"],
            values["case_count"],
            values["started_at"],
            values["finished_at"],
            values["metrics_json"],
        ),
    )
    return int(cursor.lastrowid or 0)


def _insert_case(connection: sqlite3.Connection, run_id: int, **overrides: Any) -> int:
    values: dict[str, Any] = {
        "case_id": "b" * 64,
        "expected_json": '{"sha256": "c"}',
        "predicted_json": '{"sha256": "d", "latency_ms": 1.0}',
        "correct": 1,
    }
    values.update(overrides)
    cursor = connection.execute(
        _CASE_SQL,
        (
            run_id,
            values["case_id"],
            values["expected_json"],
            values["predicted_json"],
            values["correct"],
        ),
    )
    return int(cursor.lastrowid or 0)


def test_0022_creates_the_two_evaluation_tables(conn: sqlite3.Connection) -> None:
    assert {"evaluation_run", "evaluation_case_result"} <= _table_names(conn)

    run_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(evaluation_run)").fetchall()
    }
    assert run_columns == {
        "id",
        "function",
        "model_id",
        "model_version",
        "prompt_version",
        "dataset_sha256",
        "case_count",
        "started_at",
        "finished_at",
        "metrics_json",
    }

    case_columns = {
        str(row["name"])
        for row in conn.execute(
            "PRAGMA table_info(evaluation_case_result)"
        ).fetchall()
    }
    assert case_columns == {
        "id",
        "run_id",
        "case_id",
        "expected_json",
        "predicted_json",
        "correct",
    }


def test_0022_run_shape_checks(conn: sqlite3.Connection) -> None:
    run_id = _insert_run(conn)
    assert run_id > 0

    for overrides in (
        {"function": "UNKNOWN"},
        {"model_id": ""},
        {"model_id": "   "},
        {"model_version": ""},
        {"prompt_version": ""},
        {"dataset_sha256": ""},
        {"case_count": -1},
        {"started_at": ""},
        {"finished_at": ""},
        {"metrics_json": ""},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(conn, **overrides)

    # finished_at is optional, and a NULL predicted_json is allowed.
    optional_run = _insert_run(conn, finished_at=None)
    case_id = _insert_case(conn, optional_run, predicted_json=None)
    assert case_id > 0


def test_0022_case_result_shape_checks(conn: sqlite3.Connection) -> None:
    run_id = _insert_run(conn)

    for overrides in (
        {"case_id": ""},
        {"case_id": "   "},
        {"expected_json": ""},
        {"predicted_json": ""},
        {"predicted_json": "   "},
        {"correct": 2},
        {"correct": -1},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            _insert_case(conn, run_id, **overrides)


def test_0022_case_result_is_unique_per_run_and_case(conn: sqlite3.Connection) -> None:
    run_id = _insert_run(conn)
    _insert_case(conn, run_id, case_id="same-case-hash")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_case(conn, run_id, case_id="same-case-hash")

    # The same case hash in a different run is allowed.
    other_run = _insert_run(conn)
    _insert_case(conn, other_run, case_id="same-case-hash")


def test_0022_run_foreign_key_is_restrict(conn: sqlite3.Connection) -> None:
    foreign_keys = conn.execute(
        "PRAGMA foreign_key_list(evaluation_case_result)"
    ).fetchall()
    assert len(foreign_keys) == 1
    assert str(foreign_keys[0]["table"]) == "evaluation_run"
    assert str(foreign_keys[0]["on_delete"]) == "RESTRICT"

    with pytest.raises(sqlite3.IntegrityError):
        _insert_case(conn, 9999)

    run_id = _insert_run(conn)
    _insert_case(conn, run_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM evaluation_run WHERE id = ?", (run_id,))


def test_0022_fresh_apply_and_noop_reapply(tmp_path: Path) -> None:
    connection = connect(tmp_path / "fresh.db")
    try:
        assert apply_migrations(connection) == list(range(1, 26))
        assert apply_migrations(connection) == []
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_0022_reapply_leaves_populated_evaluation_rows_untouched(
    conn: sqlite3.Connection,
) -> None:
    run_id = _insert_run(conn)
    _insert_case(conn, run_id)
    before = conn.execute(
        "SELECT * FROM evaluation_run WHERE id = ?", (run_id,)
    ).fetchone()
    assert before is not None

    assert apply_migrations(conn) == []

    after = conn.execute(
        "SELECT * FROM evaluation_run WHERE id = ?", (run_id,)
    ).fetchone()
    assert after is not None
    assert dict(after) == dict(before)
