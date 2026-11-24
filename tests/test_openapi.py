"""The additive ``python -m backend openapi [--check]`` developer command (E2).

The document is generated from the real app factory with dummy settings, so it
pins the actual route table without a database, a network call, a secret or a
bound port. The checked-in ``docs/api/openapi.json`` is deterministic: sorted
keys, two-space indent, LF and one trailing newline.

The default 422 ``HTTPValidationError``/``ValidationError`` models are kept as
FastAPI generates them; the app's own 422 body is the fixed ``ErrorResponse``.
That cosmetic mismatch is recorded as a carry-forward, not silently removed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend import __main__ as cli  # noqa: E402
from backend.api.errors import ERROR_MESSAGES  # noqa: E402
from backend.api.matrix import ROUTE_OPERATIONS  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DOCUMENT = REPO / "docs" / "api" / "openapi.json"


def _text(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def test_openapi_writes_a_deterministic_document(tmp_path: Path) -> None:
    """(a) Two builds are byte-identical, sorted, LF-only, one trailing newline."""
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    assert cli._run_openapi([], path=first) == 0
    assert cli._run_openapi([], path=second) == 0

    text = _text(first)
    assert text == _text(second)
    assert "\r" not in text
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert text == cli._render_openapi(json.loads(text))
    assert json.loads(text)["info"]["title"] == "ASEC API"


def test_openapi_check_accepts_the_committed_document() -> None:
    """(b) The checked-in document is current and ``--check`` writes nothing."""
    assert DOCUMENT.is_file()
    before = DOCUMENT.read_bytes()
    assert cli._run_openapi(["--check"]) == 0
    assert DOCUMENT.read_bytes() == before


def test_openapi_check_reports_drift_without_writing(tmp_path: Path) -> None:
    """(c) Drift and a missing document both exit non-zero and never write."""
    drifted = tmp_path / "drifted.json"
    drifted.write_text(_text(DOCUMENT) + " ", encoding="utf-8", newline="")
    before = drifted.read_bytes()

    assert cli._run_openapi(["--check"], path=drifted) == 1
    assert drifted.read_bytes() == before

    missing = tmp_path / "missing.json"
    assert cli._run_openapi(["--check"], path=missing) == 1
    assert not missing.exists()

    assert cli._run_openapi(["--unknown"], path=drifted) == 2


def test_openapi_paths_are_the_real_route_table() -> None:
    """(d) Every registered route appears once; the extensions are documented."""
    spec = cli._build_openapi_spec()
    actual = {
        (method.lower(), path)
        for path, operations in spec["paths"].items()
        for method in operations
        if method in {"get", "post", "patch", "delete", "put"}
    }
    expected = {(method.lower(), path) for method, path in ROUTE_OPERATIONS}
    assert actual == expected

    components = spec["components"]
    assert components["parameters"]["IdempotencyKey"]["name"] == "Idempotency-Key"
    assert "ErrorResponse" in components["schemas"]
    assert set(spec["x-asec-error-codes"]) == set(ERROR_MESSAGES)
    assert spec["x-asec-idempotency-key"]["component"] == (
        "#/components/parameters/IdempotencyKey"
    )


def test_openapi_build_opens_no_database_and_reads_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(e) The dummy factory is never called and no environment value leaks."""
    called: list[bool] = []

    def refuse() -> object:
        called.append(True)
        raise AssertionError("the document build opened a database")

    monkeypatch.setattr(cli, "_openapi_connection_factory", refuse)
    monkeypatch.setenv("ASEC_DB_PATH", "SENTINEL-DB-PATH")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "SENTINEL-BOT-TOKEN")

    text = cli._render_openapi(cli._build_openapi_spec())

    assert called == []
    assert "SENTINEL-DB-PATH" not in text
    assert "SENTINEL-BOT-TOKEN" not in text
