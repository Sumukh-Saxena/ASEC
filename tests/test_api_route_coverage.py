"""Every registered route must have one matrix classification (W3).

FR-AUT-01/02 and plan §4.4: a route without a row in the operation table is an
unclassified surface, so this test fails rather than allowing a default-allow
route to ship. The table itself is in ``backend/api/matrix.py``; this test only
compares names, never imports the services.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend.api.matrix import OPERATIONS, ROUTE_OPERATIONS  # noqa: E402
from tests.api_fixtures import ApiHarness, build_harness  # noqa: E402

_HTTP_METHODS = frozenset({"GET", "POST", "PATCH", "PUT", "DELETE"})


def _registered_routes(app: object) -> set[tuple[str, str]]:
    """Every (method, path) the app itself describes in its schema."""
    schema = app.openapi()  # type: ignore[attr-defined]
    return {
        (method.upper(), str(path))
        for path, operations in schema.get("paths", {}).items()
        for method in operations
        if method.upper() in _HTTP_METHODS
    }


@pytest.fixture()
def harness(tmp_path: Path) -> ApiHarness:
    return build_harness(tmp_path)


def test_every_registered_route_has_a_matrix_row(harness: ApiHarness) -> None:
    registered = _registered_routes(harness.app)
    assert registered, "the app exposed no routes at all"
    missing = sorted(registered - set(ROUTE_OPERATIONS))
    assert missing == []


def test_every_route_mapping_names_a_known_operation() -> None:
    unknown = {
        route: operation
        for route, operation in ROUTE_OPERATIONS.items()
        if operation not in OPERATIONS
    }
    assert unknown == {}


def test_every_operation_row_is_complete_and_uses_stable_reasons() -> None:
    allowed_reasons = {
        "NOT_ROLE",
        "NOT_OWNER",
        "NOT_VERTICAL",
        "INACTIVE",
        "NOT_RECIPIENT",
    }
    for name, operation in OPERATIONS.items():
        assert operation.name == name
        assert operation.scope.strip() != ""
        assert operation.subject_type.strip() != ""
        if operation.public:
            assert operation.allowed_roles == frozenset()
        else:
            assert operation.allowed_roles, name
            assert operation.allowed_roles <= {
                "Admin",
                "Head",
                "AH",
                "Volunteer",
            }
            assert operation.denial_reason in allowed_reasons, name


def test_no_route_is_mapped_twice_to_the_same_operation() -> None:
    seen = list(ROUTE_OPERATIONS.values())
    duplicates = {name for name in seen if seen.count(name) > 1}
    assert duplicates == set()
