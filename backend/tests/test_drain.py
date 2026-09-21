"""Clearing whoever is still holding, at the end of a show."""

from __future__ import annotations

from xml.etree.ElementTree import fromstring

import httpx
import pytest

from app.config import Settings
from app.schemas.calls import Caller, CallStatus
from app.services.broadcaster import Broadcaster
from app.services.call_registry import CallRegistry
from app.services.drain import hang_up_holders
from app.telephony.rest import TwilioRestClient
from app.telephony.twiml import say_and_hangup

ACCOUNT = "AC" + "0" * 32


def settings(**overrides: object) -> Settings:
    base = {
        "twilio_account_sid": ACCOUNT,
        "twilio_api_key_sid": "SK" + "0" * 32,
        "twilio_api_key_secret": "secret",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[arg-type]


def registry_with(*call_ids: str) -> CallRegistry:
    reg = CallRegistry(broadcaster=Broadcaster(queue_max=50))
    for call_id in call_ids:
        reg.register_incoming(call_id, caller=Caller(number="+15035551234"))
        reg.set_transcript(call_id, "I have a question.", 0.9)
    return reg


@pytest.fixture
def patch_transport(monkeypatch: pytest.MonkeyPatch):
    def _apply(transport: httpx.MockTransport) -> None:
        original = TwilioRestClient.__aenter__

        async def entered(self: TwilioRestClient) -> TwilioRestClient:
            await original(self)
            self._client = httpx.AsyncClient(transport=transport)  # noqa: SLF001
            return self

        monkeypatch.setattr(TwilioRestClient, "__aenter__", entered)

    return _apply


def twilio_stub(*, reject: set[str] | None = None, statuses: dict[str, str] | None = None):
    """Accepts hang-ups except for call SIDs in ``reject``.

    ``statuses`` drives the follow-up status probe, which is how a rejected
    write is told apart from a caller who had already hung up.
    """
    reject = reject or set()
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sid = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        if request.method == "POST":
            seen.append((sid, request.content.decode()))
            if sid in reject:
                return httpx.Response(500, text="nope")
            return httpx.Response(200, json={"sid": sid, "status": "completed"})
        return httpx.Response(
            200,
            json={
                "sid": sid,
                "from": "+15035551234",
                "to": "+15039990000",
                "caller_name": None,
                "start_time": None,
                "status": (statuses or {}).get(sid, "in-progress"),
            },
        )

    return httpx.MockTransport(handler), seen


class TestGoodbyeTwiml:
    def test_says_then_hangs_up(self) -> None:
        doc = fromstring(say_and_hangup("The show has ended.").body)
        assert [child.tag for child in doc] == ["Say", "Hangup"]
        assert doc.find("Say").text == "The show has ended."

    def test_the_message_cannot_break_out_of_the_xml(self) -> None:
        """The message is configuration, so it is built, not interpolated."""
        doc = fromstring(say_and_hangup("Bye</Say><Dial>+15559999999</Dial><Say>").body)
        assert [child.tag for child in doc] == ["Say", "Hangup"]
        assert doc.find("Dial") is None


async def test_everyone_holding_is_hung_up(patch_transport) -> None:
    transport, seen = twilio_stub()
    patch_transport(transport)
    reg = registry_with("CA1", "CA2", "CA3")

    result = await hang_up_holders(reg, settings())

    assert sorted(result.ended) == ["CA1", "CA2", "CA3"]
    assert result.failed == []
    assert reg.open_calls() == []
    assert all(c.status is CallStatus.ENDED for c in reg.recent_calls())


async def test_callers_hear_a_goodbye_rather_than_a_silent_drop(patch_transport) -> None:
    transport, seen = twilio_stub()
    patch_transport(transport)

    await hang_up_holders(registry_with("CA1"), settings(closing_message="That is all, folks."))

    assert len(seen) == 1
    _, body = seen[0]
    # Sent as inline TwiML, not Status=completed.
    assert "Twiml=" in body
    assert "Status=completed" not in body
    assert "folks" in body


async def test_a_caller_we_could_not_reach_stays_on_the_dashboard(patch_transport) -> None:
    """The failure that matters: they are still connected and still holding,
    so removing them from the queue would hide a live caller."""
    transport, _ = twilio_stub(reject={"CA2"}, statuses={"CA2": "in-progress"})
    patch_transport(transport)
    reg = registry_with("CA1", "CA2")

    result = await hang_up_holders(reg, settings())

    assert result.ended == ["CA1"]
    assert result.failed == ["CA2"]
    assert [c.call_id for c in reg.open_calls()] == ["CA2"]


async def test_a_caller_who_already_hung_up_counts_as_success(patch_transport) -> None:
    """Twilio rejects the write because the call is gone. The goal was 'this
    caller is not holding', which is satisfied -- reporting it as a failure
    would send an operator chasing a call that ended by itself."""
    transport, _ = twilio_stub(reject={"CA1"}, statuses={"CA1": "completed"})
    patch_transport(transport)
    reg = registry_with("CA1")

    result = await hang_up_holders(reg, settings())

    assert result.ended == ["CA1"]
    assert result.failed == []


async def test_an_empty_queue_needs_no_credentials() -> None:
    result = await hang_up_holders(registry_with(), Settings(_env_file=None))
    assert result.ended == [] and result.failed == []


async def test_missing_credentials_report_failure_not_success() -> None:
    """Silence here would let an operator believe the line was cleared."""
    reg = registry_with("CA1", "CA2")

    result = await hang_up_holders(reg, Settings(_env_file=None))

    assert result.ended == []
    assert sorted(result.failed) == ["CA1", "CA2"]
    assert len(reg.open_calls()) == 2
