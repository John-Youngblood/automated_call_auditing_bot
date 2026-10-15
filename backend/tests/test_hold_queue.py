"""Sizing the hold queue to HOLD_QUEUE_MAX_SIZE at startup.

The size is the cap on how much holding a show can cost, so the cases that
matter are the ones where it would silently not be applied.
"""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from app.config import Settings
from app.services.hold_queue import size_hold_queue
from tests.conftest import patch_twilio

ACCOUNT = "AC00000000000000000000000000000000"
QUEUES = f"/2010-04-01/Accounts/{ACCOUNT}/Queues.json"
QUEUE_SID = "QU00000000000000000000000000000000"


def settings(**overrides: object) -> Settings:
    base = {
        "twilio_account_sid": ACCOUNT,
        "twilio_api_key_sid": "SK00000000000000000000000000000000",
        "twilio_api_key_secret": "secret",
        "hold_queue_name": "screening",
        "hold_queue_max_size": 200,
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[arg-type]


@pytest.fixture
def twilio_queues(monkeypatch: pytest.MonkeyPatch):
    """Serve ``queues`` from the listing; return the POSTs made, as (path, form)."""

    def _apply(queues: list[dict] | None) -> list[tuple[str, dict[str, list[str]]]]:
        posted: list[tuple[str, dict[str, list[str]]]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                posted.append((request.url.path, parse_qs(request.content.decode())))
                return httpx.Response(201, json={"sid": QUEUE_SID})
            if queues is None:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json={"queues": queues})

        patch_twilio(monkeypatch, httpx.MockTransport(handler))
        return posted

    return _apply


async def test_a_missing_queue_is_created_at_the_configured_size(twilio_queues) -> None:
    """Left to <Enqueue>, the first caller would create it at Twilio's 1000."""
    posted = twilio_queues([])

    assert await size_hold_queue(settings()) is True
    assert posted == [(QUEUES, {"FriendlyName": ["screening"], "MaxSize": ["200"]})]


async def test_an_existing_queue_is_resized(twilio_queues) -> None:
    posted = twilio_queues([{"sid": QUEUE_SID, "friendly_name": "screening", "max_size": 1000}])

    assert await size_hold_queue(settings()) is True
    assert posted == [
        (f"/2010-04-01/Accounts/{ACCOUNT}/Queues/{QUEUE_SID}.json", {"MaxSize": ["200"]})
    ]


async def test_a_queue_already_at_size_is_left_alone(twilio_queues) -> None:
    posted = twilio_queues([{"sid": QUEUE_SID, "friendly_name": "screening", "max_size": 200}])

    assert await size_hold_queue(settings()) is True
    assert posted == []


async def test_only_the_named_queue_is_touched(twilio_queues) -> None:
    posted = twilio_queues([{"sid": "QUother", "friendly_name": "support", "max_size": 50}])

    await size_hold_queue(settings())

    assert [path for path, _ in posted] == [QUEUES]


async def test_twilio_being_down_creates_nothing(twilio_queues) -> None:
    """Not knowing whether the queue exists is not the same as it not
    existing -- creating one then could leave two queues with one name."""
    posted = twilio_queues(None)

    assert await size_hold_queue(settings()) is False
    assert posted == []


async def test_missing_credentials_skip_silently() -> None:
    assert await size_hold_queue(Settings(_env_file=None)) is False


@pytest.mark.parametrize("size", [0, 5001])
def test_sizes_twilio_would_refuse_are_rejected_at_startup(size: int) -> None:
    with pytest.raises(ValueError, match="hold_queue_max_size"):
        settings(hold_queue_max_size=size)
