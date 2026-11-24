"""Masked local exports and the ``export`` CLI (D3, owner decision D).

The export covers the domain record set, masks text by default with
:func:`backend.masking.mask_pii`, never includes login credentials or push
tokens, and writes local files only. The database is opened read-only.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from backend.__main__ import main
from backend.db.connection import connect
from backend.export import ExportError, export_database
from backend.masking import EMAIL_PLACEHOLDER, PHONE_PLACEHOLDER, ROLL_NO_PLACEHOLDER
from tests.api_fixtures import (
    NOW,
    assign,
    make_approval,
    make_notification,
    make_proposal,
    make_task,
    seed_database,
    seed_world,
)

PHONE = "+919876543210"
EMAIL = "synthetic.person@example.com"
ROLL = "B25CS1044"
#: The repository root, derived the same way the export guard derives it.
REPO_ROOT = Path(__file__).resolve().parents[1]
#: A 64-character synthetic request hash (0025 requires exactly 64 chars).
SECRET_HASH = "SECRET-HASH-" + "0" * 52


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "source.db"
    seed_database(database)
    seed_world(database)
    task = make_task(database, creator_id=3, description=f"Call {PHONE} or {EMAIL}")
    assign(database, task, 4, agreement="ACCEPTED", work_status="IN_PROGRESS")
    proposal = make_proposal(
        database, sender_person_id=3, description=f"Roll {ROLL} needs a room"
    )
    make_approval(
        database,
        make_task(database, creator_id=3, approval_state="PENDING"),
        requested_by=3,
        recipients=(3,),
        message=f"Mail {EMAIL}",
    )
    make_notification(database, task_id=task, dedupe_key="export-1")
    connection = connect(database)
    try:
        connection.execute(
            "UPDATE person SET phone = ?, name = ? WHERE id = 4",
            (PHONE, f"Volunteer {EMAIL}"),
        )
        connection.execute(
            "INSERT INTO auth_session (person_id, token_hash, issued_at,"
            " expires_at) VALUES (1, 'SECRET-TOKEN-HASH', ?, ?)",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO otp_challenge (person_id, code_salt, code_hash,"
            " created_at, expires_at) VALUES (1, 'SECRET-SALT', 'SECRET-CODE-HASH',"
            " ?, ?)",
            (NOW, NOW),
        )
        connection.execute(
            "INSERT INTO idempotency_key (person_id, route_key, key, request_hash,"
            " response_status, response_body, created_at, expires_at)"
            " VALUES (1, 'TASK_CLAIM', 'SECRET-IDEMPOTENCY-KEY', ?,"
            " 200, 'SECRET-RESPONSE-BODY', ?, ?)",
            (SECRET_HASH, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO push_endpoint (person_id, provider, token, active,"
            " created_at) VALUES (4, 'FCM', 'SECRET-PUSH-TOKEN', 1, ?)",
            (NOW,),
        )
        connection.execute(
            "UPDATE proposal SET description = ? WHERE id = ?",
            (f"Roll {ROLL} needs a room", proposal),
        )
    finally:
        connection.close()
    return database


def _all_text(destination: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(destination.iterdir())
        if path.is_file()
    )


def test_masked_export_writes_jsonl_files_and_a_manifest(tmp_path: Path) -> None:
    database = _database(tmp_path)
    destination = tmp_path / "export-masked"

    result = export_database(
        database, destination=destination, masked=True, exported_at=NOW
    )

    assert result.masked is True
    assert result.exported_at == NOW
    names = {name for name, _count in result.datasets}
    assert {"persons", "source_messages", "proposals", "tasks", "notifications"} <= names
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["masked"] is True
    assert manifest["exported_at"] == NOW
    assert dict(result.datasets) == manifest["datasets"]
    for name, count in result.datasets:
        lines = (destination / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == count
        for line in lines:
            assert isinstance(json.loads(line), dict)


def test_masked_export_contains_no_personal_text_or_credentials(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    destination = tmp_path / "export-masked"

    export_database(database, destination=destination, masked=True, exported_at=NOW)
    text = _all_text(destination)

    for raw in (PHONE, EMAIL, ROLL, "SECRET-TOKEN-HASH", "SECRET-CODE-HASH",
                "SECRET-SALT", "SECRET-IDEMPOTENCY-KEY", "SECRET-RESPONSE-BODY",
                SECRET_HASH, "SECRET-PUSH-TOKEN"):
        assert raw not in text
    assert PHONE_PLACEHOLDER in text
    assert EMAIL_PLACEHOLDER in text
    assert ROLL_NO_PLACEHOLDER in text


def test_an_unmasked_export_keeps_text_but_still_excludes_credentials(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    destination = tmp_path / "export-raw"

    export_database(database, destination=destination, masked=False, exported_at=NOW)
    text = _all_text(destination)

    assert PHONE in text
    assert EMAIL in text
    assert "SECRET-TOKEN-HASH" not in text
    assert "SECRET-CODE-HASH" not in text
    assert "SECRET-IDEMPOTENCY-KEY" not in text
    assert "SECRET-PUSH-TOKEN" not in text
    # The roll-number *column* is the login credential; it is never selected.
    assert '"roll_number"' not in text
    assert "R-ADMIN" not in text


def test_export_refuses_to_overwrite_a_non_empty_destination(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    destination = tmp_path / "occupied"
    destination.mkdir()
    (destination / "keep.txt").write_text("operator file", encoding="utf-8")

    with pytest.raises(ExportError):
        export_database(database, destination=destination, masked=True, exported_at=NOW)
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "operator file"


def test_export_refuses_a_destination_inside_the_repository(
    tmp_path: Path,
) -> None:
    """A destination under the repo would mix operator data into source control."""
    database = _database(tmp_path)
    destination = REPO_ROOT / "export-must-not-be-created"
    assert not destination.exists()
    try:
        with pytest.raises(ExportError, match="repository"):
            export_database(
                database, destination=destination, masked=True, exported_at=NOW
            )
        assert not destination.exists()
    finally:
        shutil.rmtree(destination, ignore_errors=True)


def test_export_refuses_an_existing_manifest_before_any_other_check(
    tmp_path: Path,
) -> None:
    """An existing export is never overwritten; the refusal names the manifest."""
    database = _database(tmp_path)
    destination = tmp_path / "existing-export"
    destination.mkdir()
    manifest = destination / "manifest.json"
    manifest.write_text('{"keep": true}', encoding="utf-8")

    with pytest.raises(ExportError, match="manifest"):
        export_database(
            database, destination=destination, masked=True, exported_at=NOW
        )

    assert manifest.read_text(encoding="utf-8") == '{"keep": true}'
    assert sorted(path.name for path in destination.iterdir()) == ["manifest.json"]


def test_export_does_not_write_to_the_source_database(tmp_path: Path) -> None:
    database = _database(tmp_path)
    before = database.stat().st_mtime_ns
    connection = connect(database)
    try:
        counts_before = {
            table: connection.execute(
                f"SELECT COUNT(*) AS n FROM {table}"
            ).fetchone()["n"]
            for table in ("person", "system_control", "authorization_denial")
        }
    finally:
        connection.close()

    export_database(
        database, destination=tmp_path / "read-only", masked=True, exported_at=NOW
    )

    connection = connect(database)
    try:
        counts_after = {
            table: connection.execute(
                f"SELECT COUNT(*) AS n FROM {table}"
            ).fetchone()["n"]
            for table in ("person", "system_control", "authorization_denial")
        }
    finally:
        connection.close()
    assert counts_after == counts_before
    assert database.stat().st_mtime_ns == before


def test_the_export_cli_writes_files_and_prints_counts_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    destination = tmp_path / "cli-export"

    code = main(
        [
            "export",
            "--db",
            str(database),
            "--destination",
            str(destination),
        ]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert (destination / "manifest.json").exists()
    assert PHONE not in out
    assert EMAIL not in out
    assert "SECRET-TOKEN-HASH" not in out
    assert "SECRET-PUSH-TOKEN" not in out
    assert "masked=True" in out


def test_the_export_cli_refuses_unmasked_without_the_acknowledgement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    destination = tmp_path / "cli-unmasked-refused"

    code = main(
        [
            "export",
            "--db",
            str(database),
            "--destination",
            str(destination),
            "--unmasked",
        ]
    )

    assert code == 2
    assert not destination.exists()
    err = capsys.readouterr().err
    assert "--i-understand-this-contains-real-data" in err


def test_the_export_cli_accepts_unmasked_with_one_acknowledgement_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _database(tmp_path)
    destination = tmp_path / "cli-unmasked"

    code = main(
        [
            "export",
            "--db",
            str(database),
            "--destination",
            str(destination),
            "--unmasked",
            "--i-understand-this-contains-real-data",
        ]
    )

    assert code == 0
    assert json.loads(
        (destination / "manifest.json").read_text(encoding="utf-8")
    )["masked"] is False
    assert PHONE in _all_text(destination)
    captured = capsys.readouterr()
    warnings = [line for line in captured.err.splitlines() if line.strip()]
    assert len(warnings) == 1
    assert "unmasked" in warnings[0].lower()
    assert PHONE not in captured.err
    assert EMAIL not in captured.err
    assert "SECRET-TOKEN-HASH" not in captured.out + captured.err


@pytest.mark.parametrize(
    "args",
    [
        ["export"],
        ["export", "--db", "x.db"],
        ["export", "--destination", "out"],
        ["export", "--db", "x.db", "--destination", "out", "--unknown"],
    ],
)
def test_the_export_cli_rejects_incomplete_or_unknown_arguments(
    args: list[str],
) -> None:
    assert main(args) == 2
