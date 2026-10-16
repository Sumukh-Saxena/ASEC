"""Phase 0 smoke tests: model-free import and the version entrypoint."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from backend import __version__
from backend.__main__ import main

REPO_ROOT = Path(__file__).resolve().parents[1]

_FORBIDDEN = ("discord", "torch", "transformers", "aiohttp", "requests", "httpx")


def test_package_imports_without_ai_or_network_dependencies() -> None:
    code = (
        "import sys; import backend; "
        f"bad = [m for m in {_FORBIDDEN!r} if m in sys.modules]; "
        "assert not bad, bad"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_version_flag_prints_version_and_exits_zero(capsys) -> None:
    return_code = main(["--version"])
    captured = capsys.readouterr()
    assert return_code == 0
    assert __version__ in captured.out


def test_unknown_argument_exits_nonzero(capsys) -> None:
    return_code = main(["--bogus"])
    capsys.readouterr()
    assert return_code != 0
