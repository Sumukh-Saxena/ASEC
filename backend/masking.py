"""Personal-data masking for ASEC (NFR-SEC-04, EIR-03).

Replaces phone numbers, email addresses, government-ID-like numbers and
institute roll numbers with fixed placeholders so they can be withheld before
text leaves the backend for an AI call. The originals stay in the backend;
masking never mutates or replaces source content (DR-03).

The function is pure: deterministic, idempotent, and non-destructive. Patterns
are plain code constants for now (no configuration, no dependencies). Matching
is pattern-based only; it does not validate values.

Replacement order (each pass sees the output of the previous one):
1. email addresses,
2. institute roll numbers (an extra safeguard because the roll number is the
   login credential, not an SRS-mandated scope item; NFR-SEC-04 requires
   phones, emails and government-ID-like numbers) — before phones so a roll
   number is never split into a partial phone,
3. phone numbers:
   a. the fixed five-and-five forms with optional +91 / 91 / 0 prefix,
   b. '+'-prefixed international numbers with 8-14 digits and separators,
      before the grouped Indian patterns so "+7-999-123-45-67" is masked
      whole,
   c. flexibly grouped Indian mobiles: exactly 10 digits starting 6-9,
      single space/hyphen/dot between any digit groups, optional +91 / 91 / 0
      prefix, every digit run at most 5 digits.
   Phones run before Aadhaar so a '+91 ...' with a ten-digit body is a phone,
   not a government ID. Exact digit counts plus digit lookarounds keep
   Discord snowflakes (17-19 digits), ISO timestamps and small numbers out.
4. government-ID-like numbers — Aadhaar shape (12 digits, first digit 2-9,
   contiguous or grouped 4-4-4 by space or hyphen) and PAN shape
   (AAAAA9999A), both replaced with ``[GOV_ID]``. Rejections never guess:
   a candidate that fails its shape validator is left as written.
"""

from __future__ import annotations

import re
from collections.abc import Callable

PHONE_PLACEHOLDER = "[PHONE]"
EMAIL_PLACEHOLDER = "[EMAIL]"
ROLL_NO_PLACEHOLDER = "[ROLL_NO]"
GOV_ID_PLACEHOLDER = "[GOV_ID]"

_Replacement = str | Callable[[re.Match[str]], str]

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Common Indian mobile formats: optional +91 / 91 / 0 prefix (a separator may
# directly follow the prefix), optional parentheses around the first five
# digits, and grouping separators between the two five-digit halves made of
# spaces, tabs or non-breaking spaces optionally around a single hyphen (for
# example "98765 43210", "98765 - 43210", "98765- 43210").
# The digit lookarounds keep it out of longer digit runs such as Discord
# snowflakes and timestamps.
_PHONE_PATTERN = re.compile(
    r"(?<![0-9])(?:\+?91[ \t\u00a0-]*|0[ \t\u00a0.\-]?)?"
    r"\(?[6-9][0-9]{4}\)?[ \t\u00a0]*(?:-[ \t\u00a0]*)?[0-9]{5}(?![0-9])"
)

# Institute roll number: BTech/MTech/PhD marker, two admission-year digits, a
# variable-length alphabetic branch code, then a numeric identifier (for example
# B25CS1044). Alphanumeric lookarounds stop it matching part of a longer token.
_ROLL_NO_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])[BMPbmp][0-9]{2}[A-Za-z]+[0-9]+(?![A-Za-z0-9])"
)

# At most one separator between adjacent digit groups of a flexibly formatted
# number: a single space, tab, non-breaking space, hyphen or dot.
_SEP = r"[ \t\u00a0.\-]"

# '+'-prefixed international numbers with 8-14 digits and separators between
# any digit groups. The 8-14 bounds exclude timezone offsets and single/small
# numbers; the lookarounds exclude longer digit runs such as snowflakes.
_INTERNATIONAL_PHONE = re.compile(
    r"(?<![0-9])\+(?:" + _SEP + r"?[0-9]){8,14}(?![0-9])"
)


def _indian_flexible(match: re.Match[str]) -> str:
    """Replacer for flexibly grouped Indian mobile candidates.

    The pattern has already fixed the total digit count (10) and the leading
    digit (6-9); the replacer additionally requires every digit run inside
    the match to be at most 5 digits, so hand-grouped mobiles ("98 76 54 32
    10", "987 654 3210") are masked while coincidentally adjacent numbers
    whose first run is 6+ digits ("960961 9999 ...") are not merged into a
    fake number. On rejection the matched text is returned unchanged.
    """
    runs = re.findall(r"[0-9]+", match.group(0))
    if runs and max(len(run) for run in runs) <= 5:
        return PHONE_PLACEHOLDER
    return match.group(0)


# Indian mobile as digit groups: optional +91 / 91 / 0 prefix, then exactly
# 10 digits with their leading digit in 6-9, at most one separator per gap.
# The exact ``{9}`` unit tail and the (?!...) digit lookarounds keep matches
# out of digit runs with a different length.
_INDIAN_PHONE_PREFIXED = re.compile(
    r"(?<![0-9])(?:\+91|91|0)" + _SEP + r"?[6-9](?:" + _SEP + r"?[0-9]){9}(?![0-9])"
)
_INDIAN_PHONE_BARE = re.compile(
    r"(?<![0-9])[6-9](?:" + _SEP + r"?[0-9]){9}(?![0-9])"
)

# Government-ID-like numbers (NFR-SEC-04), masked after phone rules so a
# prefixed Indian mobile is always a phone. Aadhaar shape: 12 digits, first
# digit 2-9, contiguous or grouped 4-4-4 by space or hyphen. PAN shape: five
# letters, four digits, one letter, delimited by non-alphanumerics.
_AADHAAR_PATTERN = re.compile(
    r"(?<![0-9])(?:[2-9][0-9]{11}|[2-9][0-9]{3}[ \-][0-9]{4}[ \-][0-9]{4})(?![0-9])"
)
_PAN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Za-z0-9])"
)

# Order matters (see module docstring): email, roll numbers, phones (fixed
# forms, international, flexibly grouped), government IDs (Aadhaar, PAN).
_REPLACEMENTS: tuple[tuple[re.Pattern[str], _Replacement], ...] = (
    (_EMAIL_PATTERN, EMAIL_PLACEHOLDER),
    (_ROLL_NO_PATTERN, ROLL_NO_PLACEHOLDER),
    (_PHONE_PATTERN, PHONE_PLACEHOLDER),
    (_INTERNATIONAL_PHONE, PHONE_PLACEHOLDER),
    (_INDIAN_PHONE_PREFIXED, _indian_flexible),
    (_INDIAN_PHONE_BARE, _indian_flexible),
    (_AADHAAR_PATTERN, GOV_ID_PLACEHOLDER),
    (_PAN_PATTERN, GOV_ID_PLACEHOLDER),
)


def mask_pii(text: str) -> str:
    """Return a masked copy of ``text``.

    Phone numbers, government-ID-like numbers (Aadhaar/PAN shapes), institute
    roll numbers and email addresses are replaced by ``[PHONE]``, ``[GOV_ID]``,
    ``[ROLL_NO]`` and ``[EMAIL]`` in the documented order. The input string is
    never modified and no other content is changed.
    """
    masked = text
    for pattern, replacement in _REPLACEMENTS:
        masked = pattern.sub(replacement, masked)
    return masked
