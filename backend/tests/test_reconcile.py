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
from app.schemas.calls import Call, CallStatus
from app.services.broadcaster import Broadcaster
from app.services.call_registry import CallRegistry
from app.services.reconcile import reconcile_hold_queue
from app.services.store import Store
from app.telephony.rest import TwilioRestClient, client_from_settings

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


@pytest.fixture
def reg(store: Store) -> CallRegistry:
    return CallRegistry(Broadcaster(queue_max=50), store)


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
            return httpx.Response(200, json={"queue_members": members or [], "next_page_uri": None})
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


async def test_waiting_callers_are_restored_to_the_queue(patch_transport, reg) -> None:
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

    assert await reconcile_hold_queue(reg, settings()) == 2

    calls = reg.open_calls()
    assert [c.call_id for c in calls] == ["CA1", "CA2"]
    assert all(c.status == "on-hold" for c in calls)
    # The point of the flag: these are not callers who stayed silent.
    assert all(c.recovered for c in calls)
    assert all(c.transcript is None for c in calls)
    assert [c.caller.number for c in calls] == ["+15035551234", "+15035559999"]


async def test_the_callers_original_start_time_is_kept(patch_transport, reg) -> None:
    """The queue is ordered by wait time, so restoring `now` would send
    everyone recovered to the back and reward hanging up and redialling."""
    patch_transport(
        twilio_stub(queues=[QUEUE], members=[member("CA1")], calls={"CA1": call_body("CA1")})
    )

    await reconcile_hold_queue(reg, settings())

    call = reg.get("CA1")
    assert call is not None
    assert call.started_at == datetime(2026, 9, 21, 20, 13, 20, tzinfo=UTC)


async def test_a_live_call_is_never_overwritten(patch_transport, reg) -> None:
    """The race that matters: a caller mid-webhook while reconciliation runs.

    The call in memory is the newest copy of them, so clobbering it with a
    rebuilt one would throw away whatever arrived since."""
    patch_transport(
        twilio_stub(queues=[QUEUE], members=[member("CA1")], calls={"CA1": call_body("CA1")})
    )
    reg.register_incoming("CA1")
    reg.set_transcript("CA1", "I have a question for the guest.", 0.94)

    assert await reconcile_hold_queue(reg, settings()) == 0

    call = reg.get("CA1")
    assert call is not None
    assert call.transcript == "I have a question for the guest."
    assert call.recovered is False


async def test_a_caller_who_already_hung_up_is_not_restored(patch_transport, reg) -> None:
    patch_transport(
        twilio_stub(
            queues=[QUEUE],
            members=[member("CA1")],
            calls={"CA1": call_body("CA1", status="completed")},
        )
    )

    assert await reconcile_hold_queue(reg, settings()) == 0
    assert reg.open_calls() == []


async def test_a_caller_who_hung_up_meanwhile_stays_ended(patch_transport, reg, store) -> None:
    """Twilio's answer can be out of date by the time the rebuild acts on it:
    the hang-up has already closed their saved row, end time and all."""
    store.save_call(Call(call_id="CA1", status=CallStatus.ON_HOLD))
    reg.end("CA1")
    patch_transport(
        twilio_stub(queues=[QUEUE], members=[member("CA1")], calls={"CA1": call_body("CA1")})
    )

    assert await reconcile_hold_queue(reg, settings()) == 0
    assert reg.open_calls() == []
    assert store.get_call("CA1").ended_at is not None


async def test_only_a_caller_whose_words_were_lost_is_flagged(patch_transport, reg, store) -> None:
    """A saved row that reached on-hold had its speech result: no transcript
    there means they said nothing, not that it needs asking again."""
    store.save_call(Call(call_id="CA1", status=CallStatus.ON_HOLD))
    store.save_call(Call(call_id="CA2"))  # still talking when the backend went
    patch_transport(
        twilio_stub(
            queues=[QUEUE],
            members=[member("CA1", 1), member("CA2", 2)],
            calls={"CA1": call_body("CA1"), "CA2": call_body("CA2")},
        )
    )

    assert await reconcile_hold_queue(reg, settings()) == 2
    assert [(c.call_id, c.recovered) for c in reg.open_calls()] == [
        ("CA1", False),
        ("CA2", True),
    ]


async def test_a_failed_lookup_still_restores_the_caller(patch_transport, reg) -> None:
    """Losing someone's number is survivable; losing their place is not."""
    patch_transport(twilio_stub(queues=[QUEUE], members=[member("CA1")], calls={}))

    assert await reconcile_hold_queue(reg, settings()) == 1
    assert reg.get("CA1").caller.number is None


async def test_a_withheld_number_stays_withheld(patch_transport, reg) -> None:
    """The Call resource carries Twilio's placeholders just as From does."""
    patch_transport(
        twilio_stub(
            queues=[QUEUE],
            members=[member("CA1")],
            calls={"CA1": call_body("CA1", "+266696687")},
        )
    )

    assert await reconcile_hold_queue(reg, settings()) == 1
    assert reg.get("CA1").caller.number is None


async def test_no_queue_yet_is_not_an_error(patch_transport, reg) -> None:
    patch_transport(twilio_stub(queues=[], members=[]))
    assert await reconcile_hold_queue(reg, settings()) == 0


async def test_twilio_being_down_leaves_an_empty_queue(patch_transport, reg) -> None:
    """A boot-time failure must not stop the service answering new calls."""
    patch_transport(twilio_stub(fail={f"/2010-04-01/Accounts/{ACCOUNT}/Queues.json"}))
    assert await reconcile_hold_queue(reg, settings()) == 0


async def test_missing_credentials_skip_silently(reg) -> None:
    blank = Settings(_env_file=None)
    assert await reconcile_hold_queue(reg, blank) == 0


class TestCredentialSelection:
    def test_api_key_is_preferred(self) -> None:
        client = client_from_settings(settings())
        assert client is not None
        assert client._auth[0].startswith("SK")  # noqa: SLF001

    def test_auth_token_is_the_fallback(self) -> None:
        client = client_from_settings(
            settings(twilio_api_key_sid="", twilio_api_key_secret="", twilio_auth_token="tok")
        )
        assert client is not None
        assert client._auth == (ACCOUNT, "tok")  # noqa: SLF001

    def test_no_credentials_at_all(self) -> None:
        assert client_from_settings(Settings(_env_file=None)) is None
