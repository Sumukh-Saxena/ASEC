"""W9 H1: logging setup, rotating file, redaction and level filtering.

Every handler configured for the ``asec`` namespace must run the redaction
filter before formatting. Log output may carry a level, logger name, event and
counts, but never a credential, a Discord identifier or message content
(NFR-SEC-01, SRS DR-07, plan §4.6.2).
"""

from __future__ import annotations

import io
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from backend.config import ConfigError, load_config
from backend.logging_setup import setup_logging

TOKEN = "Bot MTIzNDU2Nzg5MDEyMzQ1Njc4.GaBsCd." + "x" * 30
MESSAGE_BODY = "meet me at the north gate at seven"


@pytest.fixture(autouse=True)
def _clean_asec_logger() -> object:
    logger = logging.getLogger("asec")
    yield
    for handler in list(logger.handlers):
        if getattr(handler, "_asec_managed", False):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


def _configure(
    tmp_path: Path,
    values: dict[str, str] | None = None,
) -> tuple[Path, io.StringIO]:
    log_file = tmp_path / "asec.log"
    settings = {"LOG_FILE": str(log_file)}
    if values is not None:
        settings.update(values)
    stream = io.StringIO()
    setup_logging(load_config(settings), stream=stream)
    return log_file, stream


def test_token_and_message_body_never_appear_in_file_output(tmp_path: Path) -> None:
    log_file, _stream = _configure(tmp_path)

    logging.getLogger("asec.ingest").info("request token=%s", TOKEN)
    logging.getLogger("asec.ingest").warning("payload content=%s", MESSAGE_BODY)

    text = log_file.read_text(encoding="utf-8")
    assert TOKEN not in text
    assert MESSAGE_BODY not in text
    assert "REDACTED" in text


def test_level_filtering_uses_log_level(tmp_path: Path) -> None:
    log_file, _stream = _configure(tmp_path, {"LOG_LEVEL": "WARNING"})

    logging.getLogger("asec.test").debug("debug detail")
    logging.getLogger("asec.test").info("info detail")
    logging.getLogger("asec.test").warning("warning detail")

    text = log_file.read_text(encoding="utf-8")
    assert "debug detail" not in text
    assert "info detail" not in text
    assert "warning detail" in text


def test_file_handler_uses_five_megabytes_and_five_backups(tmp_path: Path) -> None:
    _configure(tmp_path)

    logger = logging.getLogger("asec")
    file_handlers = [
        handler
        for handler in logger.handlers
        if isinstance(handler, RotatingFileHandler)
        and getattr(handler, "_asec_managed", False)
    ]
    assert len(file_handlers) == 1
    handler = file_handlers[0]
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 5
    assert handler.encoding == "utf-8"


def test_invalid_log_level_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        setup_logging(
            load_config(
                {"LOG_LEVEL": "LOUD", "LOG_FILE": str(tmp_path / "asec.log")}
            )
        )


def test_setup_twice_does_not_duplicate_handlers(tmp_path: Path) -> None:
    _configure(tmp_path)
    log_file, _stream = _configure(tmp_path)

    logger = logging.getLogger("asec")
    managed = [
        handler
        for handler in logger.handlers
        if getattr(handler, "_asec_managed", False)
    ]
    assert len(managed) == 2

    logging.getLogger("asec.test").info("one record")
    text = log_file.read_text(encoding="utf-8")
    assert text.count("one record") == 1
