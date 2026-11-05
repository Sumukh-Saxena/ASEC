"""Tests for the Phase 0 configuration loader (no AI, no Discord, no DB)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from backend.config import (
    DEFAULT_PROPOSAL_MAX_CANDIDATES_PER_CALL,
    DEFAULT_RELEVANCE_MAX_MESSAGES_PER_CALL,
    ConfigError,
    PartSettings,
    is_secret_key,
    load_config,
    load_part_settings,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_TOKEN_LIKE = re.compile(r"[A-Za-z0-9_\-]{24,}")


def test_load_config_reads_values_from_environment() -> None:
    config = load_config({"APP_ENV": "test", "LOG_LEVEL": "DEBUG"})
    assert config.get("APP_ENV") == "test"
    assert config.get("LOG_LEVEL") == "DEBUG"


def test_get_returns_default_when_absent() -> None:
    config = load_config({})
    assert config.get("MISSING", "fallback") == "fallback"
    assert config.get("MISSING") is None


def test_require_returns_present_value() -> None:
    config = load_config({"REQUIRED_THING": "value"})
    assert config.require("REQUIRED_THING") == "value"


def test_require_raises_when_missing() -> None:
    config = load_config({})
    with pytest.raises(ConfigError):
        config.require("REQUIRED_THING")


def test_require_raises_when_blank() -> None:
    config = load_config({"REQUIRED_THING": "   "})
    with pytest.raises(ConfigError):
        config.require("REQUIRED_THING")


def test_as_public_dict_exposes_key_names_only() -> None:
    config = load_config({"APP_ENV": "dev", "LOG_LEVEL": "DEBUG"})
    public = config.as_public_dict()
    assert set(public) == {"APP_ENV", "LOG_LEVEL"}
    assert "dev" not in public.values()
    assert "DEBUG" not in public.values()


def test_public_output_hides_values_regardless_of_name_heuristic() -> None:
    database_url = "postgres://user:secretpw@host/db"
    webhook = "https://discord.com/api/webhooks/123/secret-hook-token"
    config = load_config(
        {"DATABASE_URL": database_url, "DISCORD_WEBHOOK": webhook, "APP_ENV": "dev"}
    )

    text = f"{config!r} {config} {config.as_public_dict()!r}"
    assert database_url not in text
    assert webhook not in text
    assert "secretpw" not in text
    assert "secret-hook-token" not in text


def test_secret_values_never_appear_in_repr_str_public_output_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "s3cr3t-token-value"
    database_url = "postgres://user:dbpassword@host/db"
    webhook = "https://discord.example/api/webhooks/42/webhook-secret"
    config = load_config(
        {
            "DISCORD_BOT_TOKEN": secret,
            "DATABASE_URL": database_url,
            "DISCORD_WEBHOOK": webhook,
            "APP_ENV": "dev",
        }
    )

    for text in (repr(config), str(config), repr(config.as_public_dict())):
        assert secret not in text
        assert database_url not in text
        assert "dbpassword" not in text
        assert "webhook-secret" not in text

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("asec").info("config=%r", config)
        logging.getLogger("asec").info("public=%r", config.as_public_dict())
    assert secret not in caplog.text
    assert database_url not in caplog.text
    assert "dbpassword" not in caplog.text

    # Real values stay retrievable for application code.
    assert config.require("DISCORD_BOT_TOKEN") == secret
    assert config.get("DATABASE_URL") == database_url
    assert config.get("DISCORD_WEBHOOK") == webhook


def test_is_secret_key_matches_common_secret_names() -> None:
    assert is_secret_key("DISCORD_BOT_TOKEN")
    assert is_secret_key("db_password")
    assert is_secret_key("API_KEY")
    assert not is_secret_key("APP_ENV")
    assert not is_secret_key("LOG_LEVEL")


def test_part_settings_defaults_are_the_owner_approved_values() -> None:
    """O9-6: 40 relevance messages and 20 proposal candidates per part."""
    assert DEFAULT_RELEVANCE_MAX_MESSAGES_PER_CALL == 40
    assert DEFAULT_PROPOSAL_MAX_CANDIDATES_PER_CALL == 20
    assert load_part_settings(load_config({})) == PartSettings(
        relevance_max_messages_per_call=40,
        proposal_max_candidates_per_call=20,
    )


def test_part_settings_are_read_from_configuration() -> None:
    config = load_config(
        {
            "RELEVANCE_MAX_MESSAGES_PER_CALL": "7",
            "PROPOSAL_MAX_CANDIDATES_PER_CALL": "3",
        }
    )
    assert load_part_settings(config) == PartSettings(
        relevance_max_messages_per_call=7,
        proposal_max_candidates_per_call=3,
    )


@pytest.mark.parametrize("value", ["0", "-1", "abc", "2.5"])
def test_invalid_part_settings_fail_loudly(value: str) -> None:
    """NFR-MNT-01: a limit is validated, never silently clamped."""
    config = load_config({"RELEVANCE_MAX_MESSAGES_PER_CALL": value})
    with pytest.raises(ConfigError):
        load_part_settings(config)


def test_production_database_path_is_documented() -> None:
    env_example = REPO_ROOT / ".env.example"
    text = env_example.read_text(encoding="utf-8")

    assert "ASEC_DB_PATH" in text


def test_env_example_exists_and_has_no_real_secrets() -> None:
    env_example = REPO_ROOT / ".env.example"
    assert env_example.is_file()

    for raw_line in env_example.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if is_secret_key(key):
            assert value == "", f"{key} must stay empty in .env.example"
        assert not _TOKEN_LIKE.search(value), f"{key} looks like a real secret"
