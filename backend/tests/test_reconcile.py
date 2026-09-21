"""Rebuilding the hold queue from Twilio after a restart.

Twilio is stubbed at the HTTP layer with real response bodies rather than by
mocking our own client, so these cover the parsing too -- RFC 2822 dates and
the ``from``/``to`` keys are exactly the kind of thing that looks right until
a real payload arrives.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.config import Settings
from app.services.broadcaster import Broadcaster
from app.services.call_registry import CallRegistry
from app.services.reconcile import reconcile_hold_queue, rest_client
from app.telephony.rest import TwilioRestClient

ACCOUNT = "AC00000000000000000000000000000000"
QUEUE_SID = "QU00000000000000000000000000000000"


def settings(**overrides: object) -> Settings:
    base = {
        "twilio_account_sid": ACCOUNT,
        "twilio_api_key_sid": "SK00000000000000000000000000000000",
        "twilio_api_key_secret": "secret",
        "hold_queue_name": "screening",
    }
    # _env_file=None matters: Settings otherwise reads ../.env, so whether
    # these tests pass would depend on the developer's local credentials.
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[arg-type]


def registry() -> CallRegistry:
    return CallRegistry(broadcaster=Broadcaster(queue_max=50))


def twilio_stub(
    *,
    queues: list[dict] | None = None,
    members: list[dict] | None = None,
    calls: dict[str, dict] | None = None,
    fail: set[str] | None = None,
) -> httpx.MockTransport:
    """A stand-in Twilio, serving the three endpoints reconciliation uses."""
    fail = fail or set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in fail:
            return httpx.Response(500, text="boom")
        if path.endswith("/Queues.json"):
            return httpx.Response(200, json={"queues": queues if queues is not None else []})
        if path.endswith("/Members.json"):
            return httpx.Response(
                200, json={"queue_members": members or [], "next_page_uri": None}
            )
        if "/Calls/" in path:
            sid = path.rsplit("/", 1)[-1].removesuffix(".json")
            body = (calls or {}).get(sid)
            return httpx.Response(200, json=body) if body else httpx.Response(404, json={})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


@pytest.fixture
def patch_transport(monkeypatch: pytest.MonkeyPatch):
    """Route the REST client's httpx calls at a stub instead of the internet."""

    def _apply(transport: httpx.MockTransport) -> None:
        original = TwilioRestClient.__aenter__

        async def entered(self: TwilioRestClient) -> TwilioRestClient:
            await original(self)
            self._client = httpx.AsyncClient(transport=transport)  # noqa: SLF001
            return self

        monkeypatch.setattr(TwilioRestClient, "__aenter__", entered)

    return _apply


QUEUE = {"sid": QUEUE_SID, "friendly_name": "screening", "current_size": 2, "max_size": 1000}


def member(sid: str, position: int = 1) -> dict:
    return {
        "call_sid": sid,
        "date_enqueued": "Mon, 21 Sep 2026 20:14:02 +0000",
        "position": position,
        "wait_time": 45,
        "queue_sid": QUEUE_SID,
    }


def call_body(sid: str, number: str = "+15035551234", **overrides: object) -> dict:
    body = {
        "sid": sid,
        # Not a kwarg: `from` is a Python keyword, which is exactly how the
        # first version of this helper silently ignored the override.
        "from": number,
        "to": "+15039990000",
        "caller_name": None,
        "start_time": "Mon, 21 Sep 2026 20:13:20 +0000",
        "status": "in-progress",
    }
    return {**body, **overrides}


async def test_waiting_callers_are_restored_to_the_queue(patch_transport) -> None:
    patch_transport(
        twilio_stub(
            queues=[QUEUE],
            members=[member("CA1", 1), member("CA2", 2)],
            calls={
                "CA1": call_body("CA1", "+15035551234"),
                "CA2": call_body("CA2", "+15035559999"),
            },
        )
    )
    reg = registry()

    assert await reconcile_hold_queue(reg, settings()) == 2

    calls = reg.open_calls()
    assert [c.call_id for c in calls] == ["CA1", "CA2"]
    assert all(c.status == "on-hold" for c in calls)
    # The point of the flag: these are not callers who stayed silent.
    assert all(c.recovered for c in calls)
    assert all(c.transcript is None for c in calls)
    assert [c.caller.number for c in calls] == ["+15035551234", "+15035559999"]


async def test_the_callers_original_start_time_is_kept(patch_transport) -> None:
    """The queue is ordered by wait time, so restoring `now` would send
    everyone recovered to the back and reward hanging up and redialling."""
    patch_transport(
        twilio_stub(queues=[QUEUE], members=[member("CA1")], calls={"CA1": call_body("CA1")})
    )
    reg = registry()

    await reconcile_hold_queue(reg, settings())

    call = reg.require("CA1")
    assert call.started_at == datetime(2026, 9, 21, 20, 13, 20, tzinfo=UTC)


async def test_a_live_call_is_never_overwritten(patch_transport) -> None:
    """The race that matters: a caller mid-webhook while reconciliation runs.

    Their transcript is the one thing that cannot be recovered from Twilio, so
    clobbering it with a blank recovered row would destroy the only copy."""
    patch_transport(
        twilio_stub(queues=[QUEUE], members=[member("CA1")], calls={"CA1": call_body("CA1")})
    )
    reg = registry()
    reg.register_incoming("CA1")
    reg.set_transcript("CA1", "I have a question for the guest.", 0.94)

    assert await reconcile_hold_queue(reg, settings()) == 0

    call = reg.require("CA1")
    assert call.transcript == "I have a question for the guest."
    assert call.recovered is False


async def test_a_caller_who_already_hung_up_is_not_restored(patch_transport) -> None:
    patch_transport(
        twilio_stub(
            queues=[QUEUE],
            members=[member("CA1")],
            calls={"CA1": call_body("CA1", status="completed")},
        )
    )
    reg = registry()

    assert await reconcile_hold_queue(reg, settings()) == 0
    assert reg.open_calls() == []


async def test_a_failed_lookup_still_restores_the_caller(patch_transport) -> None:
    """Losing someone's number is survivable; losing their place is not."""
    patch_transport(twilio_stub(queues=[QUEUE], members=[member("CA1")], calls={}))
    reg = registry()

    assert await reconcile_hold_queue(reg, settings()) == 1
    assert reg.require("CA1").caller.number is None


async def test_no_queue_yet_is_not_an_error(patch_transport) -> None:
    patch_transport(twilio_stub(queues=[], members=[]))
    assert await reconcile_hold_queue(registry(), settings()) == 0


async def test_twilio_being_down_leaves_an_empty_queue(patch_transport) -> None:
    """A boot-time failure must not stop the service answering new calls."""
    patch_transport(twilio_stub(fail={f"/2010-04-01/Accounts/{ACCOUNT}/Queues.json"}))
    assert await reconcile_hold_queue(registry(), settings()) == 0


async def test_missing_credentials_skip_silently() -> None:
    blank = Settings(_env_file=None)
    assert await reconcile_hold_queue(registry(), blank) == 0


class TestCredentialSelection:
    def test_api_key_is_preferred(self) -> None:
        client = rest_client(settings())
        assert client is not None
        assert client._auth[0].startswith("SK")  # noqa: SLF001

    def test_auth_token_is_the_fallback(self) -> None:
        client = rest_client(
            settings(twilio_api_key_sid="", twilio_api_key_secret="", twilio_auth_token="tok")
        )
        assert client is not None
        assert client._auth == (ACCOUNT, "tok")  # noqa: SLF001

    def test_no_credentials_at_all(self) -> None:
        assert rest_client(Settings(_env_file=None)) is None
