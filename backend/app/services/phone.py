"""Phone number normalisation.

The single most important detail in a blocklist: the number a moderator types
("+1 (555) 019-2834") and the number the provider sends ("+15550192834") must
compare equal, or blocking silently does nothing and nobody notices until the
caller gets through again.

So every number is normalised to E.164-ish digits with a leading ``+`` on the
way *into* the database and on the way *into* a comparison. Store normalised,
compare normalised, display formatted.

Scope: this is deliberately a small, dependency-free normaliser that handles
NANP (+1) and already-qualified international numbers. It does not do real
carrier-grade parsing -- if you need that (extensions, short codes, national
formats for arbitrary countries), swap the body of :func:`normalize_number`
for `phonenumbers` (the Python port of libphonenumber). The signature is the
only thing the rest of the app depends on.
"""

from __future__ import annotations

import re

#: Anything that is not a digit or a leading plus is punctuation to us.
_NON_DIALABLE = re.compile(r"[^\d+]")
_NANP_LENGTH = 10


class InvalidPhoneNumber(ValueError):
    """Raised when a string cannot be read as a phone number at all."""


def normalize_number(raw: str, *, default_country_code: str = "1") -> str:
    """Return ``raw`` as ``+<digits>``.

    >>> normalize_number("+1 (555) 019-2834")
    '+15550192834'
    >>> normalize_number("555-019-2834")
    '+15550192834'
    >>> normalize_number("+44 20 7946 0958")
    '+442079460958'

    Raises :class:`InvalidPhoneNumber` for anything with too few digits to be
    a real number, so a typo in the moderation UI surfaces as a 400 rather
    than silently adding an entry that can never match.
    """
    if not raw or not raw.strip():
        raise InvalidPhoneNumber("phone number is empty")

    cleaned = _NON_DIALABLE.sub("", raw.strip())
    had_plus = cleaned.startswith("+")
    digits = cleaned.lstrip("+")

    if not digits.isdigit():
        raise InvalidPhoneNumber(f"{raw!r} is not a phone number")
    if len(digits) < 7:
        raise InvalidPhoneNumber(f"{raw!r} has too few digits to be a phone number")
    if len(digits) > 15:  # E.164 caps the subscriber number at 15 digits.
        raise InvalidPhoneNumber(f"{raw!r} has too many digits to be a phone number")

    # A bare 10-digit number is a NANP local number; a leading 1 with 11 digits
    # is the same number already carrying its country code.
    if not had_plus:
        if len(digits) == _NANP_LENGTH:
            digits = default_country_code + digits
        elif len(digits) == _NANP_LENGTH + 1 and digits.startswith("1"):
            pass  # already 1XXXXXXXXXX

    return f"+{digits}"


def try_normalize(raw: str | None) -> str | None:
    """Normalise, or return ``None`` for missing/unparseable input.

    Used on the inbound-call path, where a withheld or malformed caller ID is
    normal and must not break call handling.
    """
    if not raw:
        return None
    try:
        return normalize_number(raw)
    except InvalidPhoneNumber:
        return None
