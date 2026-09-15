"""The inbound-call webhook: what the provider gets back, and who gets queued."""

from __future__ import annotations

import json
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


def test_returns_twiml_that_plays_then_streams(client: TestClient) -> None:
    response = client.post("/webhook/incoming-call", data=TWILIO_FORM)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")

    root = fromstring(response.text)
    assert root.tag == "Response"
    # Order matters: <Connect> blocks until the stream ends, so the greeting
    # has to be queued before it.
    assert [child.tag for child in root] == ["Play", "Connect"]

    assert root.findtext("Play") == "https://calls.example.test/static/greeting.mp3"

    stream = root.find("Connect/Stream")
    assert stream is not None
    # wss, not ws -- the public base URL is https.
    assert stream.attrib["url"] == "wss://calls.example.test/ws/audio-stream"

    params = {p.attrib["name"]: p.attrib["value"] for p in stream.findall("Parameter")}
    assert params["callId"] == "CA0123456789"
    assert params["from"] == "+15551230000"


def test_call_enters_the_queue_with_caller_details(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data=TWILIO_FORM)

    calls = client.get("/api/calls").json()
    assert len(calls) == 1
    assert calls[0]["callId"] == "CA0123456789"
    assert calls[0]["status"] == "ringing"
    assert calls[0]["caller"]["number"] == "+15551230000"
    assert calls[0]["caller"]["city"] == "Portland"
    assert calls[0]["toNumber"] == "+15559990000"


def test_retried_webhook_does_not_duplicate_the_call(client: TestClient) -> None:
    for _ in range(3):
        assert client.post("/webhook/incoming-call", data=TWILIO_FORM).status_code == 200

    assert len(client.get("/api/calls").json()) == 1


def test_vonage_provider_answers_with_json_ncco(make_client) -> None:
    with make_client(TELEPHONY_PROVIDER="vonage") as client:
        response = client.post(
            "/webhook/incoming-call",
            json={"uuid": "vg-abc123", "from": "15551230000", "to": "15559990000"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")

    ncco = json.loads(response.text)
    assert [action["action"] for action in ncco] == ["stream", "connect"]
    endpoint = ncco[1]["endpoint"][0]
    assert endpoint["type"] == "websocket"
    assert endpoint["uri"] == "wss://calls.example.test/ws/audio-stream"
    assert endpoint["headers"]["callId"] == "vg-abc123"


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


def test_call_status_webhook_clears_the_queue(client: TestClient) -> None:
    client.post("/webhook/incoming-call", data=TWILIO_FORM)
    assert len(client.get("/api/calls").json()) == 1

    client.post(
        "/webhook/call-status",
        data={"CallSid": TWILIO_FORM["CallSid"], "CallStatus": "completed"},
    )
    assert client.get("/api/calls").json() == []
