"""Opening and closing the line.

Closing does two things, always: turns new callers away, and hangs up on
anyone still holding. The service itself keeps running -- that is the
distinction that matters, because an unreachable webhook makes Twilio play a
caller a generic error, while a closed line tells them when to call back.
"""

from __future__ import annotations

from xml.etree.ElementTree import fromstring

import httpx
import pytest
from fastapi.testclient import TestClient

from app.telephony.rest import TwilioRestClient

INCOMING = {"CallSid": "CA-line-1", "From": "+15035551234", "To": "+15039990000"}

TWILIO_CREDENTIALS = {
    "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
    "TWILIO_API_KEY_SID": "SK" + "0" * 32,
    "TWILIO_API_KEY_SECRET": "secret",
}


@pytest.fixture
def twilio(monkeypatch: pytest.MonkeyPatch):
    """A Twilio that accepts every hang-up, so closing can actually clear."""
    hung_up: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sid = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        if request.method == "POST":
            hung_up.append(sid)
            return httpx.Response(200, json={"sid": sid, "status": "completed"})
        return httpx.Response(200, json={"sid": sid, "status": "in-progress"})

    transport = httpx.MockTransport(handler)
    original = TwilioRestClient.__aenter__

    async def entered(self: TwilioRestClient) -> TwilioRestClient:
        await original(self)
        self._client = httpx.AsyncClient(transport=transport)  # noqa: SLF001
        return self

    monkeypatch.setattr(TwilioRestClient, "__aenter__", entered)
    return hung_up


def call_in(client: TestClient, call_sid: str = "CA-line-1"):
    return client.post("/webhook/incoming-call", data={**INCOMING, "CallSid": call_sid})


class TestClosedLine:
    def test_callers_are_told_why_rather_than_queued(self, client: TestClient) -> None:
        client.post("/api/line/close")

        response = call_in(client)

        doc = fromstring(response.text)
        assert [child.tag for child in doc] == ["Say", "Hangup"]
        assert "not taking calls" in doc.find("Say").text
        # Never screened, so never queued.
        assert client.get("/api/calls").json() == []

    def test_a_turned_away_caller_does_not_enter_history(self, client: TestClient) -> None:
        """A closed line would otherwise fill history with rows nobody reads."""
        client.post("/api/line/close")
        call_in(client)

        assert client.get("/api/call-history").json() == []

    def test_the_message_is_configurable(self, make_client) -> None:
        client = make_client(CLOSED_LINE_MESSAGE="Back on Thursday.")
        with client:
            client.post("/api/line/close")
            response = call_in(client)

        assert fromstring(response.text).find("Say").text == "Back on Thursday."

    def test_closing_is_idempotent(self, client: TestClient) -> None:
        """Two operators clicking close a second apart must both succeed."""
        assert client.post("/api/line/close").json()["open"] is False
        assert client.post("/api/line/close").json()["open"] is False

    def test_reopening_accepts_calls_again(self, client: TestClient) -> None:
        client.post("/api/line/close")
        call_in(client, "CA-turned-away")

        client.post("/api/line/open")
        response = call_in(client, "CA-welcome")

        # Back to the greeting-and-listen document.
        assert fromstring(response.text).find("Gather") is not None
        assert [c["callId"] for c in client.get("/api/calls").json()] == ["CA-welcome"]


class TestClosingClearsTheQueue:
    """Closing is one action: no caller is left waiting on a line nobody is
    watching, whether they were mid-greeting or on hold."""

    def hold(self, client: TestClient, call_sid: str) -> None:
        client.post("/webhook/incoming-call", data={**INCOMING, "CallSid": call_sid})
        client.post(
            "/webhook/speech-result",
            data={"CallSid": call_sid, "SpeechResult": "A question", "Confidence": "0.9"},
        )

    def test_everyone_holding_is_hung_up(self, make_client, twilio) -> None:
        client = make_client(**TWILIO_CREDENTIALS)
        with client:
            self.hold(client, "CA-a")
            self.hold(client, "CA-b")

            body = client.post("/api/line/close").json()

            assert body["open"] is False
            assert sorted(body["endedCallIds"]) == ["CA-a", "CA-b"]
            assert body["failedCallIds"] == []
            assert client.get("/api/calls").json() == []
        assert sorted(twilio) == ["CA-a", "CA-b"]

    def test_an_empty_queue_closes_cleanly(self, make_client, twilio) -> None:
        client = make_client(**TWILIO_CREDENTIALS)
        with client:
            body = client.post("/api/line/close").json()

        assert body["open"] is False
        assert body["endedCallIds"] == []
        assert twilio == []

    def test_a_caller_we_could_not_reach_stays_visible(self, client: TestClient) -> None:
        """No REST credentials here, so every hang-up fails. The line still
        closes -- but those callers are still connected, so they keep their
        place rather than vanishing from a dashboard that has gone quiet."""
        self.hold(client, "CA-stuck")

        body = client.post("/api/line/close").json()

        assert body["open"] is False
        assert body["failedCallIds"] == ["CA-stuck"]
        assert [c["callId"] for c in client.get("/api/calls").json()] == ["CA-stuck"]


class TestDashboardsAreTold:
    def test_the_snapshot_carries_the_line_state(self, client: TestClient) -> None:
        """A dashboard opened while off air must not show 'on air'."""
        client.post("/api/line/close")

        with client.websocket_connect("/ws/frontend") as ws:
            snapshot = ws.receive_json()

        assert snapshot["data"]["lineOpen"] is False

    def test_a_change_is_broadcast_to_everyone(self, client: TestClient) -> None:
        """Two operators disagreeing about whether the show is taking calls is
        how someone gets put on air after it has ended."""
        with (
            client.websocket_connect("/ws/frontend") as first,
            client.websocket_connect("/ws/frontend") as second,
        ):
            first.receive_json()
            second.receive_json()

            client.post("/api/line/close")

            for ws in (first, second):
                event = ws.receive_json()
                assert event["type"] == "line.changed"
                assert event["data"]["lineOpen"] is False

    def test_closing_twice_broadcasts_once(self, client: TestClient) -> None:
        """Idempotent: a second operator clicking close must not look like a
        state change, nor like a failure to the person who clicked."""
        with client.websocket_connect("/ws/frontend") as ws:
            ws.receive_json()

            assert client.post("/api/line/close").json()["open"] is False
            assert client.post("/api/line/close").json()["open"] is False

            assert ws.receive_json()["type"] == "line.changed"
            client.post("/api/line/open")
            # The next event is the reopen, not a duplicate close.
            assert ws.receive_json()["data"]["lineOpen"] is True


class TestStartupState:
    def test_the_line_is_open_by_default(self, client: TestClient) -> None:
        """A deploy must not silently stop taking calls mid-show."""
        assert fromstring(call_in(client).text).find("Gather") is not None

    def test_it_can_come_up_closed(self, make_client) -> None:
        client = make_client(LINE_OPEN_ON_START="false")
        with client:
            response = call_in(client)

        assert fromstring(response.text).find("Say") is not None
