"""Caller location labels.

Twilio derives FromCity/FromState/FromCountry from the *number's* rate centre,
so they describe where the number was issued, not where the person is. A weak
hint, never a fact. Any of them can arrive as an empty string.
"""

from __future__ import annotations

#: Where a state is more useful than the country: "Portland, OR" beats
#: "Portland, US" to a US operator.
_NANP_COUNTRIES = frozenset({"US", "CA"})


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
