"""W9 H1: configuration-driven logging for the ``asec`` namespace.

One call configures the ``asec`` logger from :class:`backend.config.Config`:
the validated ``LOG_LEVEL`` (default INFO), an optional rotating file handler
(5 MB x 5 backups, UTF-8) and a stderr handler for foreground runs. Every
handler runs :class:`RedactionFilter` before formatting, so a token or a
message body cannot reach an output even if a future call site is careless.

Logs are operational only. SRS DR-07 keeps message-derived data until three
months after the festival end date, so message content, Discord user ids and
credentials must never be logged at all; redaction is defence in depth, not
permission to log them.
"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

from backend.config import Config, load_logging_settings

#: Required by W9 H1: 5 MB per file, five backups.
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_SECRET_REPLACEMENT = "[REDACTED:SECRET]"
_CONTENT_REPLACEMENT = "[REDACTED:CONTENT]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(Bot|Bearer)\s+\S+"), rf"\1 {_SECRET_REPLACEMENT}"),
    (
        re.compile(
            r"(?i)\b(TOKEN|SECRET|PASSWORD|PASSWD|PWD|API_KEY|KEY|OTP)"
            r"\s*=\s*[^\s,;]+"
        ),
        rf"\1={_SECRET_REPLACEMENT}",
    ),
    (
        re.compile(r"\b[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{20,}\b"),
        _SECRET_REPLACEMENT,
    ),
    (
        re.compile(r"\b[A-Za-z0-9+/=_\-]{32,}\b"),
        _SECRET_REPLACEMENT,
    ),
    (
        re.compile(r"(?i)\b(CONTENT|BODY|TEXT)\s*=\s*[^\n]+"),
        rf"\1={_CONTENT_REPLACEMENT}",
    ),
)


def _redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactionFilter(logging.Filter):
    """Redact credentials and content before a record is formatted.

    The record is rendered first, so values passed through lazy ``%s``
    arguments are covered too; ``args`` is cleared afterwards to avoid a second
    formatting pass. The filter never raises on a malformed record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001 - logging must never break the caller
            rendered = str(record.msg)
        record.msg = _redact_text(rendered)
        record.args = ()
        if record.exc_text:
            record.exc_text = _redact_text(record.exc_text)
        return True


def setup_logging(
    config: Config, *, stream: TextIO | None = None
) -> logging.Logger:
    """Configure the ``asec`` logger from ``config`` and return it.

    Calling this again replaces the handlers this module added earlier, so a
    second call (or a test that invokes the CLI repeatedly) does not duplicate
    output. Handlers installed by someone else are left alone.
    """
    settings = load_logging_settings(config)
    logger = logging.getLogger("asec")
    for existing in list(logger.handlers):
        if getattr(existing, "_asec_managed", False):
            logger.removeHandler(existing)
            existing.close()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    redaction = RedactionFilter()
    logger.setLevel(settings.level)
    logger.propagate = False

    if settings.log_file is not None:
        path = Path(settings.log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        _install(file_handler, formatter, redaction, logger)

    stream_handler = logging.StreamHandler(sys.stderr if stream is None else stream)
    _install(stream_handler, formatter, redaction, logger)
    return logger


def _install(
    handler: logging.Handler,
    formatter: logging.Formatter,
    redaction: RedactionFilter,
    logger: logging.Logger,
) -> None:
    handler.setFormatter(formatter)
    handler.addFilter(redaction)
    # Tag only the handlers this module owns, so a second setup can replace
    # them without disturbing a handler another component attached.
    handler.__dict__["_asec_managed"] = True
    logger.addHandler(handler)
