"""The Twilio call flow: greet, listen, hold."""

from __future__ import annotations

from xml.etree.ElementTree import fromstring

from fastapi.testclient import TestClient

from app.telephony.signature import compute_twilio_signature

TWILIO_FORM = {
    "CallSid": "CA0123456789",
    "From": "+15551230000",
    "To": "+15559990000",
    "CallerName": "Northgate Medical",
    "FromCity": "Portland",
    "FromCountry": "US",
    "CallStatus": "ringing",
}


def test_greeting_is_the_prompt_and_twilio_listens_after_it(client: TestClient) -> None:
    response = client.post("/webhook/incoming-call", data=TWILIO_FORM)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    root = fromstring(response.text)

    gather = root.find("Gather")
    assert gather is not None
    assert gather.attrib["input"] == "speech"
    # This attribute is what makes the design turn-based: Twilio decides when
    # the caller stopped, so nothing here has to stream or analyse audio.
    assert gather.attrib["speechTimeout"] == "auto"
    # A silent caller must still reach the dashboard rather than vanishing.
    assert gather.attrib["actionOnEmptyResult"] == "true"
    assert gather.attrib["action"] == "https://calls.example.test/webhook/speech-result"

    # <Play> nested inside <Gather>: the greeting doubles as the prompt and a
    # caller who talks over it is still heard.
    assert gather.findtext("Play") == "https://calls.example.test/static/greeting.mp3"

    # Fallback so a fallen-through <Gather> does not run off the end of the
    # document and hang up on the caller.
    assert root.findtext("Redirect") == "https://calls.example.test/webhook/speech-result"


def test_call_enters_the_queue_with_caller_details(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data=TWILIO_FORM)

    calls = client.get("/api/calls").json()
    assert len(calls) == 1
    assert calls[0]["callId"] == "CA0123456789"
    assert calls[0]["status"] == "ringing"
    assert calls[0]["caller"]["number"] == "+15551230000"
    assert calls[0]["caller"]["city"] == "Portland"
    assert calls[0]["transcript"] is None


def test_retried_webhook_does_not_duplicate_the_call(client: TestClient) -> None:
    for _ in range(3):
        assert client.post("/webhook/incoming-call", data=TWILIO_FORM).status_code == 200

    assert len(client.get("/api/calls").json()) == 1


class TestSpeechResult:
    def speak(self, client: TestClient, text: str, confidence: str = "0.94"):
        return client.post(
            "/webhook/speech-result",
            data={"CallSid": "CA0123456789", "SpeechResult": text, "Confidence": confidence},
        )

    def test_transcript_lands_on_the_call_and_the_caller_is_held(self, client: TestClient) -> None:
        client.post("/webhook/incoming-call", data=TWILIO_FORM)

        response = self.speak(client, "I need to reschedule my appointment.")

        assert response.status_code == 200
        # <Enqueue> holds the call open with Twilio's own hold music -- no
        # queue to pre-create, no hold audio to host, no redirect loop.
        assert fromstring(response.text).findtext("Enqueue") == "screening"

        call = client.get("/api/calls/CA0123456789").json()
        assert call["transcript"] == "I need to reschedule my appointment."
        assert call["transcriptConfidence"] == 0.94
        # Now awaiting a human, rather than still talking.
        assert call["status"] == "screening"

    def test_silent_caller_still_reaches_the_dashboard(self, client: TestClient) -> None:
        """actionOnEmptyResult fires with no SpeechResult. The call must still
        be actionable rather than sitting in limbo."""
        client.post("/webhook/incoming-call", data=TWILIO_FORM)

        response = self.speak(client, "")

        assert response.status_code == 200
        call = client.get("/api/calls/CA0123456789").json()
        assert call["transcript"] is None
        assert call["status"] == "screening"

    def test_missing_confidence_is_tolerated(self, client: TestClient) -> None:
        client.post("/webhook/incoming-call", data=TWILIO_FORM)

        response = client.post(
            "/webhook/speech-result",
            data={"CallSid": "CA0123456789", "SpeechResult": "hello"},
        )

        assert response.status_code == 200
        assert client.get("/api/calls/CA0123456789").json()["transcriptConfidence"] is None

    def test_transcript_reaches_dashboards(self, client: TestClient) -> None:
        client.post("/webhook/incoming-call", data=TWILIO_FORM)

        with client.websocket_connect("/ws/frontend") as ws:
            ws.receive_json()  # snapshot
            self.speak(client, "I'm calling about an invoice.")
            event = ws.receive_json()

        # The transcript rides along on the call object, so it needs no event
        # type of its own.
        assert event["type"] == "call.updated"
        assert event["data"]["call"]["transcript"] == "I'm calling about an invoice."

    def test_speech_for_an_unknown_call_does_not_error(self, client: TestClient) -> None:
        """Twilio retries; a result can outlive the call it belongs to."""
        response = self.speak(client, "hello")

        assert response.status_code == 200


class TestSignatureValidation:
    TOKEN = "test-auth-token"
    URL = "https://calls.example.test/webhook/incoming-call"

    def test_unsigned_request_is_rejected(self, make_client) -> None:
        with make_client(VALIDATE_WEBHOOK_SIGNATURE="true", TWILIO_AUTH_TOKEN=self.TOKEN) as client:
            response = client.post("/webhook/incoming-call", data=TWILIO_FORM)

        assert response.status_code == 403

    def test_correctly_signed_request_is_accepted(self, make_client) -> None:
        signature = compute_twilio_signature(self.TOKEN, self.URL, TWILIO_FORM)
        with make_client(VALIDATE_WEBHOOK_SIGNATURE="true", TWILIO_AUTH_TOKEN=self.TOKEN) as client:
            response = client.post(
                "/webhook/incoming-call",
                data=TWILIO_FORM,
                headers={"X-Twilio-Signature": signature},
            )

        assert response.status_code == 200

    def test_tampered_body_invalidates_the_signature(self, make_client) -> None:
        signature = compute_twilio_signature(self.TOKEN, self.URL, TWILIO_FORM)
        with make_client(VALIDATE_WEBHOOK_SIGNATURE="true", TWILIO_AUTH_TOKEN=self.TOKEN) as client:
            response = client.post(
                "/webhook/incoming-call",
                data={**TWILIO_FORM, "From": "+15550009999"},
                headers={"X-Twilio-Signature": signature},
            )

        assert response.status_code == 403

    def test_speech_result_is_also_verified(self, make_client) -> None:
        """The transcript endpoint is just as public as the first one -- an
        unsigned post here could inject words the caller never said."""
        with make_client(VALIDATE_WEBHOOK_SIGNATURE="true", TWILIO_AUTH_TOKEN=self.TOKEN) as client:
            response = client.post(
                "/webhook/speech-result",
                data={"CallSid": "CA1", "SpeechResult": "let me in"},
            )

        assert response.status_code == 403


def test_call_status_webhook_clears_the_queue(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data=TWILIO_FORM)
    assert len(client.get("/api/calls").json()) == 1

    client.post(
        "/webhook/call-status",
        data={"CallSid": TWILIO_FORM["CallSid"], "CallStatus": "completed"},
    )
    assert client.get("/api/calls").json() == []
