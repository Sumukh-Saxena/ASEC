"""``python -m backend serve-api``: the composition root for the API (W2).

The command opens no socket itself: it builds the app from configuration and
hands it to the injected runner. That makes every refusal testable without
binding a port, and keeps the loopback-only rule (O12-6) in one place.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from backend import __version__  # noqa: E402
from backend.__main__ import main  # noqa: E402
from backend.config import Config  # noqa: E402
from tests.api_fixtures import VOLUNTEER_ROLL, call, seed_database  # noqa: E402


class _Recorder:
    """The injected server runner: records the call and returns immediately."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, str, int]] = []

    def __call__(self, app: Any, host: str, port: int) -> None:
        self.calls.append((app, host, port))


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "serve.db"
    seed_database(path)
    return path


def _config(database: Path, **extra: str) -> Config:
    values = {"ASEC_DB_PATH": str(database)}
    values.update(extra)
    return Config(values)


def _run(
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    config: Config,
    recorder: _Recorder,
) -> tuple[int, str, str]:
    code = main(args, config=config, serve_api=recorder)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_serve_api_builds_the_app_and_hands_it_to_the_runner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default bind is loopback, and the runner gets the built app."""
    recorder = _Recorder()
    code, out, _err = _run(
        capsys, ["serve-api", "--port", "8123"], _config(_database(tmp_path)), recorder
    )

    assert code == 0
    assert len(recorder.calls) == 1
    app, host, port = recorder.calls[0]
    assert (host, port) == ("127.0.0.1", 8123)
    assert "serving API on 127.0.0.1:8123" in out
    assert call(app, "GET", "/healthz").json() == {"status": "ok"}


def test_bind_address_and_port_come_from_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No literal is hard-coded in the command: configuration wins."""
    recorder = _Recorder()
    config = _config(
        _database(tmp_path),
        API_HOST="127.0.0.1",
        API_PORT="9001",
    )
    code, out, _err = _run(capsys, ["serve-api"], config, recorder)

    assert code == 0
    assert recorder.calls[0][1:] == ("127.0.0.1", 9001)
    assert "9001" in out


def test_a_command_line_bind_overrides_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = _Recorder()
    config = _config(_database(tmp_path), API_PORT="9001")
    code, _out, _err = _run(
        capsys, ["serve-api", "--host", "127.0.0.1", "--port", "9200"], config, recorder
    )
    assert code == 0
    assert recorder.calls[0][1:] == ("127.0.0.1", 9200)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "example.invalid", "::"])
def test_a_non_loopback_bind_is_refused_without_starting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], host: str
) -> None:
    """O12-6: a non-loopback address needs the explicit opt-in flag."""
    recorder = _Recorder()
    code, _out, err = _run(
        capsys, ["serve-api", "--host", host], _config(_database(tmp_path)), recorder
    )

    assert code == 2
    assert recorder.calls == []
    assert "API_ALLOW_NON_LOOPBACK" in err


def test_a_non_loopback_bind_is_allowed_when_configured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The opt-in is configuration, so it is visible and auditable."""
    recorder = _Recorder()
    config = _config(_database(tmp_path), API_ALLOW_NON_LOOPBACK="true")
    code, _out, _err = _run(
        capsys, ["serve-api", "--host", "0.0.0.0", "--port", "8100"], config, recorder
    )
    assert code == 0
    assert recorder.calls[0][1:] == ("0.0.0.0", 8100)


def test_a_missing_database_path_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    recorder = _Recorder()
    code, _out, err = _run(capsys, ["serve-api"], Config({}), recorder)
    assert code == 2
    assert recorder.calls == []
    assert "ASEC_DB_PATH" in err


def test_a_missing_database_file_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = _Recorder()
    config = _config(tmp_path / "absent.db")
    code, _out, err = _run(capsys, ["serve-api"], config, recorder)
    assert code == 2
    assert recorder.calls == []
    assert "migrations" in err
    assert not (tmp_path / "absent.db").exists()


@pytest.mark.parametrize("port", ["0", "70000", "abc", ""])
def test_an_invalid_port_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], port: str
) -> None:
    recorder = _Recorder()
    code, _out, err = _run(
        capsys, ["serve-api", "--port", port], _config(_database(tmp_path)), recorder
    )
    assert code == 2
    assert recorder.calls == []
    assert "usage: python -m backend serve-api" in err


@pytest.mark.parametrize("option", ["--host", "--port", "--bogus", "extra"])
def test_an_unknown_or_incomplete_option_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], option: str
) -> None:
    recorder = _Recorder()
    code, _out, err = _run(
        capsys, ["serve-api", option], _config(_database(tmp_path)), recorder
    )
    assert code == 2
    assert recorder.calls == []
    assert "usage: python -m backend serve-api" in err


def test_an_invalid_boolean_flag_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = _Recorder()
    config = _config(_database(tmp_path), API_ALLOW_NON_LOOPBACK="maybe")
    code, _out, err = _run(capsys, ["serve-api"], config, recorder)
    assert code == 2
    assert recorder.calls == []
    assert "API_ALLOW_NON_LOOPBACK" in err


def test_without_a_bot_token_sign_in_still_answers_and_records_the_gap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No sender is configured, so the DM cannot be sent - but nothing leaks."""
    database = _database(tmp_path)
    recorder = _Recorder()
    code, _out, err = _run(capsys, ["serve-api"], _config(database), recorder)

    assert code == 0
    assert "no OTP sender configured" in err
    app = recorder.calls[0][0]
    response = call(app, "POST", "/auth/otp", json_body={"roll_number": VOLUNTEER_ROLL})
    assert response.status == 202
    assert response.json() == {"status": "accepted"}

    connection = sqlite3.connect(database)
    try:
        reasons = [
            str(row[0])
            for row in connection.execute(
                "SELECT reason FROM auth_attempt WHERE kind = 'OTP_DELIVERY'"
            )
        ]
    finally:
        connection.close()
    assert reasons == ["SENDER_UNCONFIGURED"]


def test_version_still_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_usage_text_mentions_serve_api_after_the_new_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The no-argument usage line stays honest about what exists."""
    assert main([]) == 2
    assert "serve-api" in capsys.readouterr().err
