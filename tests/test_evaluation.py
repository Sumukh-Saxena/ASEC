"""W4 tests: evaluation loader, scoring and end-to-end metrics (plan 4.4/7).

All cases here are small invented synthetic cases. The real owner-labelled set
is never copied into the repository, tests or logs, and no network or model is
used: predictions come from an injected fake adapter.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

import backend.evaluation as evaluation_module
from backend.config import Config
from backend.db.connection import connect
from backend.db.migrations import apply_migrations
from backend.evaluation import (
    DATASET_PATH_ENV,
    WORK_NOT_STATED_PLACEHOLDER,
    DatasetError,
    DatasetPathError,
    EvaluationError,
    LabelledCase,
    ModelVersionIdentity,
    convert_owner_csv,
    correction_table,
    eval_set_path_from_environment,
    evaluate_cases,
    export_correction_table,
    format_correction_table,
    format_report,
    load_labelled_set,
    persist_evaluation_run,
    production_reasons,
    review_burden,
    run_evaluation,
    score_end_to_end,
    score_proposals,
    score_relevance,
    validate_dataset_path,
    wilson_interval,
    write_correction_table,
)
from backend.proposals import (
    ProposalDraft,
    ProposalGenerationRequest,
    ProposalResponse,
)
from backend.relevance import (
    MessageDecision,
    RelevanceRequest,
    RelevanceResponse,
)
from backend.review import EvalFieldDelta, EvalProposal, EvalRevision

SECRET_TEXT = "synthetic-secret-message-text-do-not-print"
SECRET_MESSAGE_ID = 987654


def _rel_message(
    message_id: int,
    decision: str = "RELEVANT",
    *,
    category: str | None = None,
    confidence: str | None = "HIGH",
    review: bool = False,
    content: str = "synthetic message",
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "sender_name": None,
        "event_timestamp": None,
        "reply_to_message_id": None,
        "masked_content": content,
        "mention_handles": [],
        "decision": decision,
        "hard_negative_category": category,
        "label_confidence": confidence,
        "needs_owner_review": review,
    }


def _rel_case(
    case_id: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    sender_role: str | None = "AH",
    bucket_id: int = 1,
) -> LabelledCase:
    payload_messages = [
        {
            "message_id": message["message_id"],
            "sender_name": message.get("sender_name"),
            "event_timestamp": message.get("event_timestamp"),
            "reply_to_message_id": message.get("reply_to_message_id"),
            "masked_content": message.get("masked_content"),
            "mention_handles": list(message.get("mention_handles", ())),
        }
        for message in messages
    ]
    expected_messages = [
        {
            "message_id": message["message_id"],
            "decision": message["decision"],
            "hard_negative_category": message.get("hard_negative_category"),
            "label_confidence": message.get("label_confidence"),
            "needs_owner_review": message.get("needs_owner_review", False),
        }
        for message in messages
    ]
    return LabelledCase(
        case_id=case_id,
        function="RELEVANCE",
        sender_role=sender_role,
        payload={
            "bucket_id": bucket_id,
            "group_handle": "g1",
            "vertical_name": None,
            "messages": payload_messages,
        },
        expected={"messages": expected_messages},
    )


def _proposal_message(message_id: int, content: str = "synthetic message") -> dict[str, Any]:
    return {
        "message_id": message_id,
        "sender_name": None,
        "event_timestamp": None,
        "reply_reference": None,
        "content": content,
        "mention_handles": [],
    }


def _proposal_case(
    case_id: str,
    proposals: Sequence[Mapping[str, Any]],
    *,
    sender_role: str | None = "AH",
    bucket_id: int = 2,
    message_id: int = 1,
) -> LabelledCase:
    return LabelledCase(
        case_id=case_id,
        function="PROPOSAL",
        sender_role=sender_role,
        payload={
            "bucket_id": bucket_id,
            "vertical_name": None,
            "candidate_messages": [_proposal_message(message_id)],
            "context_messages": [],
        },
        expected={"proposals": list(proposals)},
    )


def _relevance_response(
    case_id: str,
    decisions: Sequence[tuple[int, str]],
    *,
    message_ids: Sequence[int] | None = None,
) -> RelevanceResponse:
    del case_id
    del message_ids
    return RelevanceResponse(
        model_id="fake-model",
        model_version="1",
        prompt_version="fake-prompt",
        decisions=tuple(
            MessageDecision(message_id=message_id, decision=decision)
            for message_id, decision in decisions
        ),
    )


def _write_jsonl(path: Path, cases: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )


def _valid_jsonl_case(
    *,
    case_id: str = "rel-1",
    function: str = "RELEVANCE",
    sender_role: str | None = "AH",
) -> dict[str, Any]:
    if function == "RELEVANCE":
        return {
            "case_id": case_id,
            "function": function,
            "sender_role": sender_role,
            "payload": {
                "bucket_id": 1,
                "group_handle": "g1",
                "messages": [_rel_message(1, content=SECRET_TEXT)],
            },
            "expected": {
                "messages": [
                    {
                        "message_id": 1,
                        "decision": "RELEVANT",
                        "label_confidence": "HIGH",
                    }
                ]
            },
        }
    return {
        "case_id": case_id,
        "function": function,
        "sender_role": sender_role,
        "payload": {
            "bucket_id": 2,
            "candidate_messages": [_proposal_message(1)],
            "context_messages": [],
        },
        "expected": {
            "proposals": [
                {
                    "primary_message_id": 1,
                    "ptype": "TASK",
                    "assignment_mode": "OPEN",
                    "task_type": "INTERNAL",
                    "people_needed": 2,
                    "people_needed_unlimited": 0,
                    "assignees": [],
                    "description": WORK_NOT_STATED_PLACEHOLDER,
                }
            ]
        },
    }


# ---------------------------------------------------------------------------
# Loader and dataset path validation
# ---------------------------------------------------------------------------


def test_load_labelled_set_reads_valid_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    _write_jsonl(
        path,
        [
            _valid_jsonl_case(),
            _valid_jsonl_case(case_id="prop-1", function="PROPOSAL"),
        ],
    )

    cases = load_labelled_set(path)

    assert [case.case_id for case in cases] == ["rel-1", "prop-1"]
    assert cases[0].function == "RELEVANCE"
    assert cases[1].expected["proposals"][0]["ptype"] == "TASK"


def test_load_labelled_set_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    _write_jsonl(path, [_valid_jsonl_case(), _valid_jsonl_case()])

    with pytest.raises(DatasetError, match="duplicate"):
        load_labelled_set(path)


def test_load_labelled_set_rejects_unknown_function(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    _write_jsonl(path, [_valid_jsonl_case(function="CHAT")])

    with pytest.raises(DatasetError, match="function"):
        load_labelled_set(path)


def test_load_labelled_set_never_fills_a_missing_label(tmp_path: Path) -> None:
    case = _valid_jsonl_case()
    del case["expected"]["messages"][0]["decision"]
    path = tmp_path / "cases.jsonl"
    _write_jsonl(path, [case])

    with pytest.raises(DatasetError, match="decision"):
        load_labelled_set(path)


def test_load_labelled_set_rejects_a_task_without_stated_work(tmp_path: Path) -> None:
    case = _valid_jsonl_case(case_id="prop-1", function="PROPOSAL")
    case["expected"]["proposals"][0]["description"] = None
    path = tmp_path / "cases.jsonl"
    _write_jsonl(path, [case])

    with pytest.raises(DatasetError, match="TASK needs a stated description"):
        load_labelled_set(path)


def test_blank_sender_role_is_allowed_but_marks_dataset_non_production(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cases.jsonl"
    _write_jsonl(path, [_valid_jsonl_case(sender_role="")])

    cases = load_labelled_set(path)

    assert len(cases) == 1
    assert cases[0].sender_role is None
    assert production_reasons(cases) == ("blank sender role",)


def test_validate_dataset_path_refuses_a_repository_path(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    inside = repo_root / "eval" / "secret.jsonl"
    inside.parent.mkdir(parents=True)
    inside.write_text("{}", encoding="utf-8")

    with pytest.raises(DatasetPathError, match="outside the repository"):
        validate_dataset_path(inside, repo_root=repo_root)


def test_eval_set_path_from_environment_uses_the_setting(tmp_path: Path) -> None:
    external = tmp_path / "owner-set.jsonl"
    external.write_text("", encoding="utf-8")
    config = Config({DATASET_PATH_ENV: str(external)})

    assert eval_set_path_from_environment(config, repo_root=tmp_path / "repo") == external


# ---------------------------------------------------------------------------
# Owner CSV converter
# ---------------------------------------------------------------------------


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


def _csv_row(**overrides: str) -> dict[str, str]:
    row = {column: "" for column in CSV_COLUMNS}
    row.update(
        {
            "case_id": "c1",
            "conversation_id": "conv-1",
            "timestamp": "2026-01-01T10:00:00+00:00",
            "sender": "Synthetic Sender",
            "sender_role": "AH",
            "message_kind": "TEXT",
            "masked_text": "we need volunteers at the gate",
            "relevance": "RELEVANT",
            "proposal_expected": "NO",
            "label_confidence": "HIGH",
            "needs_owner_review": "N",
        }
    )
    row.update(overrides)
    return row


def test_convert_owner_csv_synthetic_shape(tmp_path: Path) -> None:
    path = tmp_path / "owner.csv"
    rows = [
        _csv_row(
            case_id="c1",
            masked_text="TASK: need two volunteers at the gate",
            proposal_expected="YES",
            proposal_type="TASK",
            assignment_mode="OPEN",
            people_needed="2",
            task_type_guess="INTERNAL",
            expected_description=WORK_NOT_STATED_PLACEHOLDER,
        ),
        _csv_row(
            case_id="c2",
            masked_text="jaldi pahucho",
            proposal_expected="NO",
        ),
        _csv_row(
            case_id="c3",
            masked_text="haha",
            sender_role="",
            relevance="IRRELEVANT",
            hard_negative_category="jokes",
            proposal_expected="NO",
            label_confidence="LOW",
            needs_owner_review="Y",
        ),
        _csv_row(
            case_id="c4",
            masked_text="excluded row",
            relevance="EXCLUDED",
            proposal_expected="NO",
        ),
        _csv_row(
            case_id="c5",
            conversation_id="conv-2",
            masked_text="announcement: gate opens at 9",
            proposal_expected="YES",
            proposal_type="ANNOUNCEMENT",
            assignment_mode="",
            task_type_guess="",
            expected_description="",
        ),
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    conversion = convert_owner_csv(path)

    assert conversion.excluded_count == 1
    relevance_cases = [case for case in conversion.cases if case.function == "RELEVANCE"]
    proposal_cases = [case for case in conversion.cases if case.function == "PROPOSAL"]
    assert len(relevance_cases) == 2
    # Movement-only row c2 is RELEVANT with proposal_expected = NO: zero proposals.
    assert len(proposal_cases) == 2
    assert production_reasons(conversion.cases) == (
        "blank sender role",
        "needs owner review",
    )

    first = relevance_cases[0]
    assert len(first.expected["messages"]) == 3
    assert [entry["decision"] for entry in first.expected["messages"]] == [
        "RELEVANT",
        "RELEVANT",
        "IRRELEVANT",
    ]
    assert first.expected["messages"][2]["hard_negative_category"] == "jokes"

    task_case = next(case for case in proposal_cases if case.case_id == "proposal:c1")
    task = task_case.expected["proposals"][0]
    assert task["ptype"] == "TASK"
    assert task["assignment_mode"] == "OPEN"
    assert task["people_needed"] == 2
    assert task["description"] == WORK_NOT_STATED_PLACEHOLDER
    assert all(
        "Synthetic Sender" not in json.dumps(value)
        for value in (task_case.payload, task_case.expected)
    )

    announcement_case = next(
        case for case in proposal_cases if case.case_id == "proposal:c5"
    )
    announcement = announcement_case.expected["proposals"][0]
    assert announcement["ptype"] == "ANNOUNCEMENT"
    assert announcement["assignment_mode"] == "NONE"
    assert announcement["description"] is None


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------


def test_score_relevance_metrics_hard_negatives_and_subsets() -> None:
    case = _rel_case(
        "rel-1",
        [
            _rel_message(1, "RELEVANT"),
            _rel_message(2, "IRRELEVANT", category="jokes"),
            _rel_message(3, "IRRELEVANT", category="status"),
            _rel_message(4, "RELEVANT", review=True),
            _rel_message(5, "IRRELEVANT", category="jokes"),
            _rel_message(6, "RELEVANT"),
        ],
    )
    responses = {
        "rel-1": _relevance_response(
            "rel-1",
            [
                (1, "RELEVANT"),
                (2, "IRRELEVANT"),
                (3, "RELEVANT"),
                (4, "IRRELEVANT"),
                (6, "RELEVANT"),
            ],
        )
    }

    score = score_relevance([case], responses)

    overall = score.overall
    assert (overall.true_positive, overall.false_positive, overall.true_negative) == (2, 1, 1)
    assert (overall.false_negative, overall.missing, overall.false_irrelevant) == (1, 1, 1)
    assert overall.precision == pytest.approx(2 / 3)
    assert overall.recall == pytest.approx(2 / 3)
    assert overall.f1 == pytest.approx(2 / 3)

    hard = {entry.category: entry for entry in score.hard_negatives}
    assert hard["jokes"].total == 2
    assert hard["jokes"].correct == 1
    assert hard["jokes"].missing == 1
    assert hard["jokes"].accuracy == pytest.approx(0.5)
    assert hard["status"].total == 1
    assert hard["status"].false_positive == 1

    assert score.high_confidence.n == 6
    assert score.needs_owner_review.n == 1
    assert score.needs_owner_review.false_negative == 1
    assert score.needs_owner_review.recall == pytest.approx(0.0)


def test_missing_prediction_is_a_miss_not_irrelevant_correct() -> None:
    case = _rel_case(
        "rel-1",
        [_rel_message(1, "RELEVANT"), _rel_message(2, "IRRELEVANT")],
    )
    responses = {"rel-1": _relevance_response("rel-1", [(1, "RELEVANT")])}

    score = score_relevance([case], responses)

    assert score.overall.true_positive == 1
    assert score.overall.true_negative == 0
    assert score.overall.missing == 1
    assert score.overall.false_negative == 0


def test_wilson_interval_matches_hand_computed_values() -> None:
    low, high = wilson_interval(95, 100)

    assert low == pytest.approx(0.888248, abs=1e-6)
    assert high == pytest.approx(0.978456, abs=1e-6)
    assert wilson_interval(0, 0) == (None, None)


# ---------------------------------------------------------------------------
# Proposal scoring
# ---------------------------------------------------------------------------


def _open_task(
    *,
    description: str | None = WORK_NOT_STATED_PLACEHOLDER,
    people_needed: int | None = 2,
) -> dict[str, Any]:
    return {
        "primary_message_id": 1,
        "ptype": "TASK",
        "assignment_mode": "OPEN",
        "task_type": "INTERNAL",
        "people_needed": people_needed,
        "people_needed_unlimited": 0,
        "assignees": [],
        "description": description,
    }


def _proposal_response(*drafts: ProposalDraft) -> ProposalResponse:
    return ProposalResponse(
        model_id="fake-model",
        model_version="1",
        prompt_version="fake-prompt",
        proposals=drafts,
    )


def test_score_proposals_matches_fields_and_ignores_description_equality() -> None:
    case = _proposal_case(
        "prop-1",
        [
            {
                "primary_message_id": 1,
                "ptype": "TASK",
                "assignment_mode": "EXPLICIT",
                "task_type": "CROSS_VERTICAL",
                "people_needed": 2,
                "people_needed_unlimited": 0,
                "assignees": ["@m2", "@m1"],
                "description": "Need volunteers",
            }
        ],
    )
    response = _proposal_response(
        ProposalDraft(
            primary_message_id=1,
            ptype="TASK",
            description="Need three volunteers at the gate",
            assignment_mode="EXPLICIT",
            proposed_assignees=("@m1", "@m2"),
            people_needed=2,
            task_type="CROSS_VERTICAL",
        )
    )

    score = score_proposals([case], {"prop-1": response})

    assert score.correct_count == 1
    fields = {entry.field: entry for entry in score.field_scores}
    assert fields["description"].correct == 1
    assert fields["assignees"].correct == 1
    assert score.false_positive_count == 0
    assert score.false_negative_count == 0


def test_score_proposals_requires_visible_placeholder_and_flags_wrong_mode() -> None:
    case = _proposal_case("prop-1", [_open_task()])
    response = _proposal_response(
        ProposalDraft(
            primary_message_id=1,
            ptype="TASK",
            description=None,
            assignment_mode="EXPLICIT",
            proposed_assignees=("@m1",),
            people_needed=2,
            task_type="INTERNAL",
        )
    )

    score = score_proposals([case], {"prop-1": response})

    assert score.correct_count == 0
    fields = {entry.field: entry for entry in score.field_scores}
    assert fields["description"].correct == 0
    assert fields["assignment_mode"].correct == 0
    assert fields["ptype"].correct == 1
    assert fields["people_needed"].correct == 1


def test_score_proposals_counts_extra_and_missing_as_errors() -> None:
    case = _proposal_case("prop-1", [_open_task()])
    extra_case = _proposal_case(
        "prop-2",
        [_open_task()],
        bucket_id=3,
        message_id=2,
    )
    response = _proposal_response(
        ProposalDraft(
            primary_message_id=1,
            ptype="TASK",
            description=WORK_NOT_STATED_PLACEHOLDER,
            assignment_mode="OPEN",
            proposed_assignees=(),
            people_needed=2,
            task_type="INTERNAL",
        ),
        ProposalDraft(
            primary_message_id=2,
            ptype="TASK",
            description=None,
            assignment_mode="OPEN",
            people_needed=1,
            task_type="INTERNAL",
        ),
    )

    score = score_proposals([case, extra_case], {"prop-1": response, "prop-2": None})

    assert score.expected_count == 2
    assert score.correct_count == 1
    assert score.false_positive_count == 1
    assert score.false_negative_count == 1
    assert score.failed_case_count == 1


# ---------------------------------------------------------------------------
# End-to-end, adapter injection and report privacy
# ---------------------------------------------------------------------------


def _end_to_end_case(case_id: str, actionable: Sequence[int]) -> LabelledCase:
    return LabelledCase(
        case_id=case_id,
        function="END_TO_END",
        sender_role="AH",
        payload={
            "bucket_id": 3,
            "candidate_messages": [_proposal_message(message_id) for message_id in actionable],
            "context_messages": [],
        },
        expected={
            "actionable_message_ids": list(actionable),
            "reviewer_days": 2,
            "review_decisions": {"1": "ACCEPTED", "2": "REJECTED"},
        },
    )


def test_score_end_to_end_recall_precision_burden_and_rejection_share() -> None:
    cases = [
        _end_to_end_case("e2e-1", [1, 2]),
        _end_to_end_case("e2e-2", [4]),
    ]
    responses = {
        "e2e-1": _proposal_response(
            ProposalDraft(1, "TASK", assignment_mode="OPEN", description="a"),
            ProposalDraft(2, "TASK", assignment_mode="OPEN", description="b"),
            ProposalDraft(3, "TASK", assignment_mode="OPEN", description="c"),
        ),
        "e2e-2": _proposal_response(
            ProposalDraft(4, "TASK", assignment_mode="OPEN", description="d"),
        ),
    }

    score = score_end_to_end(cases, responses)

    assert score.actionable_expected == 3
    assert score.actionable_matched == 3
    assert score.recall == pytest.approx(1.0)
    assert score.precision == pytest.approx(0.75)
    assert score.proposals_per_reviewer_day == pytest.approx(1.0)
    assert score.rejected_share == pytest.approx(0.5)


class _FakeAdapter:
    """Injected fake: no network, deterministic per bucket id."""

    def __init__(
        self,
        relevance: Mapping[int, RelevanceResponse],
        proposals: Mapping[int, ProposalResponse],
    ) -> None:
        self._relevance = relevance
        self._proposals = proposals
        self.calls: list[tuple[str, int]] = []

    def classify(self, request: RelevanceRequest) -> RelevanceResponse:
        self.calls.append(("classify", request.bucket_id))
        return self._relevance[request.bucket_id]

    def generate(self, request: ProposalGenerationRequest) -> ProposalResponse:
        self.calls.append(("generate", request.bucket_id))
        return self._proposals[request.bucket_id]


def test_evaluate_cases_uses_the_injected_adapter_and_report_hides_text_and_ids() -> None:
    relevance_case = _rel_case(
        "rel-secret-1",
        [_rel_message(SECRET_MESSAGE_ID, "RELEVANT", content=SECRET_TEXT)],
        sender_role="",
        bucket_id=10,
    )
    proposal_case = _proposal_case(
        "prop-1",
        [_open_task()],
        bucket_id=20,
    )
    end_to_end_case = _end_to_end_case("e2e-1", [1])
    adapter = _FakeAdapter(
        {10: _relevance_response("rel-secret-1", [(SECRET_MESSAGE_ID, "RELEVANT")])},
        {
            20: _proposal_response(
                ProposalDraft(
                    primary_message_id=1,
                    ptype="TASK",
                    description=WORK_NOT_STATED_PLACEHOLDER,
                    assignment_mode="OPEN",
                    people_needed=2,
                    task_type="INTERNAL",
                )
            ),
            3: _proposal_response(
                ProposalDraft(1, "TASK", assignment_mode="OPEN", description="a"),
            ),
        },
    )

    result = evaluate_cases(
        [relevance_case, proposal_case, end_to_end_case], adapter=adapter
    )
    report = format_report(result)

    assert ("classify", 10) in adapter.calls
    assert ("generate", 20) in adapter.calls
    assert ("generate", 3) in adapter.calls
    assert result.relevance.overall.recall == pytest.approx(1.0)
    assert result.proposals.correct_count == 1
    assert "NON-PRODUCTION" in report
    assert SECRET_TEXT not in report
    assert "rel-secret-1" not in report
    assert str(SECRET_MESSAGE_ID) not in report
    assert "precision" in report
    assert "recall" in report


def test_evaluate_cases_turns_adapter_failure_into_a_miss() -> None:
    class _FailingAdapter:
        def classify(self, request: RelevanceRequest) -> RelevanceResponse:
            del request
            raise RuntimeError("synthetic failure")

        def generate(self, request: ProposalGenerationRequest) -> ProposalResponse:
            del request
            raise RuntimeError("synthetic failure")

    case = _rel_case("rel-1", [_rel_message(1, "RELEVANT")], bucket_id=1)

    result = evaluate_cases([case], adapter=_FailingAdapter())

    assert result.relevance.failed_case_count == 1
    assert result.relevance.overall.false_negative == 1
    assert result.relevance.overall.true_negative == 0


# ---------------------------------------------------------------------------
# W5: run persistence, append-only shape and the report writer (0022)
# ---------------------------------------------------------------------------

SENTINEL = "synthetic-sentinel-content-never-store"


@pytest.fixture()
def eval_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "evaluation.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _persist_relevance(
    connection: sqlite3.Connection,
    *,
    case_id: str = "rel-1",
    sender_role: str | None = "AH",
    needs_owner_review: bool = False,
    message_review: bool = False,
    dataset_sha256: str = "e" * 64,
) -> tuple[int, dict[str, Any]]:
    case = _rel_case(
        case_id,
        [_rel_message(7, "RELEVANT", review=message_review)],
        sender_role=sender_role,
        bucket_id=10,
    )
    if needs_owner_review:
        case = LabelledCase(
            case_id=case.case_id,
            function=case.function,
            sender_role=case.sender_role,
            payload=case.payload,
            expected=case.expected,
            needs_owner_review=True,
        )
    response = _relevance_response(case_id, [(7, "RELEVANT")])
    result = evaluate_cases([case], adapter=_FakeAdapter({10: response}, {}))
    run = persist_evaluation_run(
        connection,
        function="RELEVANCE",
        model_id="model-a",
        model_version="1.0",
        prompt_version="prompt-1",
        adapter_contract_version="contract-1",
        dataset_sha256=dataset_sha256,
        cases=[case],
        responses={case_id: response},
        latencies_ms={case_id: 12.5},
        result=result,
        excluded_count=2,
        started_at="2026-01-01T10:00:00+00:00",
        finished_at="2026-01-01T10:00:01+00:00",
    )
    row = connection.execute(
        "SELECT * FROM evaluation_run WHERE id = ?", (run.run_id,)
    ).fetchone()
    assert row is not None
    return run.run_id, dict(row)


def test_persist_evaluation_run_records_metadata_and_opaque_case_result(
    eval_conn: sqlite3.Connection,
) -> None:
    case = _rel_case(
        "rel-1",
        [_rel_message(7, "RELEVANT", content=SENTINEL)],
        bucket_id=10,
    )
    response = _relevance_response("rel-1", [(7, "RELEVANT")])
    result = evaluate_cases([case], adapter=_FakeAdapter({10: response}, {}))

    run = persist_evaluation_run(
        eval_conn,
        function="RELEVANCE",
        model_id="model-a",
        model_version="1.0",
        prompt_version="prompt-1",
        adapter_contract_version="contract-1",
        dataset_sha256="e" * 64,
        cases=[case],
        responses={"rel-1": response},
        latencies_ms={"rel-1": 12.5},
        result=result,
        excluded_count=2,
        started_at="2026-01-01T10:00:00+00:00",
        finished_at="2026-01-01T10:00:01+00:00",
    )

    run_row = eval_conn.execute(
        "SELECT * FROM evaluation_run WHERE id = ?", (run.run_id,)
    ).fetchone()
    case_row = eval_conn.execute(
        "SELECT * FROM evaluation_case_result WHERE run_id = ?", (run.run_id,)
    ).fetchone()
    assert run_row is not None
    assert case_row is not None
    metrics = json.loads(str(run_row["metrics_json"]))
    assert metrics["adapter_contract_version"] == "contract-1"
    assert metrics["non_production"] is False
    assert metrics["dataset_case_counts"] == {
        "total": 1,
        "scored": 1,
        "excluded": 2,
        "by_function": {"RELEVANCE": 1},
    }
    assert metrics["latency_ms"]["count"] == 1
    assert metrics["latency_ms"]["mean_ms"] == pytest.approx(12.5)
    assert str(run_row["dataset_sha256"]) == "e" * 64
    assert int(run_row["case_count"]) == 1

    assert len(str(case_row["case_id"])) == 64
    assert str(case_row["case_id"]) != "rel-1"
    assert set(json.loads(str(case_row["expected_json"]))) == {"sha256"}
    predicted = json.loads(str(case_row["predicted_json"]))
    assert set(predicted) == {"sha256", "latency_ms"}
    assert predicted["latency_ms"] == pytest.approx(12.5)
    assert int(case_row["correct"]) == 1
    for value in tuple(run_row) + tuple(case_row):
        if isinstance(value, str):
            assert SENTINEL not in value


def test_persist_evaluation_run_marks_blank_sender_role_non_production(
    eval_conn: sqlite3.Connection,
) -> None:
    _, row = _persist_relevance(
        eval_conn, case_id="rel-blank", sender_role=""
    )

    metrics = json.loads(str(row["metrics_json"]))
    assert metrics["non_production"] is True
    assert "blank sender role" in metrics["non_production_reasons"]


def test_persist_evaluation_run_marks_owner_review_non_production(
    eval_conn: sqlite3.Connection,
) -> None:
    _, message_row = _persist_relevance(
        eval_conn, case_id="rel-message-review", message_review=True
    )
    _, case_row = _persist_relevance(
        eval_conn, case_id="rel-case-review", needs_owner_review=True
    )

    for row in (message_row, case_row):
        metrics = json.loads(str(row["metrics_json"]))
        assert metrics["non_production"] is True
        assert "needs owner review" in metrics["non_production_reasons"]


def test_run_evaluation_never_persists_message_or_label_content(
    tmp_path: Path,
) -> None:
    case = _valid_jsonl_case(case_id="case-sentinel-1", function="PROPOSAL")
    case["payload"]["candidate_messages"][0]["content"] = SENTINEL
    case["expected"]["proposals"][0]["description"] = SENTINEL
    dataset = tmp_path / "cases.jsonl"
    _write_jsonl(dataset, [case])
    reports = tmp_path / "reports"
    db_path = tmp_path / "evaluation.db"
    connection = connect(db_path)
    try:
        apply_migrations(connection)
        adapter = _FakeAdapter(
            {},
            {
                2: _proposal_response(
                    ProposalDraft(
                        1,
                        "TASK",
                        assignment_mode="OPEN",
                        people_needed=2,
                        task_type="INTERNAL",
                        description="safe prediction",
                    )
                )
            },
        )
        run = run_evaluation(
            connection,
            dataset_path=dataset,
            adapter=adapter,
            output_dir=reports,
            model_id="model-a",
            model_version="1.0",
            prompt_version="prompt-1",
            adapter_contract_version="contract-1",
        )
        assert run.dataset_sha256 == hashlib.sha256(dataset.read_bytes()).hexdigest()
        run_row = connection.execute(
            "SELECT * FROM evaluation_run WHERE id = ?", (run.run_id,)
        ).fetchone()
        case_row = connection.execute(
            "SELECT * FROM evaluation_case_result WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
        assert run_row is not None
        assert case_row is not None
        assert len(str(case_row["case_id"])) == 64
        assert str(case_row["case_id"]) != "case-sentinel-1"
        assert SENTINEL not in str(run_row["metrics_json"])
        assert SENTINEL not in str(case_row["expected_json"])
        assert SENTINEL not in str(case_row["predicted_json"])
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()

    assert SENTINEL.encode() not in db_path.read_bytes()
    for sidecar in tmp_path.glob("evaluation.db-*"):
        assert SENTINEL.encode() not in sidecar.read_bytes()
    report_files = sorted(reports.glob("*.txt"))
    assert report_files
    for report in report_files:
        assert SENTINEL not in report.read_text(encoding="utf-8")


def test_persisted_evaluation_runs_are_append_only(
    eval_conn: sqlite3.Connection,
) -> None:
    first_id, first_row = _persist_relevance(eval_conn, case_id="rel-1")
    first_cases = [
        dict(row)
        for row in eval_conn.execute(
            "SELECT * FROM evaluation_case_result WHERE run_id = ? ORDER BY id",
            (first_id,),
        ).fetchall()
    ]

    second_id, _ = _persist_relevance(eval_conn, case_id="rel-1")

    assert second_id != first_id
    old_row = eval_conn.execute(
        "SELECT * FROM evaluation_run WHERE id = ?", (first_id,)
    ).fetchone()
    assert old_row is not None
    assert dict(old_row) == first_row
    second_cases = [
        dict(row)
        for row in eval_conn.execute(
            "SELECT * FROM evaluation_case_result WHERE run_id = ? ORDER BY id",
            (second_id,),
        ).fetchall()
    ]
    assert len(first_cases) == len(second_cases) == 1
    assert first_cases[0]["case_id"] != second_cases[0]["case_id"]
    assert not [
        name
        for name in dir(evaluation_module)
        if name.startswith(("update_", "delete_"))
    ]


def test_persist_evaluation_run_rejects_duplicate_case_ids(
    eval_conn: sqlite3.Connection,
) -> None:
    case = _rel_case("dup", [_rel_message(7, "RELEVANT")], bucket_id=10)
    response = _relevance_response("dup", [(7, "RELEVANT")])
    result = evaluate_cases(
        [case, case], adapter=_FakeAdapter({10: response}, {})
    )

    with pytest.raises(EvaluationError, match="duplicate case id"):
        persist_evaluation_run(
            eval_conn,
            function="RELEVANCE",
            model_id="model-a",
            model_version="1.0",
            prompt_version="prompt-1",
            adapter_contract_version="contract-1",
            dataset_sha256="e" * 64,
            cases=[case, case],
            responses={"dup": response},
            latencies_ms={"dup": 1.0},
            result=result,
        )


def test_run_evaluation_rejects_a_mixed_function_dataset(tmp_path: Path) -> None:
    dataset = tmp_path / "mixed.jsonl"
    _write_jsonl(
        dataset,
        [
            _valid_jsonl_case(case_id="rel-1", function="RELEVANCE"),
            _valid_jsonl_case(case_id="prop-1", function="PROPOSAL"),
        ],
    )
    connection = connect(":memory:")
    try:
        apply_migrations(connection)
        with pytest.raises(EvaluationError, match="exactly one function"):
            run_evaluation(
                connection,
                dataset_path=dataset,
                adapter=_FakeAdapter({}, {}),
                output_dir=tmp_path / "out",
                model_id="model-a",
                model_version="1.0",
                prompt_version="prompt-1",
                adapter_contract_version="contract-1",
            )
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# W6: correction rate per model version, review burden and exported table
# ---------------------------------------------------------------------------


MODEL_A = ModelVersionIdentity("model-a", "1.0", "prompt-1")
MODEL_B = ModelVersionIdentity("model-b", "2.0", "prompt-2")


def _eval_proposal(
    proposal_id: int,
    *,
    decision: str | None = None,
    edited: bool = False,
    decided_by: int | None = 1,
    decided_at: str | None = "2026-01-01T10:00:00+00:00",
) -> EvalProposal:
    revisions: tuple[EvalRevision, ...] = ()
    delta: tuple[EvalFieldDelta, ...] = ()
    if edited:
        revisions = (
            EvalRevision(
                revision_number=1,
                person_id=decided_by or 1,
                changed_fields=("description",),
                changed_at="2026-01-01T09:00:00+00:00",
            ),
        )
        delta = (
            EvalFieldDelta(
                field="description",
                generated="generated line",
                final="edited line",
            ),
        )
    state = "PENDING"
    if decision == "ACCEPTED":
        state = "ACCEPTED"
    elif decision == "REJECTED":
        state = "REJECTED"
    elif edited:
        state = "EDITED"
    return EvalProposal(
        proposal_id=proposal_id,
        ptype="TASK",
        review_state=state,
        decision=decision,
        decided_by=decided_by if decision is not None else None,
        decided_at=decided_at if decision is not None else None,
        note=None,
        revisions=revisions,
        generated_final_delta=delta,
    )


def test_edited_proposal_counts_as_a_correction() -> None:
    table = correction_table([_eval_proposal(1, edited=True)], {1: MODEL_A})

    (row,) = table.rows
    assert row.reviewed == 1
    assert row.edited == 1
    assert row.accepted_unchanged == 0
    assert row.rejected == 0
    assert row.correction_rate == pytest.approx(1.0)


def test_accepted_unchanged_proposal_is_not_a_correction() -> None:
    table = correction_table(
        [_eval_proposal(1, decision="ACCEPTED")], {1: MODEL_A}
    )

    (row,) = table.rows
    assert row.edited == 0
    assert row.accepted_unchanged == 1
    assert row.rejected == 0
    assert row.correction_rate == pytest.approx(0.0)


def test_rejected_proposal_is_counted_separately() -> None:
    table = correction_table(
        [_eval_proposal(1, decision="REJECTED")], {1: MODEL_A}
    )

    (row,) = table.rows
    assert row.edited == 0
    assert row.rejected == 1
    assert row.rejected_share == pytest.approx(1.0)
    assert row.correction_rate == pytest.approx(0.0)


def test_correction_table_groups_by_model_and_prompt_version() -> None:
    proposals = [
        _eval_proposal(1, edited=True),
        _eval_proposal(2, decision="ACCEPTED"),
        _eval_proposal(3, decision="REJECTED"),
    ]
    provenance = {1: MODEL_A, 2: MODEL_A, 3: MODEL_B}

    table = correction_table(proposals, provenance)

    assert [
        (row.model_id, row.model_version, row.prompt_version)
        for row in table.rows
    ] == [
        ("model-a", "1.0", "prompt-1"),
        ("model-b", "2.0", "prompt-2"),
    ]
    row_a, row_b = table.rows
    assert (
        row_a.reviewed,
        row_a.edited,
        row_a.accepted_unchanged,
        row_a.rejected,
    ) == (2, 1, 1, 0)
    assert row_a.correction_rate == pytest.approx(0.5)
    assert (row_b.reviewed, row_b.edited, row_b.rejected) == (1, 0, 1)
    assert row_b.correction_rate == pytest.approx(0.0)


def test_review_burden_counts_proposals_per_reviewer_day() -> None:
    proposals = [
        _eval_proposal(
            1,
            decision="ACCEPTED",
            decided_by=1,
            decided_at="2026-01-01T10:00:00+00:00",
        ),
        _eval_proposal(
            2,
            decision="ACCEPTED",
            decided_by=1,
            decided_at="2026-01-01T18:00:00+00:00",
        ),
        _eval_proposal(
            3,
            decision="REJECTED",
            decided_by=2,
            decided_at="2026-01-02T09:00:00+00:00",
        ),
    ]

    burden = review_burden(proposals)

    assert burden.reviewed == 3
    assert burden.reviewer_days == 2
    assert burden.proposals_per_reviewer_day == pytest.approx(1.5)
    assert burden.rejected == 1
    assert burden.rejected_share == pytest.approx(1 / 3)


def test_correction_table_empty_input_has_no_division_by_zero() -> None:
    table = correction_table([], {})

    assert table.rows == ()
    assert table.burden.reviewed == 0
    assert table.burden.reviewer_days == 0
    assert table.burden.proposals_per_reviewer_day is None
    assert table.burden.rejected == 0
    assert table.burden.rejected_share is None


def test_format_correction_table_reports_counts_only() -> None:
    table = correction_table(
        [_eval_proposal(101, edited=True)], {101: MODEL_A}
    )

    text = format_correction_table(table)

    assert "model-a" in text
    assert "correction_rate" in text
    assert "review_burden" in text
    assert "101" not in text


def test_write_correction_table_writes_the_same_exported_table(
    tmp_path: Path,
) -> None:
    table = correction_table(
        [_eval_proposal(1, decision="REJECTED")], {1: MODEL_A}
    )
    path = tmp_path / "correction.txt"

    write_correction_table(table, path)

    assert path.read_text(encoding="utf-8") == format_correction_table(table)


def _seed_review_world(connection: sqlite3.Connection) -> None:
    connection.execute("INSERT INTO vertical (id, name) VALUES (1, 'PR')")
    connection.execute(
        "INSERT INTO source_group (id, discord_channel_id, vertical_id)"
        " VALUES (1, 'chan-1', 1)"
    )
    connection.execute(
        "INSERT INTO person (id, name, phone, role, active, roll_number)"
        " VALUES (5, 'Admin', '+910000000005', 'Admin', 1, 'ROLL-005')"
    )
    connection.execute(
        "INSERT INTO person_vertical (person_id, vertical_id) VALUES (5, 1)"
    )
    connection.execute(
        "INSERT INTO bucket (id, group_id, start_event_timestamp,"
        " end_event_timestamp, parameters_used, processing_state)"
        " VALUES (1, 1, '2026-01-01T10:00:00+00:00',"
        " '2026-01-01T10:00:10+00:00', '{}', 'PROCESSED')"
    )
    connection.execute(
        "INSERT INTO processing_attempt (id, stage, bucket_id, started_at,"
        " finished_at, outcome, model_id, model_version, config_version)"
        " VALUES (1, 'proposal_generation', 1, '2026-01-01T10:00:00+00:00',"
        " '2026-01-01T10:00:01+00:00', 'SUCCEEDED', 'model-a', '1.0',"
        " 'prompt-1')"
    )
    connection.execute(
        "INSERT INTO source_message (id, source, source_message_id, group_id,"
        " sender_person_id, event_timestamp, received_at, content)"
        " VALUES (1, 'discord', 'm1', 1, 5, '2026-01-01T10:00:02+00:00',"
        " '2026-01-01T10:00:02+00:00', 'synthetic line')"
    )
    connection.execute(
        "INSERT INTO bucket_message (bucket_id, message_id, group_id)"
        " VALUES (1, 1, 1)"
    )


def _insert_review_proposal(
    connection: sqlite3.Connection,
    proposal_id: int,
    *,
    model_id: str,
    model_version: str,
    prompt_version: str,
    description: str,
    review_state: str,
) -> None:
    connection.execute(
        "INSERT INTO proposal (id, attempt_id, bucket_id, primary_message_id,"
        " ptype, description, assignment_mode, proposed_assignees_text,"
        " people_needed, people_needed_unlimited, task_type, source_vertical_id,"
        " suggested_responsible_vertical_id, structurally_valid, generated_at,"
        " model_id, model_version, prompt_version, review_state)"
        " VALUES (?, 1, 1, 1, 'TASK', ?, 'OPEN', NULL, NULL, 0, 'INTERNAL', 1,"
        " NULL, 1, '2026-01-01T10:00:00+00:00', ?, ?, ?, ?)",
        (
            proposal_id,
            description,
            model_id,
            model_version,
            prompt_version,
            review_state,
        ),
    )


def _insert_review_revision(
    connection: sqlite3.Connection, proposal_id: int
) -> None:
    connection.execute(
        "INSERT INTO proposal_revision (proposal_id, revision_number, person_id,"
        " changed_fields, changed_at, description, assignment_mode,"
        " proposed_assignees_text, people_needed, people_needed_unlimited,"
        " task_type, suggested_responsible_vertical_id)"
        " VALUES (?, 1, 5, ?, '2026-01-01T09:00:00+00:00', 'edited line',"
        " 'OPEN', NULL, NULL, 0, 'INTERNAL', NULL)",
        (proposal_id, json.dumps(["description"])),
    )


def _insert_review_decision(
    connection: sqlite3.Connection,
    proposal_id: int,
    decision: str,
    *,
    decided_at: str = "2026-01-01T11:00:00+00:00",
) -> None:
    connection.execute(
        "INSERT INTO proposal_decision (proposal_id, decision, decided_by,"
        " decided_at, note) VALUES (?, ?, 5, ?, NULL)",
        (proposal_id, decision, decided_at),
    )


def test_export_correction_table_reads_existing_reviewed_proposals(
    tmp_path: Path,
) -> None:
    connection = connect(tmp_path / "review.db")
    try:
        apply_migrations(connection)
        _seed_review_world(connection)
        _insert_review_proposal(
            connection,
            1,
            model_id="model-a",
            model_version="1.0",
            prompt_version="prompt-1",
            description="generated line",
            review_state="PENDING",
        )
        _insert_review_revision(connection, 1)
        _insert_review_proposal(
            connection,
            2,
            model_id="model-a",
            model_version="1.0",
            prompt_version="prompt-1",
            description="generated rejected a",
            review_state="REJECTED",
        )
        _insert_review_decision(connection, 2, "REJECTED")
        _insert_review_proposal(
            connection,
            3,
            model_id="model-b",
            model_version="2.0",
            prompt_version="prompt-2",
            description="generated rejected b",
            review_state="REJECTED",
        )
        _insert_review_decision(
            connection,
            3,
            "REJECTED",
            decided_at="2026-01-02T10:00:00+00:00",
        )

        table = export_correction_table(connection, actor_person_id=5)

        assert [
            (row.model_id, row.model_version, row.prompt_version)
            for row in table.rows
        ] == [
            ("model-a", "1.0", "prompt-1"),
            ("model-b", "2.0", "prompt-2"),
        ]
        row_a, row_b = table.rows
        assert (row_a.reviewed, row_a.edited, row_a.rejected) == (2, 1, 1)
        assert row_a.correction_rate == pytest.approx(0.5)
        assert (row_b.reviewed, row_b.edited, row_b.rejected) == (1, 0, 1)
        assert table.burden.reviewed == 2
        assert table.burden.reviewer_days == 2
        assert table.burden.proposals_per_reviewer_day == pytest.approx(1.0)
        assert table.burden.rejected == 2
        assert table.burden.rejected_share == pytest.approx(1.0)
    finally:
        connection.close()
