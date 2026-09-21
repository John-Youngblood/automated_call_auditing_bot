"""Caller location formatting.

One function, used on the inbound-call path to turn Twilio's geographic
webhook parameters into the short label the dashboard shows under a caller's
number.
"""

from __future__ import annotations

#: Countries in the North American Numbering Plan, where a state/province is a
#: more useful label than the country: "Portland, OR" beats "Portland, US".
_NANP_COUNTRIES = frozenset({"US", "CA"})


def format_location(city: str | None, state: str | None, country: str | None) -> str | None:
    """Best short label for where a caller's *number* is registered.

    Note what this is not: Twilio derives these fields from the number's rate
    centre, so they describe where the number was issued, not where the person
    is. A ported mobile keeps its original area code forever. Treat the result
    as a weak hint, never as the caller's location.

    Every one of these parameters can arrive as an empty string rather than
    being absent, so each branch tests for content, not presence.

    The rule is about which second component carries information:

    >>> format_location("Portland", "OR", "US")
    'Portland, OR'
    >>> format_location("Manchester", None, "GB")
    'Manchester, GB'

    For a domestic call the country is noise -- everything is "US" -- so the
    state does the work. For an international one the country is the whole
    point. Twilio rarely sends a state outside the NANP anyway.
    """
    # City keeps its own casing (it is a proper name); the two codes are
    # conventionally upper-case and are compared upper-case below.
    city = (city or "").strip()
    state = (state or "").strip().upper()
    country = (country or "").strip().upper()

    if city and state and country in _NANP_COUNTRIES:
        return f"{city}, {state}"
    if city and country:
        return f"{city}, {country}"
    if city:
        return city
    # No city is common on international calls; fall back to whatever is left
    # rather than showing nothing.
    if state and country:
        return f"{state}, {country}"
    return country or None
