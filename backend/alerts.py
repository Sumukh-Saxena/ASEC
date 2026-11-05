"""Structured alerts (plan §4.6.5).

The Phase 9 alert surface is intentionally small: one immutable :class:`Alert`
and one log-backed sender behind a protocol. A push or Discord sender is a
Phase 11 concern and plugs into the same protocol.

Alerts never carry message content, Discord user identifiers or credentials;
they carry the rule, severity, the observed value, the threshold and a short
operator-facing description (NFR-SEC-01, plan §4.9).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger("asec.alerts")

#: Rule name for a message that arrived with no content, no attachment and no
#: embed (owner addition to W5; Discord's Message Content intent is the likely
#: cause when this happens for a whole page).
EMPTY_CONTENT_RULE = "EMPTY_CONTENT"

#: Rule name for a group whose live poll or ingestion keeps failing; the clock
#: for that group is held until a poll succeeds again (W8a).
POLL_FAILURE_RULE = "POLL_FAILURE"


@dataclass(frozen=True)
class Alert:
    """One alert occurrence; ``rule`` is free text drawn from the rule set."""

    rule: str
    severity: str
    observed: float | int
    threshold: float | int
    message: str
    at: str


class AlertSender(Protocol):
    """The only thing an alert producer needs from a delivery channel."""

    def send(self, alert: Alert) -> None: ...


class LogAlertSender:
    """Phase 9's only sender: write the alert fields through the logger.

    The log call passes the fields as arguments, so nothing is rendered until
    the record is emitted and no untrusted content is interpolated into the
    message (plan §4.6.5, §4.9).
    """

    def __init__(self, target: logging.Logger | None = None) -> None:
        self._logger = logger if target is None else target

    def send(self, alert: Alert) -> None:
        self._logger.warning(
            "alert rule=%s severity=%s observed=%s threshold=%s at=%s message=%s",
            alert.rule,
            alert.severity,
            alert.observed,
            alert.threshold,
            alert.at,
            alert.message,
        )
