"""Tests for the Phase 1 personal-data masking service (NFR-SEC-04).

All fixtures use synthetic data only.
"""

from __future__ import annotations

import pytest

from backend.masking import (
    EMAIL_PLACEHOLDER,
    PHONE_PLACEHOLDER,
    ROLL_NO_PLACEHOLDER,
    mask_pii,
)


@pytest.mark.parametrize(
    "text",
    [
        "call me on 9876543210",
        "reach +91 98765 43210",
        "reach +91-98765-43210",
        "reach +919876543210",
        "reach 91 9876543210",
        "reach 09876543210",
        "reach 98765-43210",
        "reach (98765) 43210",
        "reach (98765)43210",
        "reach +91 (98765) 43210",
    ],
)
def test_masks_indian_phone_numbers(text: str) -> None:
    masked = mask_pii(text)
    assert PHONE_PLACEHOLDER in masked
    assert not any(char.isdigit() for char in masked)


@pytest.mark.parametrize(
    "email",
    [
        "a.b-c@example.com",
        "user_1@sub.domain.co.in",
        "x+tag@example.org",
    ],
)
def test_masks_email_addresses(email: str) -> None:
    masked = mask_pii(f"mail {email} now")
    assert EMAIL_PLACEHOLDER in masked
    assert email not in masked
    assert "@" not in masked


@pytest.mark.parametrize(
    "text",
    [
        "B25CS1044",
        "M25CS1001",
        "P25CS1001",
        "B25CSE1044",
        "B25IT1044",
        "B25ECE1001",
        "B25CS1",
        "b25cs1044",
        "m25ece7",
    ],
)
def test_masks_institute_roll_numbers(text: str) -> None:
    assert mask_pii(text) == ROLL_NO_PLACEHOLDER


@pytest.mark.parametrize(
    "text",
    [
        "XB25CS1044",
        "B25CS1044X",
        "B25CS1044ABC",
        "AB25CS1044",
        "B2CS1",
        "B2CS",
        "25CS1044",
    ],
)
def test_roll_number_false_positives_are_not_masked(text: str) -> None:
    assert mask_pii(text) == text


def test_masks_roll_number_at_message_boundaries() -> None:
    assert mask_pii("roll B25CS1044 end") == f"roll {ROLL_NO_PLACEHOLDER} end"


def test_preserves_ordinary_text_and_counts() -> None:
    text = "need 5 volunteers at Gate 3 before 18:30"
    assert mask_pii(text) == text


def test_preserves_discord_snowflakes_and_timestamps() -> None:
    text = "msg 123456789012345678 at 2026-10-13T12:00:00 by user 987654321098765432"
    assert mask_pii(text) == text


def test_does_not_apply_government_id_masking() -> None:
    assert mask_pii("ABCDE1234F") == "ABCDE1234F"
    assert mask_pii("234567890123") == "234567890123"


def test_original_string_is_not_mutated() -> None:
    original = "roll B25CS1044 call 9876543210"
    snapshot = original
    mask_pii(original)
    assert original == snapshot


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_empty_and_whitespace_input(text: str) -> None:
    assert mask_pii(text) == text


def test_masks_parenthesized_phone_with_other_pii() -> None:
    text = "reach (98765) 43210, mail a@b.com, roll B25CS1044"
    assert mask_pii(text) == (
        f"reach {PHONE_PLACEHOLDER}, mail {EMAIL_PLACEHOLDER}, roll {ROLL_NO_PLACEHOLDER}"
    )


@pytest.mark.parametrize(
    "text",
    [
        "(12345) 67890",
        "(12345)67890",
        "(98765)",
        "1234567890",
        "(55555) 12345",
    ],
)
def test_phone_number_false_positives_are_not_masked(text: str) -> None:
    assert mask_pii(text) == text


def test_masks_multiple_pii_in_one_message() -> None:
    text = "roll B25CS1044 phone +91 98765 43210 email a@b.com"
    assert mask_pii(text) == (
        f"roll {ROLL_NO_PLACEHOLDER} phone {PHONE_PLACEHOLDER} email {EMAIL_PLACEHOLDER}"
    )


def test_masks_pii_at_string_boundaries() -> None:
    assert mask_pii("9876543210") == PHONE_PLACEHOLDER
    assert mask_pii("a@b.com") == EMAIL_PLACEHOLDER
    assert mask_pii("B25CS1044") == ROLL_NO_PLACEHOLDER


@pytest.mark.parametrize(
    "text",
    [
        "call 9876543210",
        "a@b.com and 234567890123",
        "plain text 42",
        "roll B25CS1044",
    ],
)
def test_idempotent_when_applied_twice(text: str) -> None:
    once = mask_pii(text)
    assert mask_pii(once) == once


@pytest.mark.parametrize(
    "placeholder",
    [PHONE_PLACEHOLDER, EMAIL_PLACEHOLDER, ROLL_NO_PLACEHOLDER],
)
def test_placeholders_are_not_themselves_pii(placeholder: str) -> None:
    assert mask_pii(placeholder) == placeholder


def test_deterministic_same_input_same_output() -> None:
    text = "mixed a@b.com +91 98765 43210 B25CS1044"
    assert mask_pii(text) == mask_pii(text)


def test_long_message_keeps_unrelated_text() -> None:
    text = ("lorem ipsum " * 2000) + "call 9876543210"
    masked = mask_pii(text)
    assert masked.startswith("lorem ipsum ")
    assert masked.endswith(PHONE_PLACEHOLDER)
    assert len(masked) == len(text) - len("9876543210") + len(PHONE_PLACEHOLDER)


def test_hinglish_text_passes_through() -> None:
    text = "bhai gate 3 pe 5 log chahiye"
    assert mask_pii(text) == text
