"""The optional extra stays optional and the dependency arrows point one way.

O12-1: ``fastapi`` and ``uvicorn`` live in one optional ``api`` extra and are
imported only under ``backend/api``. No domain module may import the transport
package, and importing ``backend.api`` must not import the framework: a clean
install without the extra keeps every non-API test green.

The checks are static (``ast``, ``tomllib``) plus one subprocess that blocks the
framework from ``sys.meta_path`` and sees what a missing extra really does.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
API_PACKAGE = BACKEND / "api"
_FRAMEWORK_ROOTS = {"fastapi", "starlette", "uvicorn", "pydantic", "httpx"}


def _backend_modules() -> list[Path]:
    return sorted(path for path in BACKEND.rglob("*.py") if "__pycache__" not in path.parts)


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
    return found


def _all_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
    return found


def _root(name: str) -> str:
    return name.split(".")[0]


def test_the_framework_is_imported_only_inside_the_api_package() -> None:
    """No domain module - at any nesting level - touches the framework."""
    offenders: dict[str, set[str]] = {}
    for path in _backend_modules():
        if API_PACKAGE in path.parents:
            continue
        hit = {name for name in _all_imports(path) if _root(name) in _FRAMEWORK_ROOTS}
        if hit:
            offenders[str(path.relative_to(ROOT))] = hit
    assert offenders == {}


def test_no_module_imports_the_transport_package_at_module_scope() -> None:
    """``backend.api`` is imported lazily by the composition root, not by peers."""
    offenders: dict[str, set[str]] = {}
    for path in _backend_modules():
        if API_PACKAGE in path.parents:
            continue
        hit = {
            name
            for name in _top_level_imports(path)
            if name == "backend.api" or name.startswith("backend.api.")
        }
        if hit:
            offenders[str(path.relative_to(ROOT))] = hit
    assert offenders == {}


def test_no_literal_framework_import_outside_the_api_package() -> None:
    """A second, textual check: catches a literal an AST walk would miss."""
    literals = (
        "import fastapi",
        "from fastapi",
        "import starlette",
        "from starlette",
        "import uvicorn",
        "from uvicorn",
        "import pydantic",
        "from pydantic",
    )
    offenders: dict[str, list[str]] = {}
    for path in _backend_modules():
        if API_PACKAGE in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        hit = [literal for literal in literals if literal in text]
        if hit:
            offenders[str(path.relative_to(ROOT))] = hit
    assert offenders == {}


def test_the_api_extra_declares_exactly_the_two_owner_pins() -> None:
    """One extra, two pins, and a core install that pulls nothing."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = project["project"]["optional-dependencies"]
    assert project["project"]["dependencies"] == []
    assert extras["api"] == ["fastapi==0.162.0", "uvicorn==0.54.0"]
    assert set(extras) == {"dev", "api"}
    assert "httpx" not in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_ci_installs_the_api_extra() -> None:
    """CI runs the API tests, so it installs the extra they need."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'pip install -e ".[dev,api]"' in workflow


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_importing_the_api_package_does_not_import_the_framework() -> None:
    """The package import is lazy even when the extra is installed."""
    result = _run(
        "import sys\n"
        "import backend.api\n"
        "print('fastapi' in sys.modules, 'uvicorn' in sys.modules)\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["False", "False", "ok"]


def test_a_missing_extra_raises_a_configuration_error_with_the_install_command() -> None:
    """A blocked framework import is a clear, actionable error, not a traceback."""
    code = (
        "import sys\n"
        "BLOCKED = {'fastapi', 'starlette', 'uvicorn', 'pydantic'}\n"
        "class Blocker:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in BLOCKED:\n"
        "            raise ImportError('blocked by test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "import backend.api\n"
        "print('import-ok')\n"
        "from backend.config import Config\n"
        "try:\n"
        "    backend.api.create_app(\n"
        "        connection_factory=lambda: None,\n"
        "        now=lambda: '2026-11-13T09:00:00+00:00',\n"
        "        config=Config({}),\n"
        "    )\n"
        "except backend.api.ApiConfigurationError as error:\n"
        "    print('config-error')\n"
        "    print('install-command' if '.[dev,api]' in str(error) else 'no-command')\n"
        "    print('chained' if isinstance(error.__cause__, ImportError) else 'no-chain')\n"
        "else:\n"
        "    print('no-error')\n"
    )
    result = _run(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        "import-ok",
        "config-error",
        "install-command",
        "chained",
    ]
