"""Phone numbers: which are real, what operators type, where they are from.

Twilio sends city/state/country derived from the number's rate centre, and any
of them can arrive as an empty string rather than being absent.
"""

from __future__ import annotations

import pytest

from app.services.phone import (
    InvalidPhoneNumber,
    caller_number,
    format_location,
    parse_typed_number,
)

#: What carriers send in From for a withheld number, every way it arrives.
WITHHELD = [
    "anonymous",
    "Anonymous",
    "unknown",
    "restricted",
    "",
    None,
    *(
        f"{plus}{digits}"
        for digits in ("266696687", "7378742833", "2562533", "8656696", "86282452253", "464")
        for plus in ("", "+")
    ),
    "+17378742833",
]


class TestCallerNumber:
    """The gate every inbound From goes through. A placeholder that got past
    it would make every withheld caller one contact with one history."""

    @pytest.mark.parametrize("raw", WITHHELD)
    def test_a_withheld_number_is_none(self, raw: str | None) -> None:
        assert caller_number(raw) is None

    @pytest.mark.parametrize("raw", ["+15035550147", "+442079460958", "+61291234567", "+12345678"])
    def test_a_real_number_is_kept_as_sent(self, raw: str) -> None:
        assert caller_number(raw) == raw

    @pytest.mark.parametrize(
        "raw",
        [
            "5035550147",  # not E.164: Twilio would have added the +1
            "+05035550147",  # no country code starts with 0
            "+1234567890123456",  # 16 digits, past E.164's 15
            "+1 503 555 0147",
            "+15035550147\n",
            "+١٥٠٣٥٥٥٠١٤٧",  # digits, but not ASCII ones
            "sip:alice@example.com",
        ],
    )
    def test_anything_else_is_none(self, raw: str) -> None:
        assert caller_number(raw) is None


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


class TestTypedNumber:
    """What an operator types into the contacts form or the history filter."""

    @pytest.mark.parametrize(
        ("typed", "number"),
        [
            ("+15035550147", "+15035550147"),
            ("(503) 555-0147", "+15035550147"),
            ("503.555.0147", "+15035550147"),
            ("1-503-555-0147", "+15035550147"),
            ("15035550147", "+15035550147"),
            ("  503 555 0147  ", "+15035550147"),
            ("+44 20 7946 0958", "+442079460958"),
            ("+61 (2) 9123-4567", "+61291234567"),
        ],
    )
    def test_the_usual_ways_of_writing_a_number(self, typed: str, number: str) -> None:
        assert parse_typed_number(typed) == number

    @pytest.mark.parametrize(
        ("typed", "says"),
        [
            ("", "10-digit"),
            ("555-0147", "10-digit"),
            ("25035550147", "10-digit"),
            ("503-555-0147 x12", "extension"),
            ("503-555-0147 ext. 12", "extension"),
            ("1-800-FLOWERS", "letters"),
            ("011 44 20 7946 0958", "instead of 00 or 011"),
            ("0044 20 7946 0958", "instead of 00 or 011"),
            ("+0 503 555 0147", "doesn't look like"),
            ("+1 503 555 0147 1234 5", "doesn't look like"),
            ("503-555-014#", "doesn't look like"),
            ("+266696687", "withheld"),
            ("737-874-2833", "withheld"),
            ("+1 737 874 2833", "withheld"),
            ("86282452253", "withheld"),
        ],
    )
    def test_anything_else_says_what_is_wrong(self, typed: str, says: str) -> None:
        with pytest.raises(InvalidPhoneNumber, match=says):
            parse_typed_number(typed)
