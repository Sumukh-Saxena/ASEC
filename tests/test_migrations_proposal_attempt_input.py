"""Schema-level tests for the 0014_proposal_attempt_input migration.

NFR-AUD-02 (§6.2) durable-input guarantee for the proposal stage, extended
by the owner's Phase 5 corrections C1/C2: the artifact stores the
*artifact-form* masked request (dispatch JSON plus the mention handle map,
so the resolution input is reconstructable), the adapter's raw response
retained after the same ``mask_pii`` + size-cap treatment as attempt
errors (NULL only when the adapter raised before returning), a sha256 over
the stored response text, and the Tier-2 rejection records with reason
codes (FR-PRP-13). The response/sha columns always agree: both NULL or
both set — enforced by a table CHECK.

Storage contract only: schema shape, uniqueness, CHECK and foreign-key
RESTRICT behavior. Serialization rules, capture timing and pass semantics
belong to backend.proposals (tests/test_proposal_service.py).
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.db.migrations import apply_migrations


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _group_and_bucket(conn: sqlite3.Connection, state: str = "CLOSED") -> int:
    group = conn.execute(
        "INSERT INTO source_group (discord_channel_id) VALUES ('chan-1')"
    )
    bucket = conn.execute(
        "INSERT INTO bucket (group_id, processing_state) VALUES (?, ?)",
        (group.lastrowid, state),
    )
    return int(bucket.lastrowid or 0)


def _attempt(conn: sqlite3.Connection, bucket_id: int, outcome: str = "SUCCEEDED") -> int:
    cursor = conn.execute(
        "INSERT INTO processing_attempt (stage, bucket_id, started_at,"
        " finished_at, outcome) VALUES ('PROPOSAL_GENERATION', ?,"
        " '2026-01-01T10:02:00+00:00', '2026-01-01T10:02:01+00:00', ?)",
        (bucket_id, outcome),
    )
    return int(cursor.lastrowid or 0)


def _artifact(
    conn: sqlite3.Connection,
    attempt_id: int,
    *,
    ruleset_version: str = "masking-rules-v1",
    policy_version: str = "proposal-prompt-v1",
    masked_request: str = '{"bucket_id":1,"mention_handle_map":{"@m1":"100"}}',
    request_sha256: str = "0" * 64,
    raw_response: str | None = None,
    response_sha256: str | None = None,
    raw_response_truncated: int = 0,
    rejected_items: str | None = None,
) -> int:
    cursor = conn.execute(
        "INSERT INTO proposal_attempt_input"
        " (attempt_id, masking_ruleset_version, policy_version, masked_request,"
        "  request_sha256, raw_response, response_sha256, raw_response_truncated,"
        "  rejected_items, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,"
        "  '2026-01-01T10:02:01+00:00')",
        (
            attempt_id,
            ruleset_version,
            policy_version,
            masked_request,
            request_sha256,
            raw_response,
            response_sha256,
            raw_response_truncated,
            rejected_items,
        ),
    )
    return int(cursor.lastrowid or 0)


def test_table_is_created_by_the_migration(conn: sqlite3.Connection) -> None:
    conn.execute("SELECT 1 FROM proposal_attempt_input")
    # Re-application stays idempotent (migration runner contract).
    apply_migrations(conn)
    conn.execute("SELECT 1 FROM proposal_attempt_input")


def test_artifact_row_persists_against_a_real_attempt(
    conn: sqlite3.Connection,
) -> None:
    bucket_id = _group_and_bucket(conn)
    attempt_id = _attempt(conn, bucket_id)
    artifact_id = _artifact(conn, attempt_id)
    row = conn.execute(
        "SELECT attempt_id, masking_ruleset_version, policy_version, masked_request,"
        " request_sha256, raw_response, response_sha256, raw_response_truncated,"
        " rejected_items, captured_at"
        " FROM proposal_attempt_input WHERE id = ?",
        (artifact_id,),
    ).fetchone()
    assert int(row["attempt_id"]) == attempt_id
    assert str(row["masking_ruleset_version"]) == "masking-rules-v1"
    assert str(row["policy_version"]) == "proposal-prompt-v1"
    assert "mention_handle_map" in str(row["masked_request"])
    assert str(row["request_sha256"]) == "0" * 64
    assert row["raw_response"] is None
    assert row["response_sha256"] is None
    assert int(row["raw_response_truncated"]) == 0
    assert row["rejected_items"] is None
    assert str(row["captured_at"]).strip() != ""


def test_raw_response_truncated_checks(conn: sqlite3.Connection) -> None:
    """C2/DC-20: raw_response_truncated is in (0, 1) and requires non-NULL raw_response when 1."""
    bucket_id = _group_and_bucket(conn)
    attempt1 = _attempt(conn, bucket_id)
    raw = '{"proposals":[]}'
    h = hashlib.sha256(raw.encode()).hexdigest()

    # Valid: truncated=1 with non-null raw_response
    _artifact(
        conn,
        attempt1,
        raw_response=raw,
        response_sha256=h,
        raw_response_truncated=1,
    )
    row = conn.execute(
        "SELECT raw_response_truncated FROM proposal_attempt_input WHERE attempt_id = ?",
        (attempt1,),
    ).fetchone()
    assert int(row["raw_response_truncated"]) == 1

    # Invalid: truncated not in (0, 1)
    attempt2 = _attempt(conn, bucket_id)
    with pytest.raises(sqlite3.IntegrityError):
        _artifact(
            conn,
            attempt2,
            raw_response=raw,
            response_sha256=h,
            raw_response_truncated=2,
        )

    # Invalid: truncated=1 when raw_response is NULL
    attempt3 = _attempt(conn, bucket_id)
    with pytest.raises(sqlite3.IntegrityError):
        _artifact(
            conn,
            attempt3,
            raw_response=None,
            response_sha256=None,
            raw_response_truncated=1,
        )


def test_attempt_identity_is_unique(conn: sqlite3.Connection) -> None:
    bucket_id = _group_and_bucket(conn)
    attempt_id = _attempt(conn, bucket_id)
    _artifact(conn, attempt_id)
    with pytest.raises(sqlite3.IntegrityError):
        _artifact(conn, attempt_id)


def test_attempt_must_exist_restRICT(conn: sqlite3.Connection) -> None:
    _group_and_bucket(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _artifact(conn, 999)


def test_attempt_deletion_is_restricted(conn: sqlite3.Connection) -> None:
    """DR-01/DR-02: the artifact chain never breaks by silent deletion."""
    bucket_id = _group_and_bucket(conn)
    attempt_id = _attempt(conn, bucket_id)
    _artifact(conn, attempt_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM processing_attempt WHERE id = ?", (attempt_id,))


@pytest.mark.parametrize(
    "field",
    [
        "masking_ruleset_version",
        "policy_version",
        "masked_request",
        "request_sha256",
        "captured_at",
    ],
)
def test_non_blank_required_fields_enforced(
    conn: sqlite3.Connection, field: str
) -> None:
    bucket_id = _group_and_bucket(conn)
    attempt_id = _attempt(conn, bucket_id)
    values = {
        "masking_ruleset_version": "masking-rules-v1",
        "policy_version": "proposal-prompt-v1",
        "masked_request": '{"bucket_id":1}',
        "request_sha256": "0" * 64,
        "captured_at": "2026-01-01T10:02:01+00:00",
    }
    values[field] = "   "  # blank violates the CHECK
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_attempt_input"
            " (attempt_id, masking_ruleset_version, policy_version,"
            "  masked_request, request_sha256, captured_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                attempt_id,
                values["masking_ruleset_version"],
                values["policy_version"],
                values["masked_request"],
                values["request_sha256"],
                values["captured_at"],
            ),
        )


def test_raw_response_and_hash_may_be_stored_together(
    conn: sqlite3.Connection,
) -> None:
    """C2: a returned response survives masking/capping + its stored hash."""
    bucket_id = _group_and_bucket(conn)
    attempt_id = _attempt(conn, bucket_id)
    raw = '{"proposals":[]}'
    _artifact(
        conn,
        attempt_id,
        raw_response=raw,
        response_sha256=hashlib.sha256(raw.encode()).hexdigest(),
    )
    row = conn.execute(
        "SELECT raw_response, response_sha256 FROM proposal_attempt_input"
        " WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    assert str(row["raw_response"]) == raw
    assert (
        str(row["response_sha256"])
        == hashlib.sha256(raw.encode()).hexdigest()
    )


def test_raw_response_and_hash_must_agree(conn: sqlite3.Connection) -> None:
    """The pair is either both NULL or both set — never one-sided."""
    bucket_id = _group_and_bucket(conn)
    attempt_id = _attempt(conn, bucket_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_attempt_input"
            " (attempt_id, masking_ruleset_version, policy_version,"
            "  masked_request, request_sha256, raw_response, captured_at)"
            " VALUES (?, 'masking-rules-v1', 'proposal-prompt-v1', '{\"b\":1}',"
            "  ?, 'a response', '2026-01-01T10:02:01+00:00')",
            (attempt_id, "0" * 64),
        )
    other_attempt = _attempt(conn, bucket_id)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO proposal_attempt_input"
            " (attempt_id, masking_ruleset_version, policy_version,"
            "  masked_request, request_sha256, response_sha256, captured_at)"
            " VALUES (?, 'masking-rules-v1', 'proposal-prompt-v1', '{\"b\":1}',"
            "  ?, ?, '2026-01-01T10:02:01+00:00')",
            (other_attempt, "0" * 64, "f" * 64),
        )


def test_rejected_items_are_free_json_without_vocabulary_enforcement(
    conn: sqlite3.Connection,
) -> None:
    """C2: Tier-2 rejections ride the artifact as annotated JSON evidence."""
    bucket_id = _group_and_bucket(conn)
    attempt_id = _attempt(conn, bucket_id, outcome="FAILED_RETRYABLE")
    rejected = '[{"item_index":0,"reason_code":"UNKNOWN_HANDLE"}]'
    _artifact(conn, attempt_id, rejected_items=rejected)
    row = conn.execute(
        "SELECT rejected_items FROM proposal_attempt_input WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    assert str(row["rejected_items"]) == rejected
