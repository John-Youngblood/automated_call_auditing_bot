"""Caller location labels.

Twilio sends city/state/country derived from the number's rate centre, and any
of them can arrive as an empty string rather than being absent.
"""

from __future__ import annotations

from app.services.phone import format_location


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
