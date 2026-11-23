"""Masked local exports of the ASEC record set (Phase 12 D3, owner decision D).

The export writes one JSON Lines file per dataset plus a ``manifest.json`` into
a destination directory the operator chooses on their own machine. It covers
reviews, tasks, approvals, processing records and notifications, and it is
masked by default through :func:`backend.masking.mask_pii` (NFR-SEC-04,
EIR-03): every string value passes through the same masking rules an AI call
would see.

Two things are never exported, masked or not, because they are credentials and
not records:

* the auth stores — ``auth_attempt``, ``auth_session``, ``otp_challenge`` and
  ``idempotency_key``;
* push tokens — the ``push_endpoint`` table is excluded as a whole, and
  ``person.roll_number`` (the login credential) is not selected.

The source database is opened read-only (``mode=ro``), so the export never
writes a row. SQLite may still create its shared-memory sidecars (``-wal`` /
``-shm``) next to a WAL database when it opens it; the database file itself is
untouched. The destination must be absent or empty: an existing operator file
is never overwritten. Nothing here calls AI services or prints record content.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from backend.masking import mask_pii

#: Manifest format tag; bump it when the on-disk shape changes.
MANIFEST_FORMAT = "asec-export/1"
MANIFEST_NAME = "manifest.json"


class ExportError(Exception):
    """The export cannot run against this source or destination."""


@dataclass(frozen=True)
class Dataset:
    """One exported table: its file stem and its read-only query."""

    name: str
    sql: str


@dataclass(frozen=True)
class ExportResult:
    """What an export wrote: counts only, never record content."""

    destination: Path
    masked: bool
    exported_at: str
    datasets: tuple[tuple[str, int], ...]


#: Every domain table an operator needs, in a stable order. Credential stores
#: (``auth_attempt``, ``auth_session``, ``otp_challenge``, ``idempotency_key``,
#: ``push_endpoint``) are deliberately absent; ``person`` lists its columns so
#: ``roll_number`` is never selected.
_DATASETS: tuple[Dataset, ...] = (
    Dataset("verticals", "SELECT id, name FROM vertical ORDER BY id"),
    Dataset(
        "persons",
        "SELECT id, name, discord_user_id, phone, role, reports_to, active"
        " FROM person ORDER BY id",
    ),
    Dataset(
        "person_verticals",
        "SELECT person_id, vertical_id FROM person_vertical"
        " ORDER BY person_id, vertical_id",
    ),
    Dataset(
        "source_groups",
        "SELECT id, discord_channel_id, vertical_id FROM source_group ORDER BY id",
    ),
    Dataset(
        "source_group_clocks",
        "SELECT group_id, clock_at, mode, updated_at FROM source_group_clock"
        " ORDER BY group_id",
    ),
    Dataset(
        "source_group_progress",
        "SELECT group_id, progress_to, updated_at FROM source_group_progress"
        " ORDER BY group_id",
    ),
    Dataset(
        "source_messages",
        "SELECT id, source, source_message_id, group_id, sender_person_id,"
        " sender_discord_user_id, event_timestamp, received_at, content,"
        " reply_reference, raw_metadata, flag_late, flag_unmapped,"
        " flag_unknown_sender, flag_unclassified, time_basis"
        " FROM source_message ORDER BY id",
    ),
    Dataset(
        "message_mentions",
        "SELECT id, message_id, mentioned_discord_user_id, position"
        " FROM message_mention ORDER BY id",
    ),
    Dataset(
        "message_reactions",
        "SELECT id, message_id, reactor_discord_user_id, emoji, direction,"
        " reacted_at FROM message_reaction ORDER BY id",
    ),
    Dataset(
        "buckets",
        "SELECT id, group_id, predecessor_bucket_id, start_event_timestamp,"
        " end_event_timestamp, parameters_used, processing_state"
        " FROM bucket ORDER BY id",
    ),
    Dataset(
        "bucket_messages",
        "SELECT bucket_id, message_id, group_id FROM bucket_message"
        " ORDER BY bucket_id, message_id",
    ),
    Dataset(
        "processing_attempts",
        "SELECT id, stage, bucket_id, model_id, model_version, config_version,"
        " started_at, finished_at, outcome, error FROM processing_attempt"
        " ORDER BY id",
    ),
    Dataset(
        "relevance_attempt_inputs",
        "SELECT id, attempt_id, masking_ruleset_version, policy_version,"
        " masked_request, request_sha256, captured_at"
        " FROM relevance_attempt_input ORDER BY id",
    ),
    Dataset(
        "relevance_decisions",
        "SELECT id, bucket_id, message_id, attempt_id, decision, model_id,"
        " model_version, prompt_version, decided_at"
        " FROM relevance_decision ORDER BY id",
    ),
    Dataset(
        "relevance_corrections",
        "SELECT id, decision_id, person_id, corrected_decision, corrected_at"
        " FROM relevance_correction ORDER BY id",
    ),
    Dataset(
        "proposal_attempt_inputs",
        "SELECT id, attempt_id, masking_ruleset_version, policy_version,"
        " masked_request, request_sha256, raw_response, response_sha256,"
        " raw_response_truncated, rejected_items, captured_at"
        " FROM proposal_attempt_input ORDER BY id",
    ),
    Dataset(
        "proposals",
        "SELECT id, attempt_id, bucket_id, primary_message_id, ptype,"
        " description, assignment_mode, proposed_assignees_text, people_needed,"
        " people_needed_unlimited, task_type, source_vertical_id,"
        " suggested_responsible_vertical_id, structurally_valid, generated_at,"
        " model_id, model_version, prompt_version, review_state"
        " FROM proposal ORDER BY id",
    ),
    Dataset(
        "proposal_assignees",
        "SELECT id, proposal_id, assignee_text, resolution, person_id,"
        " resolved_via FROM proposal_assignee ORDER BY id",
    ),
    Dataset(
        "proposal_attributes",
        "SELECT proposal_id, attr_key, attr_value FROM proposal_attribute"
        " ORDER BY proposal_id, attr_key",
    ),
    Dataset(
        "proposal_affected_verticals",
        "SELECT proposal_id, vertical_id FROM proposal_affected_vertical"
        " ORDER BY proposal_id, vertical_id",
    ),
    Dataset(
        "proposal_context_messages",
        "SELECT proposal_id, bucket_id, message_id FROM proposal_context_message"
        " ORDER BY proposal_id, message_id",
    ),
    Dataset(
        "proposal_revisions",
        "SELECT id, proposal_id, revision_number, person_id, changed_fields,"
        " changed_at, description, assignment_mode, proposed_assignees_text,"
        " people_needed, people_needed_unlimited, task_type,"
        " suggested_responsible_vertical_id FROM proposal_revision ORDER BY id",
    ),
    Dataset(
        "revision_assignees",
        "SELECT id, revision_id, assignee_text, resolution, person_id,"
        " resolved_via FROM revision_assignee ORDER BY id",
    ),
    Dataset(
        "revision_attributes",
        "SELECT revision_id, attr_key, attr_value FROM revision_attribute"
        " ORDER BY revision_id, attr_key",
    ),
    Dataset(
        "revision_affected_verticals",
        "SELECT revision_id, vertical_id FROM revision_affected_vertical"
        " ORDER BY revision_id, vertical_id",
    ),
    Dataset(
        "proposal_decisions",
        "SELECT id, proposal_id, decision, decided_by, decided_at, note"
        " FROM proposal_decision ORDER BY id",
    ),
    Dataset(
        "tasks",
        "SELECT id, proposal_id, task_type, source_vertical_id,"
        " responsible_vertical_id, creator_id, description, assignment_mode,"
        " people_needed, people_needed_unlimited, approval_state, cancellation,"
        " version, created_at FROM task ORDER BY id",
    ),
    Dataset(
        "task_assignees",
        "SELECT id, task_id, person_id, agreement, agreed_at, work_status,"
        " cancelled, added_at FROM task_assignee ORDER BY id",
    ),
    Dataset(
        "task_attributes",
        "SELECT task_id, attr_key, attr_value FROM task_attribute"
        " ORDER BY task_id, attr_key",
    ),
    Dataset(
        "task_affected_verticals",
        "SELECT task_id, vertical_id FROM task_affected_vertical"
        " ORDER BY task_id, vertical_id",
    ),
    Dataset(
        "task_history",
        "SELECT id, task_id, field_name, old_value, new_value, changed_at,"
        " actor_person_id, actor_model_id, actor_model_version, actor_role,"
        " actor_vertical_id, reason FROM task_history ORDER BY id",
    ),
    Dataset(
        "approval_requests",
        "SELECT id, task_id, requested_by, request_message, created_at"
        " FROM approval_request ORDER BY id",
    ),
    Dataset(
        "approval_recipients",
        "SELECT request_id, person_id FROM approval_recipient"
        " ORDER BY request_id, person_id",
    ),
    Dataset(
        "approval_decisions",
        "SELECT id, request_id, decided_by, decision, decision_message,"
        " decided_at FROM approval_decision ORDER BY id",
    ),
    Dataset(
        "announcements",
        "SELECT id, proposal_id, accepted_by, accepted_at, audience_type,"
        " audience_vertical_id, audience_ah_person_id FROM announcement"
        " ORDER BY id",
    ),
    Dataset(
        "notifications",
        "SELECT id, event_type, recipient_person_id, dedupe_key, proposal_id,"
        " task_id, approval_request_id, announcement_id, delivery_state,"
        " created_at FROM notification ORDER BY id",
    ),
    Dataset(
        "notification_attempts",
        "SELECT id, notification_id, attempt_number, outcome, error,"
        " recorded_at FROM notification_attempt ORDER BY id",
    ),
    Dataset(
        "notification_reads",
        "SELECT notification_id, read_at FROM notification_read"
        " ORDER BY notification_id",
    ),
    Dataset(
        "authorization_denials",
        "SELECT id, actor_person_id, operation, subject_type, subject_id,"
        " reason, attempted_at FROM authorization_denial ORDER BY id",
    ),
    Dataset(
        "latency_measurements",
        "SELECT id, metric, value_seconds, recorded_at, source_message_id,"
        " bucket_id, processing_attempt_id, proposal_id"
        " FROM latency_measurement ORDER BY id",
    ),
    Dataset(
        "replay_batches",
        "SELECT id, source, file_sha256, time_zone, started_at, finished_at,"
        " outcome, counts_json FROM replay_batch ORDER BY id",
    ),
    Dataset(
        "evaluation_runs",
        "SELECT id, function, model_id, model_version, prompt_version,"
        " dataset_sha256, case_count, started_at, finished_at, metrics_json"
        " FROM evaluation_run ORDER BY id",
    ),
    Dataset(
        "evaluation_case_results",
        "SELECT id, run_id, case_id, expected_json, predicted_json, correct"
        " FROM evaluation_case_result ORDER BY id",
    ),
    Dataset(
        "system_controls",
        "SELECT name, paused, updated_at, updated_by FROM system_control"
        " ORDER BY name",
    ),
)


def _open_read_only(database: Path) -> sqlite3.Connection:
    """Open ``database`` read-only; no pragma here can write a row."""
    path = Path(database)
    if not path.is_file():
        raise ExportError(f"database does not exist: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise ExportError(f"cannot open the database read-only: {error}") from error
    connection.row_factory = sqlite3.Row
    return connection


def _prepare_destination(destination: Path) -> Path:
    """Create an absent destination; refuse a non-empty one."""
    path = Path(destination)
    if path.exists():
        if not path.is_dir():
            raise ExportError(f"destination is not a directory: {path}")
        if any(path.iterdir()):
            raise ExportError(f"destination is not empty: {path}")
        return path
    path.mkdir(parents=True)
    return path


def _record(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


def _export_dataset(
    connection: sqlite3.Connection,
    *,
    dataset: Dataset,
    destination: Path,
    masked: bool,
) -> int:
    try:
        rows = connection.execute(dataset.sql).fetchall()
    except sqlite3.Error as error:
        raise ExportError(f"cannot read {dataset.name}: {error}") from error
    lines: list[str] = []
    for row in rows:
        record = _record(row)
        if masked:
            record = {
                key: mask_pii(value) if isinstance(value, str) else value
                for key, value in record.items()
            }
        lines.append(
            json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        )
    target = destination / f"{dataset.name}.jsonl"
    target.write_text(
        "".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n"
    )
    return len(rows)


def export_database(
    database: str | Path,
    *,
    destination: str | Path,
    masked: bool,
    exported_at: str,
) -> ExportResult:
    """Write every dataset and the manifest; return per-dataset counts.

    ``masked`` is the operator's choice and is recorded in the manifest so a
    file set is self-describing. ``exported_at`` is injected by the caller (the
    CLI passes the wall clock); this function never reads a clock.
    """
    if not isinstance(exported_at, str) or exported_at.strip() == "":
        raise ValueError("exported_at must be a non-empty string")
    connection = _open_read_only(Path(database))
    try:
        target = _prepare_destination(Path(destination))
        counts = tuple(
            (
                dataset.name,
                _export_dataset(
                    connection,
                    dataset=dataset,
                    destination=target,
                    masked=masked,
                ),
            )
            for dataset in _DATASETS
        )
    finally:
        connection.close()
    manifest = {
        "format": MANIFEST_FORMAT,
        "masked": masked,
        "exported_at": exported_at,
        "datasets": dict(counts),
    }
    (target / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return ExportResult(
        destination=target,
        masked=masked,
        exported_at=exported_at,
        datasets=counts,
    )
