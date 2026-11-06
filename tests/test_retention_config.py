"""W9 H6: retention configuration and pure calendar-month helpers.

Configuration only: no purge code. Retention is measured from the festival end
date the Admin records (SRS DR-07), so the date arithmetic uses calendar months
and clamps a month-end day to the target month's length.
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.config import (
    ConfigError,
    load_config,
    load_retention_settings,
    retention_date,
    retention_due,
)


def test_retention_defaults_are_unset_and_three_months() -> None:
    settings = load_retention_settings(load_config({}))
    assert settings.festival_end_date is None
    assert settings.retention_months == 3


def test_retention_settings_read_valid_values() -> None:
    settings = load_retention_settings(
        load_config(
            {"FESTIVAL_END_DATE": "2026-11-30", "RETENTION_MONTHS": "6"}
        )
    )
    assert settings.festival_end_date == date(2026, 11, 30)
    assert settings.retention_months == 6


@pytest.mark.parametrize("value", ["2026-13-01", "30-11-2026", "tomorrow"])
def test_invalid_festival_end_date_is_rejected(value: str) -> None:
    with pytest.raises(ConfigError):
        load_retention_settings(load_config({"FESTIVAL_END_DATE": value}))


@pytest.mark.parametrize("value", ["0", "-1", "three", "1.5"])
def test_invalid_retention_months_is_rejected(value: str) -> None:
    with pytest.raises(ConfigError):
        load_retention_settings(load_config({"RETENTION_MONTHS": value}))


def test_retention_date_clamps_month_end() -> None:
    assert retention_date(date(2026, 11, 30), 3) == date(2027, 2, 28)


def test_retention_date_handles_leap_years() -> None:
    assert retention_date(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert retention_date(date(2024, 2, 29), 12) == date(2025, 2, 28)


def test_retention_date_rolls_over_the_year() -> None:
    assert retention_date(date(2026, 12, 15), 3) == date(2027, 3, 15)


@pytest.mark.parametrize("months", [0, -1])
def test_retention_date_rejects_non_positive_months(months: int) -> None:
    with pytest.raises(ValueError):
        retention_date(date(2026, 1, 1), months)


def test_retention_date_rejects_an_unset_festival_end() -> None:
    with pytest.raises(ValueError):
        retention_date(None, 3)


def test_retention_due_on_and_after_the_retention_date() -> None:
    festival_end = date(2026, 11, 30)
    assert retention_due(date(2027, 2, 27), festival_end, 3) is False
    assert retention_due(date(2027, 2, 28), festival_end, 3) is True
    assert retention_due(date(2027, 3, 1), festival_end, 3) is True


def test_retention_due_is_false_when_the_festival_end_is_unset() -> None:
    assert retention_due(date(2027, 2, 28), None, 3) is False
