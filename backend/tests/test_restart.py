"""A restart mid-show: two runs of the backend on one database file.

The first run takes calls as Twilio would send them. The second starts on the
same file while Twilio still has callers on hold, the way a crash or an update
mid-show leaves things -- and has to put the queue back as it was.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from app.main import create_app
from app.services.store import Store
from tests.conftest import patch_twilio

NUMBER = "+15035551234"
QUEUE_SID = "QU" + "0" * 32
INCOMING = {
    "CallSid": "CA-held",
    "From": NUMBER,
    "To": "+15039990000",
    "FromCity": "Portland",
    "FromState": "OR",
    "FromCountry": "US",
}


def twilio_holding(*call_sids: str, down: bool = False) -> httpx.MockTransport:
    """A Twilio whose hold queue has ``call_sids`` in it -- or, ``down``, one
    that cannot be asked."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if down:
            return httpx.Response(503, text="unavailable")
        if path.endswith("/Queues.json"):
            queue = {"sid": QUEUE_SID, "friendly_name": "screening", "max_size": 200}
            return httpx.Response(200, json={"queues": [queue]})
        if path.endswith("/Members.json"):
            members = [{"call_sid": sid, "position": n} for n, sid in enumerate(call_sids, 1)]
            return httpx.Response(200, json={"queue_members": members})
        sid = path.rsplit("/", 1)[-1].removesuffix(".json")
        return httpx.Response(
            200,
            json={
                "sid": sid,
                "from": NUMBER,
                "to": "+15039990000",
                "start_time": "Mon, 21 Sep 2026 20:13:20 +0000",
                "status": "in-progress",
            },
        )

    return httpx.MockTransport(handler)


@pytest.fixture
def run(configure_env, monkeypatch: pytest.MonkeyPatch):
    """Start the backend on this test's database, with Twilio as given, and
    wait for reconciliation to finish before handing it over."""

    @asynccontextmanager
    async def _run(
        twilio: httpx.MockTransport, **env: str
    ) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
        configure_env(**{"RECONCILE_ON_STARTUP": "true", **env})
        patch_twilio(monkeypatch, twilio)
        app = create_app()
        async with app.router.lifespan_context(app):
            for task in asyncio.all_tasks():
                if task.get_name() == "reconcile-hold-queue":
                    await task
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield app, client

    return _run


async def hold(client: httpx.AsyncClient, call_sid: str = "CA-held") -> None:
    await client.post("/webhook/incoming-call", data={**INCOMING, "CallSid": call_sid})
    await client.post(
        "/webhook/speech-result",
        data={"CallSid": call_sid, "SpeechResult": "About the guest", "Confidence": "0.9"},
    )


def saved(app: FastAPI, call_sid: str):
    store: Store = app.state.store
    return store.get_call(call_sid)


async def test_a_caller_on_hold_comes_back_with_what_they_said(run) -> None:
    async with run(twilio_holding()) as (_, client):
        await hold(client)

    async with run(twilio_holding("CA-held")) as (_, client):
        [call] = (await client.get("/api/calls")).json()

    assert call["callId"] == "CA-held"
    assert call["status"] == "on-hold"
    assert call["transcript"] == "About the guest"
    assert call["caller"]["location"] == "Portland, OR"
    # Nothing was lost, so nothing for the operator to ask again.
    assert call["recovered"] is False


async def test_a_caller_mid_greeting_is_revived_by_their_speech_result(run) -> None:
    """Rang before the restart, still talking through it: never in Twilio's
    queue, so reconciliation cannot find them -- their transcript arriving
    is the first this run hears of them."""
    async with run(twilio_holding()) as (_, client):
        await client.post("/webhook/incoming-call", data=INCOMING)

    async with run(twilio_holding()) as (app, client):
        # Swept as ended during the restart; the transcript proves otherwise.
        assert saved(app, "CA-held").status == "ended"
        await client.post(
            "/webhook/speech-result",
            data={"CallSid": "CA-held", "SpeechResult": "Still here", "Confidence": "0.8"},
        )
        [call] = (await client.get("/api/calls")).json()

    assert (call["callId"], call["status"], call["transcript"]) == (
        "CA-held",
        "on-hold",
        "Still here",
    )
    assert call["caller"]["location"] == "Portland, OR"
    assert call["endedAt"] is None


async def test_a_speech_result_for_a_call_never_seen_still_reaches_the_queue(run) -> None:
    """No saved row at all -- storage failed, or the ring raced the restart --
    so the caller is built from the speech result's own parameters."""
    async with run(twilio_holding()) as (_, client):
        await client.post(
            "/webhook/speech-result",
            data={**INCOMING, "CallSid": "CA-new", "SpeechResult": "Hello", "Confidence": "0.8"},
        )
        [call] = (await client.get("/api/calls")).json()

    assert (call["callId"], call["caller"]["number"], call["transcript"]) == (
        "CA-new",
        NUMBER,
        "Hello",
    )


async def test_a_finished_call_is_not_revived(run) -> None:
    """A late or retried speech result must not put a decided caller back."""
    async with run(twilio_holding()) as (_, client):
        await hold(client)
        await client.post("/api/calls/CA-held/reject")

    async with run(twilio_holding()) as (app, client):
        await client.post(
            "/webhook/speech-result", data={"CallSid": "CA-held", "SpeechResult": "Again"}
        )

        assert (await client.get("/api/calls")).json() == []
        assert saved(app, "CA-held").status == "rejected"


async def test_an_on_air_call_ending_after_the_restart_keeps_its_time(run) -> None:
    """On air through the restart: out of the queue, so swept as ended -- and
    then the host hangs up, and the dial result says how long they talked."""
    async with run(twilio_holding()) as (_, client):
        await hold(client)
        await client.post("/api/calls/CA-held/accept")

    async with run(twilio_holding()) as (app, client):
        accepted_at = saved(app, "CA-held").ended_at
        assert saved(app, "CA-held").status == "ended"
        await client.post(
            "/webhook/dial-complete",
            data={"CallSid": "CA-held", "DialCallStatus": "completed", "DialCallDuration": "312"},
        )
        call = saved(app, "CA-held")

    assert (call.status, call.on_air_seconds, call.was_accepted) == ("ended", 312, True)
    # As without a restart: an accepted call's end is when it was decided.
    assert call.ended_at == accepted_at


async def test_without_reconciliation_the_dial_result_still_closes_the_call(run) -> None:
    async with run(twilio_holding()) as (_, client):
        await hold(client)
        await client.post("/api/calls/CA-held/accept")

    async with run(twilio_holding(), RECONCILE_ON_STARTUP="false") as (app, client):
        await client.post(
            "/webhook/dial-complete",
            data={"CallSid": "CA-held", "DialCallStatus": "completed", "DialCallDuration": "45"},
        )
        call = saved(app, "CA-held")

    assert (call.status, call.on_air_seconds) == ("ended", 45)


class TestSweep:
    """Saved calls still open after a restart, that nobody came back for."""

    async def test_a_caller_twilio_no_longer_has_ended_during_the_restart(self, run) -> None:
        async with run(twilio_holding()) as (_, client):
            await hold(client, "CA-gone")
            await hold(client, "CA-still-holding")

        async with run(twilio_holding("CA-still-holding")) as (app, client):
            gone = saved(app, "CA-gone")
            history = (await client.get("/api/call-history")).json()["calls"]

        # Over, but nobody saw when -- and history can say exactly that.
        assert (gone.status, gone.ended_at) == ("ended", None)
        assert [c["callId"] for c in history] == ["CA-gone"]
        assert history[0]["endedAt"] is None

    async def test_nothing_is_swept_when_twilio_cannot_be_asked(self, run) -> None:
        """Then nobody knows who is still holding. Open rows hide nothing --
        history lists only finished calls -- so they wait for a run that can."""
        async with run(twilio_holding()) as (_, client):
            await hold(client)

        async with run(twilio_holding(down=True)) as (app, _):
            assert saved(app, "CA-held").status == "on-hold"

    async def test_without_reconciliation_the_sweep_runs_at_startup(self, run) -> None:
        async with run(twilio_holding()) as (_, client):
            await hold(client)

        async with run(twilio_holding("CA-held"), RECONCILE_ON_STARTUP="false") as (app, _):
            assert saved(app, "CA-held").status == "ended"


async def test_history_survives_a_restart(run) -> None:
    async with run(twilio_holding()) as (_, client):
        await hold(client)
        await client.post("/api/calls/CA-held/reject")
        await client.patch(f"/api/contacts/{NUMBER}", json={"name": "Pat"})

    async with run(twilio_holding()) as (_, client):
        [call] = (await client.get("/api/call-history")).json()["calls"]
        [contact] = (await client.get("/api/contacts")).json()

    assert (call["callId"], call["status"], call["transcript"]) == (
        "CA-held",
        "rejected",
        "About the guest",
    )
    assert call["caller"]["contact"]["name"] == "Pat"
    assert (contact["name"], contact["calls"]) == ("Pat", 1)
