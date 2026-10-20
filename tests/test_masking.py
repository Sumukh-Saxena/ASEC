"""Tests for the Phase 1 personal-data masking service (NFR-SEC-04).

All fixtures use synthetic data only.
"""

from __future__ import annotations

import pytest

from backend.masking import (
    EMAIL_PLACEHOLDER,
    GOV_ID_PLACEHOLDER,
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
        "reach 98765  43210",
        "reach 98765 - 43210",
        "reach 98765- 43210",
        "reach 98765\t43210",
        "reach 98765\u00a043210",
        "reach (98765)  43210",
        "reach +91  98765 43210",
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


def test_masks_government_id_like_numbers() -> None:
    """SRS v2.0 NFR-SEC-04 and EIR-03: government-ID-like numbers shall be
    masked before every AI call.

    Replaces the former ``test_does_not_apply_government_id_masking``, which
    encoded the stale pre-v2.0 SRS reading ("no Aadhaar/PAN masking")."""
    assert mask_pii("ABCDE1234F") == GOV_ID_PLACEHOLDER
    assert mask_pii("234567890123") == GOV_ID_PLACEHOLDER
    assert GOV_ID_PLACEHOLDER not in mask_pii("134567890123")


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


@pytest.mark.parametrize(
    "text",
    [
        "reach 98765  43210",
        "reach 98765 - 43210",
        "reach 98765- 43210",
        "reach 98765\t43210",
        "reach 98765\u00a043210",
        "reach (98765)  43210",
        "reach +91  98765 43210",
    ],
)
def test_masks_phones_with_unusual_separators(text: str) -> None:
    assert mask_pii(text) == f"reach {PHONE_PLACEHOLDER}"


@pytest.mark.parametrize(
    "text",
    [
        "98765  4321",
        "98765  432109",
        "9876  543210",
        "198765  43210",
        "98765  4321098765",
        "12345  67890",
    ],
)
def test_phone_separator_false_positives_are_not_masked(text: str) -> None:
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
    [PHONE_PLACEHOLDER, EMAIL_PLACEHOLDER, ROLL_NO_PLACEHOLDER, GOV_ID_PLACEHOLDER],
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


# --- NFR-SEC-04 (SRS v2.0): flexible Indian mobile digit groupings ------------


@pytest.mark.parametrize(
    "text",
    [
        "98765 43210",
        "987 654 3210",
        "98-7654-3210",
        "98 76 54 32 10",
        "98765.43210",
        "987.654.3210",
        "98.76.54.32.10",
        "call 98765 43210 now",
        "+91 987 654 3210",
        "91 98-7654-3210",
        "0 98765 43210",
        "+91.98765.43210",
        "0 98 76 54 32 10",
    ],
)
def test_masks_flex_grouped_indian_phones(text: str) -> None:
    masked = mask_pii(text)
    assert PHONE_PLACEHOLDER in masked
    assert not any(char.isdigit() for char in masked)


@pytest.mark.parametrize(
    "text",
    [
        "id 42, phone 98-7654-3210",
        "seats 456: call 98 76 54 32 10",
    ],
)
def test_masks_flex_phones_in_mixed_context(text: str) -> None:
    masked = mask_pii(text)
    assert PHONE_PLACEHOLDER in masked


@pytest.mark.parametrize(
    "text",
    [
        "134567890123456",  # 15-digit run
        "9876543210123456789",  # 19-digit run starts 9 (snowflake-shaped)
        "98765  4321",  # 9 digits
        "987 654 32101",  # 11 digits
        "03-7654-3210",
    ],
)
def test_flex_phone_false_positives_are_not_masked(text: str) -> None:
    assert mask_pii(text) == text


# --- NFR-SEC-04: '+'-prefixed international numbers (8-14 digits) -------------


@pytest.mark.parametrize(
    "text",
    [
        "+44 1632 960961",
        "+1 202 555 1234",
        "+7-999-123-45-67",
        "+911234567890",
        "+35312345678",
    ],
)
def test_masks_international_plus_numbers(text: str) -> None:
    masked = mask_pii(text)
    assert masked == PHONE_PLACEHOLDER


@pytest.mark.parametrize(
    "text",
    [
        "+1234567",  # 7 digits: below the 8-digit minimum
        "+123456789012345678",  # 18 digits: snowflake-shaped
        "2026-01-01T12:00:00+00:00",  # timezone offset, not a phone
        "+0.5 people",  # 1 digit
    ],
)
def test_international_false_positives_are_not_masked(text: str) -> None:
    assert mask_pii(text) == text


# --- NFR-SEC-04: Aadhaar-shaped numbers ----------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "aadhaar 234567890123",
        "aadhaar 2345 6789 0123",
        "aadhaar 2345-6789-0123",
        "9999 8888 7777",
        "2345-6789-0123",
    ],
)
def test_masks_aadhaar_like_numbers(text: str) -> None:
    masked = mask_pii(text)
    assert GOV_ID_PLACEHOLDER in masked
    assert not any(char.isdigit() for char in masked)


@pytest.mark.parametrize(
    "text",
    [
        "134567890123",  # first digit 1: outside 2-9
        "0234567890126",  # 13 digits starting 0
        "234567890123456789",  # Aadhaar-shaped head of a longer digit run
        "9234 567890123",  # not 4-4-4 grouped
        "2026-10-13T12:00:00",  # ISO timestamp digits
    ],
)
def test_aadhaar_false_positives_are_not_masked(text: str) -> None:
    assert mask_pii(text) == text


# --- ordering: phones before government IDs (NFR-SEC-04) -----------------------


def test_phone_rules_precede_government_ids() -> None:
    masked = mask_pii("+91 98765 43210")
    assert masked == PHONE_PLACEHOLDER
    assert GOV_ID_PLACEHOLDER not in masked


def test_masks_mixed_phone_and_government_ids() -> None:
    masked = mask_pii("aadhaar 2345 6789 0123, PAN ABCDE1234F, call +44 1632 960961")
    assert masked == (
        f"aadhaar {GOV_ID_PLACEHOLDER}, PAN {GOV_ID_PLACEHOLDER},"
        f" call {PHONE_PLACEHOLDER}"
    )


# --- idempotence and purity for the extended scope -----------------------------


@pytest.mark.parametrize(
    "text",
    [
        "aadhaar 2345 6789 0123 and PAN ABCDE1234F, call 98 76 54 32 10",
        "+91 98765 43210 -> [PHONE]",
        "roll B25CS1044 a@b.com +44 1632 960961 9999 8888 7777",
    ],
)
def test_idempotent_with_extended_scope(text: str) -> None:
    once = mask_pii(text)
    assert mask_pii(once) == once
    assert not any(char.isdigit() for char in once)


def test_gov_id_placeholder_has_no_digits() -> None:
    masked = mask_pii("234567890123")
    assert GOV_ID_PLACEHOLDER == "[GOV_ID]"
    assert not any(char.isdigit() for char in masked)


@pytest.mark.parametrize(
    "text",
    [
        "msg 987654321098765432 id 612345678901234567 at 2026-10-13T12:00:00"
        " then 10:30 and plain 42",
    ],
)
def test_v2_negatives_untouched(text: str) -> None:
    assert mask_pii(text) == text


def test_masks_mixed_pii_v2_composite() -> None:
    text = "need B25CS1044's pan ABCDE1234F by 18:30, aadhaar 9999 8888 7777"
    masked = mask_pii(text)
    assert masked == (
        f"need {ROLL_NO_PLACEHOLDER}'s pan {GOV_ID_PLACEHOLDER} by 18:30,"
        f" aadhaar {GOV_ID_PLACEHOLDER}"
    )
