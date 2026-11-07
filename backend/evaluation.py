"""Evaluation harness for the AI functions (Phase 10, W4).

This module implements the offline evaluation core from plan section 4.4:

* :func:`load_labelled_set` loads owner-labelled JSONL cases and validates them
  strictly; it never fills a missing label.
* :func:`convert_owner_csv` converts the owner's CSV export into the same
  in-memory cases, recording excluded rows instead of silently dropping them.
* :func:`score_relevance`, :func:`score_proposals` and
  :func:`score_end_to_end` compare injected predictions with the labels.
* :func:`format_report` prints counts and metrics only. It never prints message
  text, message ids or case ids.

Privacy is binding: the real labelled set is real chat. Its path must be
outside the repository (argument or ``ASEC_EVAL_SET_PATH``); the file is never
copied into the repository, tests or logs. Tests use small synthetic cases and
an injected fake adapter, so this module makes no network call.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from backend.config import Config, load_config
from backend.db.connection import transaction
from backend.proposals import (
    PROPOSAL_PROMPT,
    ProposalDraft,
    ProposalGenerationRequest,
    ProposalMessage,
    ProposalResponse,
)
from backend.relevance import (
    CLASSIFICATION_POLICY,
    DECISION_VOCABULARY,
    ContextMessage,
    RelevanceRequest,
    RelevanceResponse,
)
from backend.review import (
    EvalProposal,
    ReviewPolicy,
    list_reviewed_proposals_for_eval,
)

DATASET_PATH_ENV = "ASEC_EVAL_SET_PATH"

RELEVANCE_FUNCTION = "RELEVANCE"
PROPOSAL_FUNCTION = "PROPOSAL"
END_TO_END_FUNCTION = "END_TO_END"
KNOWN_FUNCTIONS = (RELEVANCE_FUNCTION, PROPOSAL_FUNCTION, END_TO_END_FUNCTION)

PTYPE_VOCABULARY = ("TASK", "ANNOUNCEMENT")
ASSIGNMENT_MODE_VOCABULARY = ("EXPLICIT", "OPEN", "NONE")
TASK_TYPE_VOCABULARY = ("INTERNAL", "CROSS_VERTICAL")

WORK_NOT_STATED_PLACEHOLDER = "[PLACEHOLDER: work not stated]"
_PLACEHOLDER_MARKER = "[PLACEHOLDER:"

CSV_COLUMNS = (
    "case_id",
    "conversation_id",
    "timestamp",
    "sender",
    "sender_role",
    "message_kind",
    "masked_text",
    "relevance",
    "hard_negative_category",
    "proposal_expected",
    "proposal_type",
    "assignment_mode",
    "assignees_text",
    "people_needed",
    "task_type_guess",
    "expected_description",
    "attributes",
    "label_confidence",
    "needs_owner_review",
)

_PROPOSAL_FIELDS = (
    "ptype",
    "assignment_mode",
    "task_type",
    "people_needed",
    "assignees",
    "description",
)

_READ_DECISION = "ACCEPTED"
_REJECT_DECISION = "REJECTED"


class EvaluationError(Exception):
    """Base class for evaluation failures."""


class DatasetError(EvaluationError):
    """The labelled dataset is missing, malformed or not allowed."""


class DatasetPathError(DatasetError):
    """The labelled dataset path is inside the repository or not a file."""


@dataclass(frozen=True)
class LabelledCase:
    """One labelled evaluation case (plan section 4.4).

    ``payload`` has the model-facing request fields; ``expected`` has labels.
    Optional case-level metadata supports reporting without dropping rows.
    """

    case_id: str
    function: str
    sender_role: str | None
    payload: Mapping[str, object]
    expected: Mapping[str, object]
    label_confidence: str | None = None
    needs_owner_review: bool = False
    hard_negative_category: str | None = None


@dataclass(frozen=True)
class CsvConversion:
    """Cases converted from the owner CSV plus the excluded-row count."""

    cases: tuple[LabelledCase, ...]
    excluded_count: int


@dataclass(frozen=True)
class _MessageLabel:
    message_id: int
    decision: str
    hard_negative_category: str | None
    label_confidence: str | None
    needs_owner_review: bool


@dataclass(frozen=True)
class _ProposalLabel:
    primary_message_id: int
    ptype: str
    assignment_mode: str
    task_type: str
    people_needed: int | None
    people_needed_unlimited: int
    assignees: tuple[str, ...]
    description: str | None


@dataclass(frozen=True)
class _EndToEndLabel:
    actionable_message_ids: tuple[int, ...]
    reviewer_days: int | None
    review_decisions: Mapping[int, str]


# ---------------------------------------------------------------------------
# Dataset path validation and environment setting
# ---------------------------------------------------------------------------


def _repository_root() -> Path:
    return Path(__file__).resolve().parent.parent


def validate_dataset_path(
    path: str | Path, *, repo_root: str | Path | None = None
) -> Path:
    """Resolve ``path`` and refuse anything inside the repository root."""
    resolved = Path(path).expanduser().resolve()
    root = (
        Path(repo_root).expanduser().resolve()
        if repo_root is not None
        else _repository_root()
    )
    try:
        resolved.relative_to(root)
    except ValueError:
        pass
    else:
        raise DatasetPathError(
            "the evaluation dataset path must be outside the repository"
        )
    if not resolved.is_file():
        raise DatasetPathError(
            "the evaluation dataset path does not exist or is not a file"
        )
    return resolved


def eval_set_path_from_environment(
    config: Config | None = None, *, repo_root: str | Path | None = None
) -> Path:
    """Read and validate ``ASEC_EVAL_SET_PATH``."""
    active = load_config() if config is None else config
    value = active.get(DATASET_PATH_ENV)
    if value is None or value.strip() == "":
        raise DatasetError(f"{DATASET_PATH_ENV} is not set")
    return validate_dataset_path(value, repo_root=repo_root)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DatasetError(f"{where}: expected an object")
    return value


def _sequence(value: object, where: str) -> list[object]:
    if not isinstance(value, (list, tuple)):
        raise DatasetError(f"{where}: expected a list")
    return list(value)


def _integer(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DatasetError(f"{where}: expected an integer")
    return value


def _text(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise DatasetError(f"{where}: expected a string")
    return value


def _optional_text(value: object, where: str) -> str | None:
    if value is None:
        return None
    text = _text(value, where).strip()
    return text or None


def _optional_integer(value: object, where: str) -> int | None:
    if value is None or value == "":
        return None
    return _integer(value, where)


def _boolish(value: object, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().upper()
        if text in ("Y", "YES", "TRUE", "1"):
            return True
        if text in ("", "N", "NO", "FALSE", "0"):
            return False
    raise DatasetError(f"{where}: expected a yes/no value")


def _string_tuple(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    items = _sequence(value, where)
    result: list[str] = []
    for index, item in enumerate(items):
        text = _text(item, f"{where}[{index}]").strip()
        if text == "":
            raise DatasetError(f"{where}[{index}]: expected a non-blank token")
        result.append(text)
    return tuple(result)


def _payload_message_ids(
    payload: Mapping[str, object], key: str, where: str
) -> set[int]:
    entries = _sequence(payload.get(key), f"{where}.payload.{key}")
    ids: list[int] = []
    for index, entry in enumerate(entries):
        message = _mapping(entry, f"{where}.payload.{key}[{index}]")
        ids.append(
            _integer(
                message.get("message_id"),
                f"{where}.payload.{key}[{index}].message_id",
            )
        )
    if len(set(ids)) != len(ids):
        raise DatasetError(f"{where}.payload.{key}: duplicate message ids")
    return set(ids)


def _relevance_labels(
    expected: Mapping[str, object], *, where: str
) -> tuple[_MessageLabel, ...]:
    entries = _sequence(expected.get("messages"), f"{where}.expected.messages")
    labels: list[_MessageLabel] = []
    seen: set[int] = set()
    for index, entry in enumerate(entries):
        location = f"{where}.expected.messages[{index}]"
        message = _mapping(entry, location)
        message_id = _integer(message.get("message_id"), f"{location}.message_id")
        if message_id in seen:
            raise DatasetError(f"{location}: duplicate message id")
        seen.add(message_id)
        if "decision" not in message:
            raise DatasetError(f"{location}: missing decision")
        decision = _text(message["decision"], f"{location}.decision").strip().upper()
        if decision not in DECISION_VOCABULARY:
            raise DatasetError(f"{location}: unknown decision {decision!r}")
        labels.append(
            _MessageLabel(
                message_id=message_id,
                decision=decision,
                hard_negative_category=_optional_text(
                    message.get("hard_negative_category"),
                    f"{location}.hard_negative_category",
                ),
                label_confidence=_optional_text(
                    message.get("label_confidence"),
                    f"{location}.label_confidence",
                ),
                needs_owner_review=_boolish(
                    message.get("needs_owner_review", False),
                    f"{location}.needs_owner_review",
                ),
            )
        )
    return tuple(labels)


def _proposal_label(entry: object, *, where: str) -> _ProposalLabel:
    item = _mapping(entry, where)
    required = (
        "primary_message_id",
        "ptype",
        "assignment_mode",
        "task_type",
    )
    for key in required:
        if key not in item:
            raise DatasetError(f"{where}: missing {key}")
    primary_message_id = _integer(
        item["primary_message_id"], f"{where}.primary_message_id"
    )
    ptype = _text(item["ptype"], f"{where}.ptype").strip().upper()
    if ptype not in PTYPE_VOCABULARY:
        raise DatasetError(f"{where}: unknown ptype {ptype!r}")
    assignment_mode = (
        _text(item["assignment_mode"], f"{where}.assignment_mode").strip().upper()
    )
    if assignment_mode not in ASSIGNMENT_MODE_VOCABULARY:
        raise DatasetError(f"{where}: unknown assignment mode {assignment_mode!r}")
    task_type = _text(item["task_type"], f"{where}.task_type").strip().upper()
    if task_type not in TASK_TYPE_VOCABULARY:
        raise DatasetError(f"{where}: unknown task type {task_type!r}")
    if ptype == "TASK" and assignment_mode == "NONE":
        raise DatasetError(f"{where}: a TASK cannot use assignment mode NONE")
    if ptype == "ANNOUNCEMENT" and assignment_mode != "NONE":
        raise DatasetError(f"{where}: an ANNOUNCEMENT must use assignment mode NONE")
    people_needed = _optional_integer(
        item.get("people_needed"), f"{where}.people_needed"
    )
    people_needed_unlimited = _integer(
        item.get("people_needed_unlimited", 0),
        f"{where}.people_needed_unlimited",
    )
    if people_needed_unlimited not in (0, 1):
        raise DatasetError(f"{where}: people_needed_unlimited must be 0 or 1")
    if people_needed is not None and people_needed <= 0:
        raise DatasetError(f"{where}: people_needed must be positive")
    if people_needed is not None and people_needed_unlimited == 1:
        raise DatasetError(f"{where}: people_needed conflicts with unlimited")
    assignees = _string_tuple(item.get("assignees", []), f"{where}.assignees")
    if assignment_mode in ("OPEN", "NONE") and assignees:
        raise DatasetError(f"{where}: {assignment_mode} cannot name assignees")
    if assignment_mode == "EXPLICIT" and not assignees:
        raise DatasetError(f"{where}: EXPLICIT needs at least one assignee")
    description = _optional_text(item.get("description"), f"{where}.description")
    if ptype == "TASK" and description is None:
        raise DatasetError(
            f"{where}: a TASK needs a stated description or the"
            " not-stated placeholder"
        )
    return _ProposalLabel(
        primary_message_id=primary_message_id,
        ptype=ptype,
        assignment_mode=assignment_mode,
        task_type=task_type,
        people_needed=people_needed,
        people_needed_unlimited=people_needed_unlimited,
        assignees=assignees,
        description=description,
    )


def _proposal_labels(
    expected: Mapping[str, object], *, where: str
) -> tuple[_ProposalLabel, ...]:
    entries = _sequence(expected.get("proposals"), f"{where}.expected.proposals")
    labels: list[_ProposalLabel] = []
    for index, entry in enumerate(entries):
        labels.append(_proposal_label(entry, where=f"{where}.expected.proposals[{index}]"))
    return tuple(labels)


def _end_to_end_label(
    expected: Mapping[str, object], *, where: str
) -> _EndToEndLabel:
    raw_ids = _sequence(
        expected.get("actionable_message_ids"),
        f"{where}.expected.actionable_message_ids",
    )
    ids = tuple(
        _integer(item, f"{where}.expected.actionable_message_ids[{index}]")
        for index, item in enumerate(raw_ids)
    )
    if len(set(ids)) != len(ids):
        raise DatasetError(f"{where}: duplicate actionable message ids")
    reviewer_days = _optional_integer(
        expected.get("reviewer_days"), f"{where}.expected.reviewer_days"
    )
    if reviewer_days is not None and reviewer_days <= 0:
        raise DatasetError(f"{where}: reviewer_days must be positive")
    raw_decisions = expected.get("review_decisions", {})
    decisions = _mapping(raw_decisions, f"{where}.expected.review_decisions")
    parsed: dict[int, str] = {}
    for raw_key, raw_value in decisions.items():
        try:
            message_id = int(str(raw_key))
        except ValueError as error:
            raise DatasetError(
                f"{where}.expected.review_decisions: keys must be message ids"
            ) from error
        decision = _text(
            raw_value, f"{where}.expected.review_decisions[{raw_key}]"
        ).strip().upper()
        if decision not in (_READ_DECISION, _REJECT_DECISION):
            raise DatasetError(
                f"{where}.expected.review_decisions[{raw_key}]: unknown decision"
            )
        parsed[message_id] = decision
    return _EndToEndLabel(
        actionable_message_ids=ids,
        reviewer_days=reviewer_days,
        review_decisions=parsed,
    )


def _validate_expected(
    function: str,
    payload: Mapping[str, object],
    expected: Mapping[str, object],
    *,
    where: str,
) -> None:
    if function == RELEVANCE_FUNCTION:
        relevance_labels = _relevance_labels(expected, where=where)
        payload_ids = _payload_message_ids(payload, "messages", where)
        expected_ids = {label.message_id for label in relevance_labels}
        if payload_ids != expected_ids:
            raise DatasetError(
                f"{where}: every payload message needs exactly one expected decision"
            )
    elif function == PROPOSAL_FUNCTION:
        proposal_labels = _proposal_labels(expected, where=where)
        payload_ids = _payload_message_ids(payload, "candidate_messages", where)
        for proposal_label in proposal_labels:
            if proposal_label.primary_message_id not in payload_ids:
                raise DatasetError(
                    f"{where}: expected proposal is not a candidate message"
                )
    else:
        end_to_end_label = _end_to_end_label(expected, where=where)
        payload_ids = _payload_message_ids(payload, "candidate_messages", where)
        for message_id in end_to_end_label.actionable_message_ids:
            if message_id not in payload_ids:
                raise DatasetError(
                    f"{where}: actionable item is not a candidate message"
                )


def _case_from_json(value: object, *, where: str) -> LabelledCase:
    entry = _mapping(value, where)
    for key in ("case_id", "function", "sender_role", "payload", "expected"):
        if key not in entry:
            raise DatasetError(f"{where}: missing {key}")
    case_id = _text(entry["case_id"], f"{where}.case_id").strip()
    if case_id == "":
        raise DatasetError(f"{where}.case_id: must not be blank")
    function = _text(entry["function"], f"{where}.function").strip().upper()
    if function not in KNOWN_FUNCTIONS:
        raise DatasetError(f"{where}: unknown function {function!r}")
    sender_role = _optional_text(entry["sender_role"], f"{where}.sender_role")
    payload = _mapping(entry["payload"], f"{where}.payload")
    expected = _mapping(entry["expected"], f"{where}.expected")
    if not expected:
        raise DatasetError(f"{where}.expected: labels must not be empty")
    _validate_expected(function, payload, expected, where=where)
    return LabelledCase(
        case_id=case_id,
        function=function,
        sender_role=sender_role,
        payload=payload,
        expected=expected,
        label_confidence=_optional_text(
            entry.get("label_confidence"), f"{where}.label_confidence"
        ),
        needs_owner_review=_boolish(
            entry.get("needs_owner_review", False), f"{where}.needs_owner_review"
        ),
        hard_negative_category=_optional_text(
            entry.get("hard_negative_category"), f"{where}.hard_negative_category"
        ),
    )


def load_labelled_set(
    path: str | Path, *, repo_root: str | Path | None = None
) -> tuple[LabelledCase, ...]:
    """Load and strictly validate a JSONL labelled set.

    Required keys are ``case_id``, ``function``, ``sender_role``, ``payload``
    and ``expected``. Ids must be unique and functions known. A missing label is
    an error, never a default. A blank ``sender_role`` is allowed; callers use
    :func:`production_reasons` to mark the run non-production.
    """
    resolved = validate_dataset_path(path, repo_root=repo_root)
    cases: list[LabelledCase] = []
    seen: set[str] = set()
    with resolved.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if line.strip() == "":
                raise DatasetError(f"line {line_number}: blank lines are not allowed")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise DatasetError(
                    f"line {line_number}: invalid JSON at column {error.colno}"
                ) from error
            case = _case_from_json(value, where=f"line {line_number}")
            if case.case_id in seen:
                raise DatasetError(
                    f"line {line_number}: duplicate case id {case.case_id!r}"
                )
            seen.add(case.case_id)
            cases.append(case)
    if not cases:
        raise DatasetError("the labelled dataset is empty")
    return tuple(cases)


def _case_needs_owner_review(case: LabelledCase) -> bool:
    if case.needs_owner_review:
        return True
    if case.function != RELEVANCE_FUNCTION:
        return False
    return any(
        label.needs_owner_review
        for label in _relevance_labels(
            case.expected, where=f"case {case.case_id}"
        )
    )


def production_reasons(cases: Sequence[LabelledCase]) -> tuple[str, ...]:
    """Return why a run is non-production; empty means production.

    A blank sender role cannot be scored against real eligibility policy, and
    a row the owner still has to review is not a final label (O10-2). The
    reasons stay generic: no case id or dataset content is stored or printed.
    """
    reasons: list[str] = []
    if any(not (case.sender_role or "").strip() for case in cases):
        reasons.append("blank sender role")
    if any(_case_needs_owner_review(case) for case in cases):
        reasons.append("needs owner review")
    return tuple(reasons)


# ---------------------------------------------------------------------------
# Owner CSV converter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CsvRow:
    line: int
    case_id: str
    conversation_id: str
    timestamp: str | None
    sender_role: str | None
    masked_text: str | None
    relevance: str
    hard_negative_category: str | None
    proposal_expected: str
    proposal_type: str | None
    assignment_mode: str | None
    assignees: tuple[str, ...]
    people_needed: int | None
    people_needed_unlimited: int
    task_type: str | None
    expected_description: str | None
    attributes: tuple[tuple[str, str | None], ...]
    label_confidence: str | None
    needs_owner_review: bool


def _parse_people_needed(text: str, *, line: int) -> tuple[int | None, int]:
    value = text.strip()
    if value == "":
        return (None, 0)
    if value.upper() in ("UNLIMITED", "ANY", "*"):
        return (None, 1)
    try:
        number = int(value)
    except ValueError as error:
        raise DatasetError(f"line {line}: people_needed must be an integer") from error
    if number <= 0:
        raise DatasetError(f"line {line}: people_needed must be positive")
    return (number, 0)


def _parse_assignees(text: str) -> tuple[str, ...]:
    return tuple(
        part.strip() for part in re.split(r"[,;]", text) if part.strip()
    )


def _parse_attributes(text: str, *, line: int) -> tuple[tuple[str, str | None], ...]:
    value = text.strip()
    if value == "":
        return ()
    parsed: object
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        pairs: list[tuple[str, str | None]] = []
        for index, item in enumerate(parsed):
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise DatasetError(
                    f"line {line}: attributes[{index}] must be a key/value pair"
                )
            key = str(item[0]).strip()
            raw = item[1]
            pairs.append((key, None if raw is None else str(raw)))
        return tuple(pairs)
    if isinstance(parsed, dict):
        return tuple(
            (str(key).strip(), None if raw is None else str(raw))
            for key, raw in parsed.items()
        )
    pairs = []
    for part in value.split(";"):
        if part.strip() == "":
            continue
        if "=" not in part:
            raise DatasetError(f"line {line}: attributes must be key=value pairs")
        key, raw = part.split("=", 1)
        pairs.append((key.strip(), raw.strip() or None))
    return tuple(pairs)


def _csv_row(values: Mapping[str, str], *, line: int) -> _CsvRow:
    case_id = values.get("case_id", "").strip()
    conversation_id = values.get("conversation_id", "").strip()
    if case_id == "":
        raise DatasetError(f"line {line}: case_id must not be blank")
    if conversation_id == "":
        raise DatasetError(f"line {line}: conversation_id must not be blank")
    relevance = values.get("relevance", "").strip().upper()
    if relevance not in ("RELEVANT", "IRRELEVANT", "EXCLUDED"):
        raise DatasetError(f"line {line}: relevance must be RELEVANT, IRRELEVANT or EXCLUDED")
    sender_role = values.get("sender_role", "").strip() or None
    proposal_expected = values.get("proposal_expected", "").strip().upper()
    if relevance != "EXCLUDED" and proposal_expected not in ("YES", "NO"):
        raise DatasetError(f"line {line}: proposal_expected must be YES or NO")
    people_needed, people_needed_unlimited = _parse_people_needed(
        values.get("people_needed", ""), line=line
    )
    return _CsvRow(
        line=line,
        case_id=case_id,
        conversation_id=conversation_id,
        timestamp=values.get("timestamp", "").strip() or None,
        sender_role=sender_role,
        masked_text=values.get("masked_text", "").strip() or None,
        relevance=relevance,
        hard_negative_category=(
            values.get("hard_negative_category", "").strip() or None
        ),
        proposal_expected=proposal_expected,
        proposal_type=values.get("proposal_type", "").strip().upper() or None,
        assignment_mode=values.get("assignment_mode", "").strip().upper() or None,
        assignees=_parse_assignees(values.get("assignees_text", "")),
        people_needed=people_needed,
        people_needed_unlimited=people_needed_unlimited,
        task_type=values.get("task_type_guess", "").strip().upper() or None,
        expected_description=(
            values.get("expected_description", "").strip() or None
        ),
        attributes=_parse_attributes(values.get("attributes", ""), line=line),
        label_confidence=(
            values.get("label_confidence", "").strip().upper() or None
        ),
        needs_owner_review=_boolish(
            values.get("needs_owner_review", ""), f"line {line}.needs_owner_review"
        ),
    )


def _csv_message(row: _CsvRow, message_id: int) -> dict[str, object]:
    return {
        "message_id": message_id,
        "sender_name": None,
        "event_timestamp": row.timestamp,
        "reply_to_message_id": None,
        "masked_content": row.masked_text,
        "mention_handles": [],
    }


def _csv_proposal_message(row: _CsvRow, message_id: int) -> dict[str, object]:
    return {
        "message_id": message_id,
        "sender_name": None,
        "event_timestamp": row.timestamp,
        "reply_reference": None,
        "content": row.masked_text,
        "mention_handles": [],
    }


def _csv_relevance_entry(row: _CsvRow, message_id: int) -> dict[str, object]:
    return {
        "message_id": message_id,
        "decision": row.relevance,
        "hard_negative_category": row.hard_negative_category,
        "label_confidence": row.label_confidence,
        "needs_owner_review": row.needs_owner_review,
    }


def _csv_proposal(row: _CsvRow, message_id: int) -> dict[str, object]:
    ptype = row.proposal_type
    if ptype not in PTYPE_VOCABULARY:
        raise DatasetError(
            f"line {row.line}: proposal_type must be TASK or ANNOUNCEMENT"
        )
    mode = row.assignment_mode
    if ptype == "ANNOUNCEMENT":
        if mode is None:
            mode = "NONE"
        if mode != "NONE":
            raise DatasetError(
                f"line {row.line}: an ANNOUNCEMENT must use assignment mode NONE"
            )
        task_type = row.task_type or "INTERNAL"
        if task_type not in TASK_TYPE_VOCABULARY:
            raise DatasetError(f"line {row.line}: unknown task type")
        if row.assignees:
            raise DatasetError(
                f"line {row.line}: an ANNOUNCEMENT cannot name assignees"
            )
        description = row.expected_description
    else:
        if mode is None:
            raise DatasetError(f"line {row.line}: a TASK needs an assignment mode")
        if mode not in ("EXPLICIT", "OPEN"):
            raise DatasetError(f"line {row.line}: a TASK cannot use assignment mode NONE")
        task_type_value = row.task_type
        if task_type_value is None or task_type_value not in TASK_TYPE_VOCABULARY:
            raise DatasetError(f"line {row.line}: a TASK needs a task type")
        task_type = task_type_value
        if mode == "EXPLICIT" and not row.assignees:
            raise DatasetError(f"line {row.line}: EXPLICIT needs named assignees")
        if mode == "OPEN" and row.assignees:
            raise DatasetError(
                f"line {row.line}: OPEN cannot name assignees"
            )
        if row.expected_description is None:
            raise DatasetError(
                f"line {row.line}: a TASK needs a description or the"
                " not-stated placeholder"
            )
        description = row.expected_description
    return {
        "primary_message_id": message_id,
        "ptype": ptype,
        "assignment_mode": mode,
        "task_type": task_type,
        "people_needed": row.people_needed,
        "people_needed_unlimited": row.people_needed_unlimited,
        "assignees": list(row.assignees),
        "description": description,
        "attributes": [list(pair) for pair in row.attributes],
    }


def convert_owner_csv(
    path: str | Path, *, repo_root: str | Path | None = None
) -> CsvConversion:
    """Convert the owner's CSV into relevance and proposal cases.

    One relevance case is produced per conversation with exactly one expected
    decision per message. One proposal case is produced per message whose
    ``proposal_expected`` is ``YES``; a relevance-only row (for example a
    movement-only "jaldi pahucho" message) therefore yields zero expected
    proposals. Rows labelled ``EXCLUDED`` are skipped and counted. A blank
    ``sender_role`` is allowed and makes the run non-production.
    """
    resolved = validate_dataset_path(path, repo_root=repo_root)
    with resolved.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or ()
        missing = [column for column in CSV_COLUMNS if column not in fieldnames]
        if missing:
            raise DatasetError(
                "the owner CSV is missing required columns: " + ", ".join(missing)
            )
        rows: list[_CsvRow] = []
        excluded_count = 0
        for line_number, raw in enumerate(reader, start=2):
            values = {
                key: (value if value is not None else "")
                for key, value in raw.items()
                if key is not None
            }
            if values.get("relevance", "").strip().upper() == "EXCLUDED":
                excluded_count += 1
                continue
            rows.append(_csv_row(values, line=line_number))

    seen_ids: set[str] = set()
    for row in rows:
        if row.case_id in seen_ids:
            raise DatasetError(f"line {row.line}: duplicate case_id {row.case_id!r}")
        seen_ids.add(row.case_id)

    groups: dict[str, list[_CsvRow]] = {}
    for row in rows:
        groups.setdefault(row.conversation_id, []).append(row)

    cases: list[LabelledCase] = []
    for conversation_id, group in groups.items():
        messages = [
            _csv_message(row, message_id)
            for message_id, row in enumerate(group, start=1)
        ]
        decisions = [
            _csv_relevance_entry(row, message_id)
            for message_id, row in enumerate(group, start=1)
        ]
        roles = [row.sender_role for row in group]
        sender_role = None if any(role is None for role in roles) else roles[0]
        cases.append(
            LabelledCase(
                case_id=f"relevance:{conversation_id}",
                function=RELEVANCE_FUNCTION,
                sender_role=sender_role,
                payload={
                    "bucket_id": len(cases),
                    "group_handle": f"g{len(cases) + 1}",
                    "vertical_name": None,
                    "messages": messages,
                },
                expected={"messages": decisions},
            )
        )

    for row in rows:
        if row.proposal_expected != "YES":
            continue
        group = groups[row.conversation_id]
        message_id = next(
            index for index, candidate in enumerate(group, start=1) if candidate is row
        )
        context = [
            _csv_proposal_message(candidate, index)
            for index, candidate in enumerate(group, start=1)
            if candidate is not row
        ]
        cases.append(
            LabelledCase(
                case_id=f"proposal:{row.case_id}",
                function=PROPOSAL_FUNCTION,
                sender_role=row.sender_role,
                payload={
                    "bucket_id": len(cases),
                    "vertical_name": None,
                    "candidate_messages": [
                        _csv_proposal_message(row, message_id)
                    ],
                    "context_messages": context,
                },
                expected={"proposals": [_csv_proposal(row, message_id)]},
                label_confidence=row.label_confidence,
                needs_owner_review=row.needs_owner_review,
                hard_negative_category=row.hard_negative_category,
            )
        )

    return CsvConversion(cases=tuple(cases), excluded_count=excluded_count)


# ---------------------------------------------------------------------------
# Request building (injected adapter boundary)
# ---------------------------------------------------------------------------


def _context_message(value: object, *, where: str) -> ContextMessage:
    item = _mapping(value, where)
    return ContextMessage(
        message_id=_integer(item.get("message_id"), f"{where}.message_id"),
        sender_person_id=_optional_integer(
            item.get("sender_person_id"), f"{where}.sender_person_id"
        ),
        sender_name=_optional_text(item.get("sender_name"), f"{where}.sender_name"),
        event_timestamp=_optional_text(
            item.get("event_timestamp"), f"{where}.event_timestamp"
        ),
        reply_to_message_id=_optional_integer(
            item.get("reply_to_message_id"), f"{where}.reply_to_message_id"
        ),
        masked_content=_optional_text(
            item.get("masked_content"), f"{where}.masked_content"
        ),
        mention_handles=_string_tuple(
            item.get("mention_handles", []), f"{where}.mention_handles"
        ),
    )


def _proposal_message(value: object, *, where: str) -> ProposalMessage:
    item = _mapping(value, where)
    return ProposalMessage(
        message_id=_integer(item.get("message_id"), f"{where}.message_id"),
        sender_name=_optional_text(item.get("sender_name"), f"{where}.sender_name"),
        event_timestamp=_optional_text(
            item.get("event_timestamp"), f"{where}.event_timestamp"
        ),
        reply_reference=_optional_integer(
            item.get("reply_reference"), f"{where}.reply_reference"
        ),
        content=_optional_text(item.get("content"), f"{where}.content"),
        mention_handles=_string_tuple(
            item.get("mention_handles", []), f"{where}.mention_handles"
        ),
    )


def _optional_bucket_id(payload: Mapping[str, object], *, where: str) -> int:
    return _integer(payload.get("bucket_id", 0), f"{where}.bucket_id")


def _relevance_request(case: LabelledCase) -> RelevanceRequest:
    payload = case.payload
    entries = _sequence(payload.get("messages"), f"case {case.case_id}: payload.messages")
    return RelevanceRequest(
        bucket_id=_optional_bucket_id(payload, where=f"case {case.case_id}"),
        group_handle=_text(
            payload.get("group_handle", "g0"), f"case {case.case_id}: group_handle"
        ),
        vertical_name=_optional_text(
            payload.get("vertical_name"), f"case {case.case_id}: vertical_name"
        ),
        instructions=(
            _optional_text(
                payload.get("instructions"), f"case {case.case_id}: instructions"
            )
            or CLASSIFICATION_POLICY
        ),
        messages=tuple(
            _context_message(entry, where=f"case {case.case_id}: messages[{index}]")
            for index, entry in enumerate(entries)
        ),
    )


def _proposal_request(case: LabelledCase) -> ProposalGenerationRequest:
    payload = case.payload
    candidates = _sequence(
        payload.get("candidate_messages"),
        f"case {case.case_id}: payload.candidate_messages",
    )
    context = _sequence(
        payload.get("context_messages", []),
        f"case {case.case_id}: payload.context_messages",
    )
    return ProposalGenerationRequest(
        bucket_id=_optional_bucket_id(payload, where=f"case {case.case_id}"),
        vertical_name=_optional_text(
            payload.get("vertical_name"), f"case {case.case_id}: vertical_name"
        ),
        candidate_messages=tuple(
            _proposal_message(
                entry, where=f"case {case.case_id}: candidate_messages[{index}]"
            )
            for index, entry in enumerate(candidates)
        ),
        context_messages=tuple(
            _proposal_message(
                entry, where=f"case {case.case_id}: context_messages[{index}]"
            )
            for index, entry in enumerate(context)
        ),
        instructions=(
            _optional_text(
                payload.get("instructions"), f"case {case.case_id}: instructions"
            )
            or PROPOSAL_PROMPT
        ),
    )


# ---------------------------------------------------------------------------
# Wilson interval and metric helpers
# ---------------------------------------------------------------------------


def wilson_interval(
    successes: int, total: int, *, z: float = 1.96
) -> tuple[float | None, float | None]:
    """95% Wilson score interval for a binomial proportion.

    ``z`` defaults to the two-sided 95% normal quantile (1.96); no dependency is
    used. A zero denominator has no interval and returns ``(None, None)``.
    """
    if successes < 0 or total < 0 or successes > total:
        raise ValueError("successes must be between 0 and total")
    if total == 0:
        return (None, None)
    proportion = successes / total
    denominator = 1.0 + z * z / total
    centre = (proportion + z * z / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / total
            + z * z / (4.0 * total * total)
        )
        / denominator
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _ratios(
    true_positive: int, false_positive: int, false_negative: int
) -> tuple[float | None, float | None, float | None]:
    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else None
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else None
    )
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0.0:
        f1 = 0.0
    else:
        f1 = 2.0 * precision * recall / (precision + recall)
    return (precision, recall, f1)


@dataclass(frozen=True)
class RelevanceMetrics:
    n: int
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int
    missing: int
    false_irrelevant: int
    precision: float | None
    recall: float | None
    f1: float | None
    precision_interval: tuple[float | None, float | None]
    recall_interval: tuple[float | None, float | None]


@dataclass(frozen=True)
class HardNegativeScore:
    category: str
    total: int
    correct: int
    false_positive: int
    missing: int
    accuracy: float | None


@dataclass(frozen=True)
class RelevanceScore:
    overall: RelevanceMetrics
    high_confidence: RelevanceMetrics
    needs_owner_review: RelevanceMetrics
    hard_negatives: tuple[HardNegativeScore, ...]
    failed_case_count: int
    unexpected_prediction_count: int


@dataclass(frozen=True)
class _RelevanceObservation:
    expected: str
    predicted: str | None
    hard_negative_category: str | None
    label_confidence: str | None
    needs_owner_review: bool


def _relevance_metrics(
    observations: Sequence[_RelevanceObservation],
) -> RelevanceMetrics:
    true_positive = false_positive = true_negative = false_negative = 0
    missing = false_irrelevant = 0
    for observation in observations:
        if observation.predicted is None:
            missing += 1
            if observation.expected == "RELEVANT":
                false_negative += 1
            continue
        if observation.expected == "RELEVANT":
            if observation.predicted == "RELEVANT":
                true_positive += 1
            else:
                false_negative += 1
                false_irrelevant += 1
        elif observation.predicted == "RELEVANT":
            false_positive += 1
        else:
            true_negative += 1
    precision, recall, f1 = _ratios(true_positive, false_positive, false_negative)
    return RelevanceMetrics(
        n=len(observations),
        true_positive=true_positive,
        false_positive=false_positive,
        true_negative=true_negative,
        false_negative=false_negative,
        missing=missing,
        false_irrelevant=false_irrelevant,
        precision=precision,
        recall=recall,
        f1=f1,
        precision_interval=wilson_interval(
            true_positive, true_positive + false_positive
        ),
        recall_interval=wilson_interval(true_positive, true_positive + false_negative),
    )


def _hard_negative_scores(
    observations: Sequence[_RelevanceObservation],
) -> tuple[HardNegativeScore, ...]:
    grouped: dict[str, list[_RelevanceObservation]] = {}
    for observation in observations:
        if (
            observation.expected == "IRRELEVANT"
            and observation.hard_negative_category
        ):
            grouped.setdefault(observation.hard_negative_category, []).append(
                observation
            )
    scores: list[HardNegativeScore] = []
    for category in sorted(grouped):
        items = grouped[category]
        correct = sum(1 for item in items if item.predicted == "IRRELEVANT")
        false_positive = sum(1 for item in items if item.predicted == "RELEVANT")
        missing = sum(1 for item in items if item.predicted is None)
        scores.append(
            HardNegativeScore(
                category=category,
                total=len(items),
                correct=correct,
                false_positive=false_positive,
                missing=missing,
                accuracy=correct / len(items) if items else None,
            )
        )
    return tuple(scores)


def score_relevance(
    cases: Sequence[LabelledCase],
    responses: Mapping[str, RelevanceResponse | None],
) -> RelevanceScore:
    """Score relevance predictions; a missing prediction is a miss."""
    observations: list[_RelevanceObservation] = []
    failed_case_count = 0
    unexpected_prediction_count = 0
    for case in cases:
        if case.function != RELEVANCE_FUNCTION:
            continue
        labels = _relevance_labels(case.expected, where=f"case {case.case_id}")
        response = responses.get(case.case_id)
        decisions: dict[int, str | None] = {}
        if response is None:
            failed_case_count += 1
        else:
            for decision in response.decisions:
                value = (
                    decision.decision
                    if decision.decision in DECISION_VOCABULARY
                    else None
                )
                decisions[decision.message_id] = value
            expected_ids = {label.message_id for label in labels}
            unexpected_prediction_count += len(set(decisions) - expected_ids)
        for label in labels:
            observations.append(
                _RelevanceObservation(
                    expected=label.decision,
                    predicted=decisions.get(label.message_id),
                    hard_negative_category=label.hard_negative_category,
                    label_confidence=label.label_confidence,
                    needs_owner_review=label.needs_owner_review,
                )
            )
    high_confidence = [
        item
        for item in observations
        if (item.label_confidence or "").upper() == "HIGH"
    ]
    needs_review = [item for item in observations if item.needs_owner_review]
    return RelevanceScore(
        overall=_relevance_metrics(observations),
        high_confidence=_relevance_metrics(high_confidence),
        needs_owner_review=_relevance_metrics(needs_review),
        hard_negatives=_hard_negative_scores(observations),
        failed_case_count=failed_case_count,
        unexpected_prediction_count=unexpected_prediction_count,
    )


# ---------------------------------------------------------------------------
# Proposal scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalFieldScore:
    field: str
    correct: int
    total: int
    accuracy: float | None


@dataclass(frozen=True)
class ProposalScore:
    expected_count: int
    predicted_count: int
    matched_count: int
    correct_count: int
    false_positive_count: int
    false_negative_count: int
    field_scores: tuple[ProposalFieldScore, ...]
    failed_case_count: int


def _normalised_assignees(tokens: Sequence[str]) -> frozenset[str]:
    return frozenset(token.strip().casefold() for token in tokens)


def _is_visible_placeholder(description: str | None) -> bool:
    return (
        description is not None
        and _PLACEHOLDER_MARKER in description.strip().upper()
    )


def _proposal_field_results(
    label: _ProposalLabel, draft: ProposalDraft
) -> tuple[tuple[str, bool], ...]:
    results: list[tuple[str, bool]] = [
        ("ptype", draft.ptype == label.ptype),
        ("assignment_mode", draft.assignment_mode == label.assignment_mode),
        ("task_type", draft.task_type == label.task_type),
        (
            "people_needed",
            draft.people_needed == label.people_needed
            and draft.people_needed_unlimited == label.people_needed_unlimited,
        ),
        (
            "assignees",
            _normalised_assignees(draft.proposed_assignees or ())
            == _normalised_assignees(label.assignees),
        ),
    ]
    if label.description is not None:
        if _is_visible_placeholder(label.description):
            description_correct = _is_visible_placeholder(draft.description)
        else:
            description_correct = (
                draft.description is not None and draft.description.strip() != ""
            )
        results.append(("description", description_correct))
    return tuple(results)


def _scored_proposal_fields(label: _ProposalLabel) -> tuple[str, ...]:
    if label.description is None:
        return _PROPOSAL_FIELDS[:-1]
    return _PROPOSAL_FIELDS


def score_proposals(
    cases: Sequence[LabelledCase],
    responses: Mapping[str, ProposalResponse | None],
) -> ProposalScore:
    """Score proposal fields; extra proposals are false positives.

    Descriptions are never compared for equality. A placeholder label requires
    a visible ``[PLACEHOLDER: ...]`` marker (FR-PRP-11); any other description
    is scored for presence only, because the owner's free text is not a
    target string.
    """
    field_counts: dict[str, list[int]] = {
        field: [0, 0] for field in _PROPOSAL_FIELDS
    }
    expected_count = predicted_count = matched_count = correct_count = 0
    false_positive_count = false_negative_count = failed_case_count = 0
    for case in cases:
        if case.function != PROPOSAL_FUNCTION:
            continue
        labels = _proposal_labels(case.expected, where=f"case {case.case_id}")
        response = responses.get(case.case_id)
        if response is None:
            failed_case_count += 1
            drafts: list[ProposalDraft] = []
        else:
            drafts = list(response.proposals)
        predicted_count += len(drafts)
        by_message: dict[int, list[ProposalDraft]] = {}
        for draft in drafts:
            by_message.setdefault(draft.primary_message_id, []).append(draft)
        for label in labels:
            expected_count += 1
            candidates = by_message.get(label.primary_message_id)
            if not candidates:
                false_negative_count += 1
                for field in _scored_proposal_fields(label):
                    field_counts[field][1] += 1
                continue
            draft = candidates.pop(0)
            matched_count += 1
            all_correct = True
            for field, correct in _proposal_field_results(label, draft):
                field_counts[field][1] += 1
                if correct:
                    field_counts[field][0] += 1
                else:
                    all_correct = False
            if all_correct:
                correct_count += 1
        false_positive_count += sum(len(items) for items in by_message.values())
    field_scores = tuple(
        ProposalFieldScore(
            field=field,
            correct=field_counts[field][0],
            total=field_counts[field][1],
            accuracy=(
                field_counts[field][0] / field_counts[field][1]
                if field_counts[field][1]
                else None
            ),
        )
        for field in _PROPOSAL_FIELDS
    )
    return ProposalScore(
        expected_count=expected_count,
        predicted_count=predicted_count,
        matched_count=matched_count,
        correct_count=correct_count,
        false_positive_count=false_positive_count,
        false_negative_count=false_negative_count,
        field_scores=field_scores,
        failed_case_count=failed_case_count,
    )


# ---------------------------------------------------------------------------
# End-to-end scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EndToEndScore:
    period_count: int
    actionable_expected: int
    actionable_predicted: int
    actionable_matched: int
    recall: float | None
    precision: float | None
    f1: float | None
    recall_interval: tuple[float | None, float | None]
    precision_interval: tuple[float | None, float | None]
    proposal_count: int
    proposals_per_reviewer_day: float | None
    reviewed_count: int
    rejected_count: int
    rejected_share: float | None
    failed_case_count: int


def score_end_to_end(
    cases: Sequence[LabelledCase],
    responses: Mapping[str, ProposalResponse | None],
) -> EndToEndScore:
    """Score actionable-item recall/precision and review burden (AI-08)."""
    period_count = 0
    actionable_expected = actionable_predicted = actionable_matched = 0
    proposal_count = reviewed_count = rejected_count = failed_case_count = 0
    reviewer_days = 0
    reviewer_days_complete = True
    for case in cases:
        if case.function != END_TO_END_FUNCTION:
            continue
        period_count += 1
        label = _end_to_end_label(case.expected, where=f"case {case.case_id}")
        response = responses.get(case.case_id)
        if response is None:
            failed_case_count += 1
            drafts: list[ProposalDraft] = []
        else:
            drafts = list(response.proposals)
        proposal_count += len(drafts)
        predicted_ids = {draft.primary_message_id for draft in drafts}
        expected_ids = set(label.actionable_message_ids)
        actionable_expected += len(expected_ids)
        actionable_predicted += len(predicted_ids)
        actionable_matched += len(expected_ids & predicted_ids)
        if label.reviewer_days is None:
            reviewer_days_complete = False
        else:
            reviewer_days += label.reviewer_days
        for decision in label.review_decisions.values():
            reviewed_count += 1
            if decision == _REJECT_DECISION:
                rejected_count += 1
    precision, recall, f1 = _ratios(
        actionable_matched,
        actionable_predicted - actionable_matched,
        actionable_expected - actionable_matched,
    )
    return EndToEndScore(
        period_count=period_count,
        actionable_expected=actionable_expected,
        actionable_predicted=actionable_predicted,
        actionable_matched=actionable_matched,
        recall=recall,
        precision=precision,
        f1=f1,
        recall_interval=wilson_interval(
            actionable_matched, actionable_expected
        ),
        precision_interval=wilson_interval(
            actionable_matched, actionable_predicted
        ),
        proposal_count=proposal_count,
        proposals_per_reviewer_day=(
            proposal_count / reviewer_days
            if reviewer_days_complete and reviewer_days > 0
            else None
        ),
        reviewed_count=reviewed_count,
        rejected_count=rejected_count,
        rejected_share=(
            rejected_count / reviewed_count if reviewed_count else None
        ),
        failed_case_count=failed_case_count,
    )


# ---------------------------------------------------------------------------
# Orchestration and report
# ---------------------------------------------------------------------------


class EvaluationAdapter(Protocol):
    """The two injected, stateless AI protocols the harness consumes."""

    def classify(self, request: RelevanceRequest) -> RelevanceResponse:
        """Return an untrusted relevance response for ``request``."""

    def generate(self, request: ProposalGenerationRequest) -> ProposalResponse:
        """Return an untrusted proposal response for ``request``."""


@dataclass(frozen=True)
class EvaluationResult:
    case_count: int
    excluded_count: int
    production_reasons: tuple[str, ...]
    relevance: RelevanceScore
    proposals: ProposalScore
    end_to_end: EndToEndScore

    @property
    def is_production(self) -> bool:
        return not self.production_reasons


@dataclass(frozen=True)
class _Predictions:
    relevance: dict[str, RelevanceResponse | None]
    proposals: dict[str, ProposalResponse | None]
    end_to_end: dict[str, ProposalResponse | None]
    latencies_ms: dict[str, float | None]


def _collect_predictions(
    cases: Sequence[LabelledCase], adapter: EvaluationAdapter
) -> _Predictions:
    """Call the injected adapter once per case and time each call.

    An adapter exception is recorded as a missing prediction, never as a valid
    empty or IRRELEVANT result. The latency is recorded for every call,
    including a failed or missing prediction.
    """
    relevance: dict[str, RelevanceResponse | None] = {}
    proposals: dict[str, ProposalResponse | None] = {}
    end_to_end: dict[str, ProposalResponse | None] = {}
    latencies: dict[str, float | None] = {}
    for case in cases:
        if case.function not in KNOWN_FUNCTIONS:
            raise EvaluationError(f"unknown function {case.function!r}")
        started = time.perf_counter()
        try:
            if case.function == RELEVANCE_FUNCTION:
                relevance[case.case_id] = adapter.classify(
                    _relevance_request(case)
                )
            elif case.function == PROPOSAL_FUNCTION:
                proposals[case.case_id] = adapter.generate(
                    _proposal_request(case)
                )
            else:
                end_to_end[case.case_id] = adapter.generate(
                    _proposal_request(case)
                )
        except Exception:
            if case.function == RELEVANCE_FUNCTION:
                relevance[case.case_id] = None
            elif case.function == PROPOSAL_FUNCTION:
                proposals[case.case_id] = None
            else:
                end_to_end[case.case_id] = None
        finally:
            latencies[case.case_id] = (time.perf_counter() - started) * 1000.0
    return _Predictions(relevance, proposals, end_to_end, latencies)


def _score_predictions(
    cases: Sequence[LabelledCase],
    predictions: _Predictions,
    excluded_count: int,
) -> EvaluationResult:
    return EvaluationResult(
        case_count=len(cases),
        excluded_count=excluded_count,
        production_reasons=production_reasons(cases),
        relevance=score_relevance(cases, predictions.relevance),
        proposals=score_proposals(cases, predictions.proposals),
        end_to_end=score_end_to_end(cases, predictions.end_to_end),
    )


def evaluate_cases(
    cases: Sequence[LabelledCase],
    *,
    adapter: EvaluationAdapter,
    excluded_count: int = 0,
) -> EvaluationResult:
    """Run the injected adapter over the cases and score the predictions."""
    return _score_predictions(
        cases, _collect_predictions(cases, adapter), excluded_count
    )


def evaluate_cases_with_predictions(
    cases: Sequence[LabelledCase],
    *,
    adapter: EvaluationAdapter,
    excluded_count: int = 0,
) -> tuple[EvaluationResult, _Predictions]:
    """Like :func:`evaluate_cases` but also returns the raw predictions."""
    predictions = _collect_predictions(cases, adapter)
    return _score_predictions(cases, predictions, excluded_count), predictions


def _format_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _format_interval(interval: tuple[float | None, float | None]) -> str:
    return f"[{_format_ratio(interval[0])}, {_format_ratio(interval[1])}]"


def _relevance_line(label: str, metrics: RelevanceMetrics) -> str:
    return (
        f"{label}: n={metrics.n} tp={metrics.true_positive}"
        f" fp={metrics.false_positive} tn={metrics.true_negative}"
        f" fn={metrics.false_negative} missing={metrics.missing}"
        f" false_irrelevant={metrics.false_irrelevant}"
        f" precision={_format_ratio(metrics.precision)}"
        f" recall={_format_ratio(metrics.recall)}"
        f" f1={_format_ratio(metrics.f1)}"
        f" precision_95ci={_format_interval(metrics.precision_interval)}"
        f" recall_95ci={_format_interval(metrics.recall_interval)}"
    )


def format_report(result: EvaluationResult) -> str:
    """Render counts and metrics only: no message text, ids or case ids."""
    status = "PRODUCTION" if result.is_production else "NON-PRODUCTION"
    lines = [
        "ASEC evaluation report",
        f"production: {status}",
    ]
    if result.production_reasons:
        lines.append(
            "non-production reasons: " + "; ".join(result.production_reasons)
        )
    lines.append(f"cases: {result.case_count} excluded: {result.excluded_count}")
    relevance = result.relevance
    lines.append(_relevance_line("relevance overall", relevance.overall))
    lines.append(_relevance_line("relevance confidence=HIGH", relevance.high_confidence))
    lines.append(
        _relevance_line(
            "relevance needs_owner_review", relevance.needs_owner_review
        )
    )
    for hard in relevance.hard_negatives:
        category = hard.category.replace("\r", " ").replace("\n", " ")
        lines.append(
            f"hard_negative category={category} total={hard.total}"
            f" correct={hard.correct} false_positive={hard.false_positive}"
            f" missing={hard.missing} accuracy={_format_ratio(hard.accuracy)}"
        )
    lines.append(
        f"relevance failed_cases={relevance.failed_case_count}"
        f" unexpected_predictions={relevance.unexpected_prediction_count}"
    )
    proposals = result.proposals
    lines.append(
        f"proposal: expected={proposals.expected_count}"
        f" predicted={proposals.predicted_count}"
        f" matched={proposals.matched_count}"
        f" correct={proposals.correct_count}"
        f" false_positive={proposals.false_positive_count}"
        f" false_negative={proposals.false_negative_count}"
        f" failed_cases={proposals.failed_case_count}"
    )
    for field in proposals.field_scores:
        lines.append(
            f"proposal field {field.field}: correct={field.correct}"
            f" total={field.total} accuracy={_format_ratio(field.accuracy)}"
        )
    end_to_end = result.end_to_end
    lines.append(
        f"end_to_end: periods={end_to_end.period_count}"
        f" actionable_expected={end_to_end.actionable_expected}"
        f" actionable_predicted={end_to_end.actionable_predicted}"
        f" actionable_matched={end_to_end.actionable_matched}"
        f" recall={_format_ratio(end_to_end.recall)}"
        f" precision={_format_ratio(end_to_end.precision)}"
        f" f1={_format_ratio(end_to_end.f1)}"
        f" recall_95ci={_format_interval(end_to_end.recall_interval)}"
        f" precision_95ci={_format_interval(end_to_end.precision_interval)}"
    )
    lines.append(
        f"end_to_end proposals={end_to_end.proposal_count}"
        f" proposals_per_reviewer_day="
        f"{_format_ratio(end_to_end.proposals_per_reviewer_day)}"
        f" reviewed={end_to_end.reviewed_count}"
        f" rejected={end_to_end.rejected_count}"
        f" rejected_share={_format_ratio(end_to_end.rejected_share)}"
        f" failed_cases={end_to_end.failed_case_count}"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# W5: append-only run persistence (0022) and the report writer
# ---------------------------------------------------------------------------

RUN_METRICS_VERSION = "1"


@dataclass(frozen=True)
class CaseRecord:
    """One persisted case result, without any source content."""

    case_id_hash: str
    correct: bool
    latency_ms: float | None


@dataclass(frozen=True)
class EvaluationRun:
    run_id: int
    function: str
    model_id: str
    model_version: str
    prompt_version: str
    adapter_contract_version: str
    dataset_sha256: str
    case_count: int
    dataset_case_counts: Mapping[str, object]
    non_production: bool
    non_production_reasons: tuple[str, ...]
    started_at: str
    finished_at: str
    result: EvaluationResult
    metrics: Mapping[str, object]
    records: tuple[CaseRecord, ...]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _require_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationError(f"{name} must be a non-blank string")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    """Return the SHA-256 of a dataset file without copying its content."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _response_payload(response: object) -> object:
    if isinstance(response, (RelevanceResponse, ProposalResponse)):
        return asdict(response)
    return response


def _case_id_hash(run_id: int, case_id: str) -> str:
    return _sha256_text(f"{run_id}:{case_id}")


def _case_is_correct(case: LabelledCase, response: object | None) -> bool:
    """Score one case with the existing scoring functions (missing = miss)."""
    if response is None:
        return False
    if case.function == RELEVANCE_FUNCTION:
        if not isinstance(response, RelevanceResponse):
            return False
        relevance_score = score_relevance(
            [case], {case.case_id: response}
        )
        overall = relevance_score.overall
        return (
            overall.false_positive == 0
            and overall.false_negative == 0
            and overall.missing == 0
            and relevance_score.unexpected_prediction_count == 0
        )
    if case.function == PROPOSAL_FUNCTION:
        if not isinstance(response, ProposalResponse):
            return False
        proposal_score = score_proposals([case], {case.case_id: response})
        return (
            proposal_score.correct_count == proposal_score.expected_count
            and proposal_score.false_positive_count == 0
            and proposal_score.false_negative_count == 0
            and proposal_score.failed_case_count == 0
        )
    if case.function == END_TO_END_FUNCTION:
        if not isinstance(response, ProposalResponse):
            return False
        end_to_end_score = score_end_to_end(
            [case], {case.case_id: response}
        )
        return (
            end_to_end_score.failed_case_count == 0
            and (
                end_to_end_score.recall is None
                or end_to_end_score.recall == 1.0
            )
            and (
                end_to_end_score.precision is None
                or end_to_end_score.precision == 1.0
            )
        )
    return False


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(
        0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)
    )
    return ordered[index]


def _latency_summary(
    latencies_ms: Mapping[str, float | None],
) -> dict[str, object]:
    values = [
        float(value) for value in latencies_ms.values() if value is not None
    ]
    return {
        "count": len(values),
        "total_ms": sum(values),
        "mean_ms": (sum(values) / len(values)) if values else None,
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "max_ms": max(values) if values else None,
    }


def _dataset_case_counts(
    cases: Sequence[LabelledCase], excluded_count: int
) -> dict[str, object]:
    by_function: dict[str, int] = {}
    for case in cases:
        by_function[case.function] = by_function.get(case.function, 0) + 1
    return {
        "total": len(cases),
        "scored": len(cases),
        "excluded": excluded_count,
        "by_function": by_function,
    }


def _run_metrics(
    *,
    result: EvaluationResult,
    cases: Sequence[LabelledCase],
    excluded_count: int,
    adapter_contract_version: str,
    latencies_ms: Mapping[str, float | None],
    non_production_reasons: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema": RUN_METRICS_VERSION,
        "adapter_contract_version": adapter_contract_version,
        "non_production": bool(non_production_reasons),
        "non_production_reasons": list(non_production_reasons),
        "dataset_case_counts": _dataset_case_counts(cases, excluded_count),
        "latency_ms": _latency_summary(latencies_ms),
        "result": asdict(result),
    }


def persist_evaluation_run(
    connection: sqlite3.Connection,
    *,
    function: str,
    model_id: str,
    model_version: str,
    prompt_version: str,
    adapter_contract_version: str,
    dataset_sha256: str,
    cases: Sequence[LabelledCase],
    responses: Mapping[str, object | None],
    latencies_ms: Mapping[str, float | None],
    result: EvaluationResult,
    excluded_count: int = 0,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> EvaluationRun:
    """Persist one append-only run and its per-case results.

    Only opaque data reaches the database: case ids are SHA-256 hashes of
    ``(run_id, case_id)``, expected/predicted payloads are SHA-256 digests, and
    the dataset itself is represented by its file SHA-256. Message text, sender
    names, Discord ids and labelled descriptions are never stored.
    """
    if function not in KNOWN_FUNCTIONS:
        raise EvaluationError(f"unknown function {function!r}")
    _require_text(model_id, "model_id")
    _require_text(model_version, "model_version")
    _require_text(prompt_version, "prompt_version")
    _require_text(adapter_contract_version, "adapter_contract_version")
    _require_text(dataset_sha256, "dataset_sha256")
    if result.case_count != len(cases):
        raise EvaluationError(
            "result.case_count must match the supplied cases"
        )

    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise EvaluationError(f"duplicate case id {case.case_id!r}")
        seen.add(case.case_id)
        if case.function != function:
            raise EvaluationError("every case must use the run function")

    reasons = production_reasons(cases)
    metrics = _run_metrics(
        result=result,
        cases=cases,
        excluded_count=excluded_count,
        adapter_contract_version=adapter_contract_version,
        latencies_ms=latencies_ms,
        non_production_reasons=reasons,
    )
    started = started_at or _utc_now()
    finished = finished_at or _utc_now()
    records: list[CaseRecord] = []

    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO evaluation_run (function, model_id, model_version,"
            " prompt_version, dataset_sha256, case_count, started_at,"
            " finished_at, metrics_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                function,
                model_id,
                model_version,
                prompt_version,
                dataset_sha256,
                len(cases),
                started,
                finished,
                json.dumps(metrics, ensure_ascii=False, sort_keys=True),
            ),
        )
        run_id = int(cursor.lastrowid or 0)
        if run_id <= 0:  # pragma: no cover - SQLite always returns an id
            raise EvaluationError("could not insert evaluation_run")
        for case in cases:
            case_hash = _case_id_hash(run_id, case.case_id)
            expected_hash = _sha256_text(_canonical_json(case.expected))
            response = responses.get(case.case_id)
            predicted_hash = (
                None
                if response is None
                else _sha256_text(_canonical_json(_response_payload(response)))
            )
            latency = latencies_ms.get(case.case_id)
            predicted_json = json.dumps(
                {"sha256": predicted_hash, "latency_ms": latency},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            correct = 1 if _case_is_correct(case, response) else 0
            connection.execute(
                "INSERT INTO evaluation_case_result (run_id, case_id,"
                " expected_json, predicted_json, correct)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    case_hash,
                    json.dumps(
                        {"sha256": expected_hash},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    predicted_json,
                    correct,
                ),
            )
            records.append(
                CaseRecord(
                    case_id_hash=case_hash,
                    correct=bool(correct),
                    latency_ms=latency,
                )
            )

    return EvaluationRun(
        run_id=run_id,
        function=function,
        model_id=model_id,
        model_version=model_version,
        prompt_version=prompt_version,
        adapter_contract_version=adapter_contract_version,
        dataset_sha256=dataset_sha256,
        case_count=len(cases),
        dataset_case_counts=_dataset_case_counts(cases, excluded_count),
        non_production=bool(reasons),
        non_production_reasons=reasons,
        started_at=started,
        finished_at=finished,
        result=result,
        metrics=metrics,
        records=tuple(records),
    )


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def format_run_report(run: EvaluationRun) -> str:
    """Render run metadata and aggregate metrics only (no case content)."""
    status = "PRODUCTION" if not run.non_production else "NON-PRODUCTION"
    lines = [
        "ASEC evaluation run report",
        f"run: {run.run_id}",
        f"function: {run.function}",
        f"model: {run.model_id} {run.model_version}",
        f"prompt_version: {run.prompt_version}",
        f"adapter_contract_version: {run.adapter_contract_version}",
        f"dataset_sha256: {run.dataset_sha256}",
        f"cases: total={run.case_count}"
        f" excluded={run.dataset_case_counts.get('excluded', 0)}",
        f"production: {status}",
    ]
    if run.non_production_reasons:
        lines.append(
            "non-production reasons: "
            + "; ".join(run.non_production_reasons)
        )
    latency = run.metrics.get("latency_ms")
    if isinstance(latency, Mapping):
        lines.append(
            f"latency_ms: count={latency.get('count')}"
            f" mean={_format_ratio(_optional_float(latency.get('mean_ms')))}"
            f" p50={_format_ratio(_optional_float(latency.get('p50_ms')))}"
            f" p95={_format_ratio(_optional_float(latency.get('p95_ms')))}"
            f" max={_format_ratio(_optional_float(latency.get('max_ms')))}"
        )
    lines.append(format_report(run.result).rstrip("\n"))
    return "\n".join(lines) + "\n"


def write_report(
    target: EvaluationRun | EvaluationResult, path: str | Path
) -> None:
    """Write counts and metrics only to ``path``."""
    text = (
        format_run_report(target)
        if isinstance(target, EvaluationRun)
        else format_report(target)
    )
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")


def run_evaluation(
    connection: sqlite3.Connection,
    *,
    dataset_path: str | Path,
    adapter: EvaluationAdapter,
    output_dir: str | Path,
    model_id: str,
    model_version: str,
    prompt_version: str,
    adapter_contract_version: str,
    excluded_count: int = 0,
    repo_root: str | Path | None = None,
) -> EvaluationRun:
    """Load one single-function dataset, evaluate it, persist and report.

    The dataset file stays outside the repository; only its SHA-256 and
    aggregate counts/metrics are written. A mixed-function dataset is rejected
    because ``evaluation_run.function`` records one function per run.
    """
    started = _utc_now()
    resolved = validate_dataset_path(dataset_path, repo_root=repo_root)
    cases = load_labelled_set(resolved, repo_root=repo_root)
    functions = {case.function for case in cases}
    if len(functions) != 1:
        raise EvaluationError(
            "a run must contain exactly one function; split the dataset"
        )
    function = next(iter(functions))
    digest = file_sha256(resolved)
    result, predictions = evaluate_cases_with_predictions(
        cases, adapter=adapter, excluded_count=excluded_count
    )
    responses = {
        **predictions.relevance,
        **predictions.proposals,
        **predictions.end_to_end,
    }
    run = persist_evaluation_run(
        connection,
        function=function,
        model_id=model_id,
        model_version=model_version,
        prompt_version=prompt_version,
        adapter_contract_version=adapter_contract_version,
        dataset_sha256=digest,
        cases=cases,
        responses=responses,
        latencies_ms=predictions.latencies_ms,
        result=result,
        excluded_count=excluded_count,
        started_at=started,
        finished_at=_utc_now(),
    )
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    write_report(run, destination / f"evaluation-run-{run.run_id}.txt")
    return run


# ---------------------------------------------------------------------------
# W6: correction rate per model version, review burden and exported table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelVersionIdentity:
    model_id: str
    model_version: str
    prompt_version: str


@dataclass(frozen=True)
class CorrectionRateRow:
    model_id: str
    model_version: str
    prompt_version: str
    reviewed: int
    edited: int
    accepted_unchanged: int
    rejected: int
    correction_rate: float | None
    rejected_share: float | None


@dataclass(frozen=True)
class ReviewBurden:
    reviewed: int
    reviewer_days: int
    proposals_per_reviewer_day: float | None
    rejected: int
    rejected_share: float | None


@dataclass(frozen=True)
class CorrectionTable:
    rows: tuple[CorrectionRateRow, ...]
    burden: ReviewBurden


def _proposal_is_edited(proposal: EvalProposal) -> bool:
    return bool(proposal.revisions) or bool(proposal.generated_final_delta)


def _decision_date(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvaluationError(
            "reviewer decision time must be ISO-8601"
        ) from error
    return parsed.date().isoformat()


def review_burden(proposals: Sequence[EvalProposal]) -> ReviewBurden:
    """Return proposal counts per reviewer-day and the rejected share."""
    decided = [
        proposal for proposal in proposals if proposal.decision is not None
    ]
    rejected = sum(
        1 for proposal in decided if proposal.decision == "REJECTED"
    )
    reviewer_days: set[tuple[int, str]] = set()
    for proposal in decided:
        if proposal.decided_by is None or proposal.decided_at is None:
            raise EvaluationError(
                "a decided proposal must name a reviewer and time"
            )
        reviewer_days.add(
            (proposal.decided_by, _decision_date(proposal.decided_at))
        )
    return ReviewBurden(
        reviewed=len(decided),
        reviewer_days=len(reviewer_days),
        proposals_per_reviewer_day=(
            len(decided) / len(reviewer_days) if reviewer_days else None
        ),
        rejected=rejected,
        rejected_share=(rejected / len(decided)) if decided else None,
    )


def correction_table(
    proposals: Sequence[EvalProposal],
    provenance: Mapping[int, ModelVersionIdentity],
) -> CorrectionTable:
    """Group reviewed proposals by model/prompt version.

    A correction is a proposal with at least one revision or a generated-to-
    final field delta. A rejected proposal is counted separately, and an
    accepted proposal with no edit is counted as accepted-unchanged. The rates
    are counts divided by all reviewed proposals in that group.
    """
    grouped: dict[tuple[str, str, str], list[EvalProposal]] = {}
    for proposal in proposals:
        identity = provenance.get(proposal.proposal_id)
        if identity is None:
            raise EvaluationError(
                "missing model provenance for proposal"
                f" {proposal.proposal_id}"
            )
        key = (
            identity.model_id,
            identity.model_version,
            identity.prompt_version,
        )
        grouped.setdefault(key, []).append(proposal)

    rows: list[CorrectionRateRow] = []
    for key in sorted(grouped):
        items = grouped[key]
        edited = sum(1 for item in items if _proposal_is_edited(item))
        rejected = sum(
            1 for item in items if item.decision == "REJECTED"
        )
        accepted_unchanged = sum(
            1
            for item in items
            if item.decision == "ACCEPTED"
            and not _proposal_is_edited(item)
        )
        reviewed = len(items)
        rows.append(
            CorrectionRateRow(
                model_id=key[0],
                model_version=key[1],
                prompt_version=key[2],
                reviewed=reviewed,
                edited=edited,
                accepted_unchanged=accepted_unchanged,
                rejected=rejected,
                correction_rate=(edited / reviewed) if reviewed else None,
                rejected_share=(rejected / reviewed) if reviewed else None,
            )
        )
    return CorrectionTable(rows=tuple(rows), burden=review_burden(proposals))


def export_correction_table(
    connection: sqlite3.Connection,
    *,
    actor_person_id: int,
    policy: ReviewPolicy | None = None,
    config: Config | None = None,
) -> CorrectionTable:
    """Read the reviewed proposals and export correction/review-burden counts.

    Reviewed proposals come from the existing FR-REV-08 reader. Only the
    proposal's pinned model provenance is read directly; no domain table is
    written.
    """
    proposals = list_reviewed_proposals_for_eval(
        connection,
        actor_person_id=actor_person_id,
        policy=policy,
        config=config,
    )
    provenance: dict[int, ModelVersionIdentity] = {}
    if proposals:
        ids = [proposal.proposal_id for proposal in proposals]
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            "SELECT id, model_id, model_version, prompt_version FROM proposal"
            f" WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        provenance = {
            int(row["id"]): ModelVersionIdentity(
                model_id=str(row["model_id"]),
                model_version=str(row["model_version"]),
                prompt_version=str(row["prompt_version"]),
            )
            for row in rows
        }
    return correction_table(proposals, provenance)


def format_correction_table(table: CorrectionTable) -> str:
    """Render the correction/review-burden table with counts only."""
    lines = [
        "ASEC correction rate and review burden (counts only)",
        "model_id | model_version | prompt_version | reviewed | edited |"
        " accepted_unchanged | rejected | correction_rate | rejected_share",
    ]
    for row in table.rows:
        lines.append(
            f"{row.model_id} | {row.model_version} | {row.prompt_version} |"
            f" {row.reviewed} | {row.edited} | {row.accepted_unchanged} |"
            f" {row.rejected} | {_format_ratio(row.correction_rate)} |"
            f" {_format_ratio(row.rejected_share)}"
        )
    burden = table.burden
    lines.append(
        f"review_burden: reviewed={burden.reviewed}"
        f" reviewer_days={burden.reviewer_days}"
        f" proposals_per_reviewer_day="
        f"{_format_ratio(burden.proposals_per_reviewer_day)}"
        f" rejected={burden.rejected}"
        f" rejected_share={_format_ratio(burden.rejected_share)}"
    )
    return "\n".join(lines) + "\n"


def write_correction_table(
    table: CorrectionTable, path: str | Path
) -> None:
    """Write the exported counts table to ``path``."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        format_correction_table(table), encoding="utf-8"
    )
