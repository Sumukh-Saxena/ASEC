"""Shared test cleanup for the process-wide ``asec`` logger."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _reset_asec_logging() -> Iterator[None]:
    """Close and remove handlers installed by ``setup_logging`` after each test."""
    yield
    logger = logging.getLogger("asec")
    for handler in list(logger.handlers):
        if getattr(handler, "_asec_managed", False):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
