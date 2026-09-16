"""Number normalisation.

The blocklist is only as good as this: if the number a moderator types does
not normalise to the same string the provider sends, blocking silently does
nothing.
"""

from __future__ import annotations

import pytest

from app.services.phone import (
    InvalidPhoneNumber,
    format_location,
    normalize_number,
    try_normalize,
)


@pytest.mark.parametrize(
    "raw",
    [
        "+1 (555) 019-2834",
        "+15550192834",
        "1-555-019-2834",
        "555.019.2834",
        "(555) 019 2834",
        "  5550192834  ",
        "+1 555 019 2834",
    ],
)
def test_every_human_format_reaches_the_same_key(raw: str) -> None:
    assert normalize_number(raw) == "+15550192834"


def test_international_numbers_keep_their_country_code() -> None:
    assert normalize_number("+44 20 7946 0958") == "+442079460958"


def test_bare_ten_digits_get_the_default_country_code() -> None:
    assert normalize_number("5550192834") == "+15550192834"


@pytest.mark.parametrize("raw", ["", "   ", "abc", "12345", "+" + "9" * 20])
def test_unusable_input_is_rejected(raw: str) -> None:
    with pytest.raises(InvalidPhoneNumber):
        normalize_number(raw)


def test_try_normalize_swallows_bad_input() -> None:
    # Withheld caller ID is routine on the inbound path and must not raise.
    assert try_normalize(None) is None
    assert try_normalize("") is None
    assert try_normalize("anonymous") is None
    assert try_normalize("+1 (555) 019-2834") == "+15550192834"


class TestLocationLabel:
    """Twilio sends city/state/country; only two of the three are worth showing."""

    def test_domestic_calls_show_the_state(self) -> None:
        # "Portland, US" tells a US operator nothing -- everything is US.
        assert format_location("Portland", "OR", "US") == "Portland, OR"
        assert format_location("Toronto", "ON", "CA") == "Toronto, ON"

    def test_international_calls_show_the_country(self) -> None:
        assert format_location("Manchester", None, "GB") == "Manchester, GB"
        assert format_location("Sydney", "NSW", "AU") == "Sydney, AU"

    def test_falls_back_when_twilio_omits_fields(self) -> None:
        """International calls frequently carry no city at all."""
        assert format_location("Portland", None, "US") == "Portland, US"
        assert format_location("Portland", None, None) == "Portland"
        assert format_location(None, "OR", "US") == "OR, US"
        assert format_location(None, None, "GB") == "GB"
        assert format_location(None, None, None) is None

    def test_blank_and_scruffy_input_is_tolerated(self) -> None:
        assert format_location("", "", "") is None
        assert format_location("  Portland  ", " or ", " us ") == "Portland, OR"
