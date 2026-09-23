"""Accepting and rejecting a caller -- the half that reaches the caller.

The regression these exist for: reject used to be a logging stub, so a
declined caller stayed in Twilio's hold queue hearing music while REJECTED
took them off the dashboard. Stranded and invisible, in a way closing the line
would not clear, because that only walks *open* calls.
"""

from __future__ import annotations

from urllib.parse import parse_qs
from xml.etree.ElementTree import fromstring

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import patch_twilio

INCOMING = {"CallSid": "CA-dec-1", "From": "+15035551234", "To": "+15039990000"}


def hold(client: TestClient, call_sid: str = "CA-dec-1") -> None:
    client.post("/webhook/incoming-call", data={**INCOMING, "CallSid": call_sid})
    client.post(
        "/webhook/speech-result",
        data={"CallSid": call_sid, "SpeechResult": "A question", "Confidence": "0.9"},
    )


def sent_twiml(recorded: list[tuple[str, str]], call_sid: str) -> str:
    body = next(b for sid, b in recorded if sid == call_sid)
    return parse_qs(body)["Twiml"][0]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Captures the TwiML sent to each call, so we can read what was said."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sid = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        if request.method == "POST":
            seen.append((sid, request.content.decode()))
            return httpx.Response(200, json={"sid": sid, "status": "completed"})
        return httpx.Response(200, json={"sid": sid, "status": "in-progress"})

    patch_twilio(monkeypatch, httpx.MockTransport(handler))
    return seen


class TestReject:
    def test_the_caller_is_told_and_hung_up(self, client: TestClient, recorder) -> None:
        hold(client)

        assert client.post("/api/calls/CA-dec-1/reject").status_code == 200

        doc = fromstring(sent_twiml(recorder, "CA-dec-1"))
        assert [child.tag for child in doc] == ["Say", "Hangup"]
        assert "not able to take you on air" in doc.find("Say").text

    def test_a_recording_replaces_the_spoken_fallback(
        self, make_client, recorder, static_dir
    ) -> None:
        (static_dir / "sorry.mp3").touch()
        client = make_client(REJECT_AUDIO_URL="sorry.mp3")
        with client:
            hold(client)
            client.post("/api/calls/CA-dec-1/reject")

        doc = fromstring(sent_twiml(recorder, "CA-dec-1"))
        assert [child.tag for child in doc] == ["Play", "Hangup"]
        assert doc.find("Play").text == "https://calls.example.test/static/sorry.mp3"

    def test_a_listed_recording_that_is_missing_is_spoken_instead(
        self, make_client, recorder
    ) -> None:
        """Better the text than a <Play> of a 404, which Twilio skips."""
        client = make_client(REJECT_AUDIO_URL="not-there.mp3")
        with client:
            hold(client)
            client.post("/api/calls/CA-dec-1/reject")

        doc = fromstring(sent_twiml(recorder, "CA-dec-1"))
        assert [child.tag for child in doc] == ["Say", "Hangup"]

    def test_a_failure_leaves_the_call_on_the_dashboard(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point. If we cannot reach the caller they are still
        holding, so REJECTED must not be recorded -- that would take them off
        the dashboard while they sit in hold music."""
        hold(client)
        patch_twilio(
            monkeypatch, httpx.MockTransport(lambda r: httpx.Response(401, text="nope"))
        )

        assert client.post("/api/calls/CA-dec-1/reject").status_code == 502

        assert [c["callId"] for c in client.get("/api/calls").json()] == ["CA-dec-1"]
        assert client.get("/api/calls/CA-dec-1").json()["status"] == "on-hold"

    def test_no_credentials_is_a_503_not_a_silent_success(self, make_client) -> None:
        client = make_client(TWILIO_ACCOUNT_SID="", TWILIO_API_KEY_SID="")
        with client:
            hold(client)
            response = client.post("/api/calls/CA-dec-1/reject")

            assert response.status_code == 503
            assert "credentials" in response.json()["detail"]
            assert client.get("/api/calls/CA-dec-1").json()["status"] == "on-hold"


class TestAccept:
    def test_the_caller_is_dialled_to_the_operator(self, make_client, recorder) -> None:
        client = make_client(HOST_PHONE_NUMBER="+15035559999")
        with client:
            hold(client)
            assert client.post("/api/calls/CA-dec-1/accept").status_code == 200

        doc = fromstring(sent_twiml(recorder, "CA-dec-1"))
        assert [child.tag for child in doc] == ["Dial"]
        assert doc.find("Dial").text == "+15035559999"

    def test_a_call_that_already_ended_is_a_failure(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Strict where reject is lenient: a 404 means nobody is being put on
        air, and showing the operator a live guest who hung up is worse than
        showing them an error."""
        hold(client)
        patch_twilio(monkeypatch, httpx.MockTransport(lambda r: httpx.Response(404, json={})))

        assert client.post("/api/calls/CA-dec-1/accept").status_code == 502
        assert client.get("/api/calls/CA-dec-1").json()["status"] == "on-hold"


class TestWebsocketPath:
    """This branch used to skip the carrier entirely -- it set the status and
    left a TODO where the provider call should have been."""

    def test_reject_over_the_socket_reaches_twilio(self, client: TestClient, twilio) -> None:
        hold(client)

        with client.websocket_connect("/ws/frontend") as ws:
            ws.receive_json()
            ws.send_json({"type": "call.reject", "callId": "CA-dec-1"})
            event = ws.receive_json()

        assert event["type"] == "call.ended"
        assert twilio == ["CA-dec-1"]

    def test_a_failure_reports_an_error_and_keeps_the_call(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hold(client)
        patch_twilio(
            monkeypatch, httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
        )

        with client.websocket_connect("/ws/frontend") as ws:
            ws.receive_json()
            ws.send_json({"type": "call.reject", "callId": "CA-dec-1"})
            event = ws.receive_json()

        assert event["type"] == "error"
        assert "still be holding" in event["data"]["message"]
        assert client.get("/api/calls/CA-dec-1").json()["status"] == "on-hold"


class TestHostNumberGuard:
    """A misconfigured host number is silent in the worst way: Accept
    succeeds, the dashboard says the caller is on air, and Twilio dials a
    number that goes nowhere."""

    def test_production_refuses_to_start_on_the_placeholder(self, configure_env) -> None:
        from app.config import get_settings
        from app.main import PLACEHOLDER_HOST_NUMBER, create_app

        configure_env(
            APP_ENV="production",
            HOST_PHONE_NUMBER=PLACEHOLDER_HOST_NUMBER,
            DASHBOARD_PASSWORD="a-long-enough-password",
        )
        get_settings.cache_clear()

        with pytest.raises(RuntimeError, match="HOST_PHONE_NUMBER"), TestClient(create_app()):
            pass

    def test_local_only_warns(self, configure_env) -> None:
        """Local has to keep working -- nobody sets a real phone number to
        click around the dashboard."""
        from app.config import get_settings
        from app.main import PLACEHOLDER_HOST_NUMBER, create_app

        configure_env(APP_ENV="local", HOST_PHONE_NUMBER=PLACEHOLDER_HOST_NUMBER)
        get_settings.cache_clear()

        with TestClient(create_app()) as client:
            assert client.get("/api/calls").status_code == 200

    def test_a_real_number_starts_anywhere(self, configure_env) -> None:
        from app.config import get_settings
        from app.main import create_app

        configure_env(
            APP_ENV="production",
            HOST_PHONE_NUMBER="+15035551234",
            DASHBOARD_PASSWORD="a-long-enough-password",
            VALIDATE_WEBHOOK_SIGNATURE="true",
            TWILIO_AUTH_TOKEN="tok",
        )
        get_settings.cache_clear()

        with TestClient(create_app()) as client:
            assert client.get("/healthz").status_code == 200


class TestDialComplete:
    """Twilio reports how the bridge to the host went. This is the only place
    the on-air outcome exists -- nothing else tells us."""

    def dial_result(self, client: TestClient, status: str, seconds: str = "0"):
        return client.post(
            "/webhook/dial-complete",
            data={
                "CallSid": "CA-dec-1",
                "DialCallStatus": status,
                "DialCallDuration": seconds,
            },
        )

    def accepted(self, client: TestClient) -> None:
        hold(client)
        client.post("/api/calls/CA-dec-1/accept")

    def test_a_finished_conversation_ends_the_call(self, client: TestClient) -> None:
        """ACCEPTED means "on air right now", so once the bridge is over it
        has to stop saying that -- whichever end hung up first."""
        self.accepted(client)
        assert client.get("/api/calls/CA-dec-1").json()["status"] == "accepted"

        assert self.dial_result(client, "completed", "184").status_code == 200

        assert client.get("/api/calls/CA-dec-1").json()["status"] == "ended"

    def test_a_busy_host_corrects_the_record(self, client: TestClient) -> None:
        """The host was already on air, so this caller never got through --
        ACCEPTED would be the dashboard claiming something that did not
        happen."""
        self.accepted(client)
        assert client.get("/api/calls/CA-dec-1").json()["status"] == "accepted"

        self.dial_result(client, "busy")

        assert client.get("/api/calls/CA-dec-1").json()["status"] == "ended"

    @pytest.mark.parametrize("status", ["no-answer", "failed", "canceled"])
    def test_every_non_connecting_outcome_is_corrected(
        self, client: TestClient, status: str
    ) -> None:
        self.accepted(client)

        self.dial_result(client, status)

        assert client.get("/api/calls/CA-dec-1").json()["status"] == "ended"

    def test_the_caller_is_hung_up_on_explicitly(self, client: TestClient) -> None:
        """Load-bearing: an action URL on <Dial> keeps the caller's leg alive
        and hands control back here, so after the host hangs up that caller is
        still connected to silence until we say otherwise."""
        self.accepted(client)

        response = self.dial_result(client, "completed")

        assert [child.tag for child in fromstring(response.text)] == ["Hangup"]

    def test_an_unknown_call_is_not_an_error(self, client: TestClient) -> None:
        """Twilio retries and late deliveries outlive a pruned registry."""
        assert self.dial_result(client, "completed").status_code == 200


def test_accept_tells_twilio_where_to_report(client: TestClient, recorder) -> None:
    hold(client)
    client.post("/api/calls/CA-dec-1/accept")

    doc = fromstring(sent_twiml(recorder, "CA-dec-1"))
    assert doc.find("Dial").attrib["action"] == (
        "https://calls.example.test/webhook/dial-complete"
    )


def test_the_caller_hanging_up_also_ends_an_accepted_call(client: TestClient) -> None:
    """The other direction. If the caller hangs up first the whole call ends,
    which reaches us as the number's status callback rather than the dial
    action -- two independent signals for the same fact."""
    hold(client)
    client.post("/api/calls/CA-dec-1/accept")

    client.post(
        "/webhook/call-status", data={"CallSid": "CA-dec-1", "CallStatus": "completed"}
    )

    assert client.get("/api/calls/CA-dec-1").json()["status"] == "ended"


def test_a_rejection_survives_the_caller_hanging_up(client: TestClient) -> None:
    """REJECTED does not get overwritten: we hung up on them, so the call
    ending afterwards is a consequence of that decision, and "rejected" is the
    thing an operator needs to see in history."""
    hold(client)
    client.post("/api/calls/CA-dec-1/reject")

    client.post(
        "/webhook/call-status", data={"CallSid": "CA-dec-1", "CallStatus": "completed"}
    )

    assert client.get("/api/calls/CA-dec-1").json()["status"] == "rejected"


class TestOnAirRecord:
    """`ended` is where every finished call lands, so it alone cannot say
    whether a caller made it on air."""

    def finish(self, client: TestClient, sid: str, **dial: str) -> None:
        client.post("/webhook/incoming-call", data={**INCOMING, "CallSid": sid})
        client.post(
            "/webhook/speech-result",
            data={"CallSid": sid, "SpeechResult": "Hi", "Confidence": "0.9"},
        )
        client.post(f"/api/calls/{sid}/accept")
        if dial:
            client.post("/webhook/dial-complete", data={"CallSid": sid, **dial})

    def test_a_conversation_records_its_length(self, client: TestClient) -> None:
        self.finish(client, "CA-talked", DialCallStatus="completed", DialCallDuration="212")

        call = client.get("/api/calls/CA-talked").json()
        assert call["status"] == "ended"
        assert call["wasAccepted"] is True
        assert call["onAirSeconds"] == 212

    def test_a_bridge_that_never_connected_has_no_duration(self, client: TestClient) -> None:
        """None here is meaningful, not missing -- it is how 'the host was
        already on a call' is told apart from a very short conversation."""
        self.finish(client, "CA-busy", DialCallStatus="busy")

        call = client.get("/api/calls/CA-busy").json()
        assert call["status"] == "ended"
        assert call["wasAccepted"] is True
        assert call["onAirSeconds"] is None

    def test_a_caller_who_was_never_accepted_is_not_marked(self, client: TestClient) -> None:
        hold(client, "CA-gave-up")
        client.post(
            "/webhook/call-status", data={"CallSid": "CA-gave-up", "CallStatus": "completed"}
        )

        call = client.get("/api/calls/CA-gave-up").json()
        assert call["status"] == "ended"
        assert call["wasAccepted"] is False
        assert call["onAirSeconds"] is None

    def test_a_garbled_duration_does_not_break_the_hangup(self, client: TestClient) -> None:
        self.finish(client, "CA-odd", DialCallStatus="completed", DialCallDuration="not-a-number")

        call = client.get("/api/calls/CA-odd").json()
        assert call["status"] == "ended"
        assert call["onAirSeconds"] is None
