"""Phone numbers: which ones are real, and where they are from.

Twilio derives FromCity/FromState/FromCountry from the *number's* rate centre,
so they describe where the number was issued, not where the person is. A weak
hint, never a fact. Any of them can arrive as an empty string.
"""

from __future__ import annotations

import re

#: Where a state is more useful than the country: "Portland, OR" beats
#: "Portland, US" to a US operator.
_NANP_COUNTRIES = frozenset({"US", "CA"})

#: E.164: "+", a country code that does not start with 0, at most 15 digits.
#: [0-9] rather than \d, which also matches other scripts' digits.
_E164 = re.compile(r"\+[1-9][0-9]{7,14}")

#: What carriers put in From when the caller withheld their number, as digits:
#: keypad spellings of ANONYMOUS, RESTRICTED, BLOCKED, UNKNOWN and UNAVAILABLE,
#: and 464 -- plus RESTRICTED again with the +1 a NANP carrier prepends. Some
#: are well-formed E.164, so the format check alone would let them through and
#: every withheld caller would share one contact.
_WITHHELD = frozenset(
    {"266696687", "7378742833", "2562533", "8656696", "86282452253", "464", "17378742833"}
)

#: What people type between digits.
_SEPARATORS = re.compile(r"[\s().-]")


class InvalidPhoneNumber(ValueError):
    """Typed input that is not a phone number. The message is for the operator."""


def caller_number(raw: str | None) -> str | None:
    """The caller's number from Twilio's From, or None if they withheld it.

    Twilio already sends real numbers in E.164, so nothing is normalised --
    this only decides which values are real numbers. "anonymous", "unknown"
    and the placeholders above all mean the same thing: no caller ID.
    """
    if raw is None or not _E164.fullmatch(raw) or raw[1:] in _WITHHELD:
        return None
    return raw


def parse_typed_number(raw: str) -> str:
    """A number an operator typed, as E.164.

    Accepts the forms people write: "+44 20 7946 0958", "(503) 555-0147",
    "1-503-555-0147". A bare number is taken as North American.
    """
    text = _SEPARATORS.sub("", raw)
    if any(c.isalpha() for c in text):
        # Catches extensions ("x12", "ext. 12") as well as vanity numbers.
        raise InvalidPhoneNumber("Enter digits only, without letters or an extension.")
    if text.removeprefix("+") in _WITHHELD:
        raise InvalidPhoneNumber("That's what carriers send for a withheld number, not a real one.")
    if text.startswith(("00", "011")):
        raise InvalidPhoneNumber("Start an international number with + instead of 00 or 011.")
    if text.startswith("+"):
        number = text
    elif len(text) == 10:
        number = f"+1{text}"
    elif len(text) == 11 and text.startswith("1"):
        number = f"+{text}"
    else:
        raise InvalidPhoneNumber(
            "Enter a 10-digit North American number, or + and the country code."
        )
    if not _E164.fullmatch(number):
        raise InvalidPhoneNumber("That doesn't look like a phone number.")
    return number


def format_location(city: str | None, state: str | None, country: str | None) -> str | None:
    """Best short label, or None.

    >>> format_location("Portland", "OR", "US")
    'Portland, OR'
    >>> format_location("Manchester", None, "GB")
    'Manchester, GB'
    """
    # City keeps its own casing; the codes are conventionally upper.
    city = (city or "").strip()
    state = (state or "").strip().upper()
    country = (country or "").strip().upper()

    if city and state and country in _NANP_COUNTRIES:
        return f"{city}, {state}"
    if city and country:
        return f"{city}, {country}"
    if city:
        return city
    if state and country:
        return f"{state}, {country}"
    return country or None
