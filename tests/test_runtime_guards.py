"""W9 H3: runtime guards replace every production ``assert``.

Python removes ``assert`` statements under ``-O``. These guards must still
raise the module's existing domain error, so an optimised run can never skip a
capacity, resolution or identity check. Tests stay in-process except one
subprocess run under ``python -O``.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from backend.db.connection import connect
from backend.review import (
    AssigneeSelection,
    InvalidEditError,
    ReviewError,
    _manual_assignee,
    _require_assignee_person,
    _require_person_record,
    _require_responsible_vertical,
)
from backend.tasks import TaskStateError, _require_count_row

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_manual_assignee_requires_a_selected_person() -> None:
    connection = connect(":memory:")
    try:
        with pytest.raises(InvalidEditError):
            _manual_assignee(connection, AssigneeSelection(text="someone", person_id=None))
    finally:
        connection.close()


def test_responsible_vertical_guard_raises_review_error() -> None:
    with pytest.raises(ReviewError):
        _require_responsible_vertical(None)
    assert _require_responsible_vertical(7) == 7


def test_assignee_person_guard_raises_review_error() -> None:
    with pytest.raises(ReviewError):
        _require_assignee_person(None)
    assert _require_assignee_person(11) == 11


def test_actor_record_guard_raises_review_error() -> None:
    with pytest.raises(ReviewError):
        _require_person_record(None)


def test_count_row_guard_raises_task_state_error() -> None:
    with pytest.raises(TaskStateError):
        _require_count_row(None)


def test_no_production_assert_statements_remain() -> None:
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "backend").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(isinstance(node, ast.Assert) for node in ast.walk(tree)):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []


def test_guard_still_raises_under_python_optimised() -> None:
    script = (
        "from backend.tasks import TaskStateError, _require_count_row\n"
        "try:\n"
        "    _require_count_row(None)\n"
        "except TaskStateError:\n"
        "    print('TASK_STATE_ERROR')\n"
        "else:\n"
        "    raise SystemExit(3)\n"
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "TASK_STATE_ERROR" in result.stdout
