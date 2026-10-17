"""Personal-data masking for ASEC (NFR-SEC-04).

Replaces phone numbers, institute roll numbers and email addresses with fixed
placeholders so they can be withheld before text leaves the backend for an AI call.
The originals stay in the backend; masking never mutates or replaces source content
(DR-03).

The function is pure: deterministic, idempotent, and non-destructive. Patterns are
plain code constants for now (no configuration, no dependencies). Matching is
pattern-based only; it does not validate values. No Aadhaar, PAN or generic
government-ID masking is performed.
"""

from __future__ import annotations

import re

PHONE_PLACEHOLDER = "[PHONE]"
EMAIL_PLACEHOLDER = "[EMAIL]"
ROLL_NO_PLACEHOLDER = "[ROLL_NO]"

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Common Indian mobile formats: optional +91 / 91 / 0 prefix, optional parentheses
# around the first five digits, and optional space or hyphen grouping. The digit
# lookarounds keep it out of longer digit runs such as Discord snowflakes and
# timestamps.
_PHONE_PATTERN = re.compile(
    r"(?<![0-9])(?:\+?91[ \-]?|0)?\(?[6-9][0-9]{4}\)?[ \-]?[0-9]{5}(?![0-9])"
)

# Institute roll number: BTech/MTech/PhD marker, two admission-year digits, a
# variable-length alphabetic branch code, then a numeric identifier (for example
# B25CS1044). Alphanumeric lookarounds stop it matching part of a longer token.
_ROLL_NO_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])[BMPbmp][0-9]{2}[A-Za-z]+[0-9]+(?![A-Za-z0-9])"
)

# Order matters: email first so roll-number-shaped local parts are masked as email,
# then roll numbers before phones so a roll numeric id that looks like a ten-digit
# mobile is masked as one roll number rather than partially as a phone.
_REPLACEMENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (EMAIL_PLACEHOLDER, _EMAIL_PATTERN),
    (ROLL_NO_PLACEHOLDER, _ROLL_NO_PATTERN),
    (PHONE_PLACEHOLDER, _PHONE_PATTERN),
)


def mask_pii(text: str) -> str:
    """Return a masked copy of ``text``.

    Phone numbers, institute roll numbers and email addresses are replaced by
    ``[PHONE]``, ``[ROLL_NO]`` and ``[EMAIL]``. The input string is never modified
    and no other content is changed.
    """
    masked = text
    for placeholder, pattern in _REPLACEMENTS:
        masked = pattern.sub(placeholder, masked)
    return masked
