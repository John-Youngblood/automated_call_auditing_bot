"""DEMO_MODE: simulated calls go all the way through, and nothing reaches Twilio.

Without it a simulated call cannot be accepted -- Twilio answers 404 for a SID
it never issued, and Accept reports that as a failure, correctly.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from tests.conftest import patch_twilio

INCOMING = {"CallSid": "CA-demo-1", "From": "+15035551234", "To": "+15039990000"}


def hold(client: TestClient, call_sid: str = "CA-demo-1") -> None:
    client.post("/webhook/incoming-call", data={**INCOMING, "CallSid": call_sid})
    client.post(
        "/webhook/speech-result",
        data={"CallSid": call_sid, "SpeechResult": "A question", "Confidence": "0.9"},
    )


@pytest.fixture
def demo(make_client, monkeypatch: pytest.MonkeyPatch):
    """A demo-mode client, with a Twilio that fails the test if it is called."""

    def unreachable(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"demo mode contacted Twilio: {request.method} {request.url}")

    patch_twilio(monkeypatch, httpx.MockTransport(unreachable))
    with make_client(DEMO_MODE="true", TWILIO_PHONE_NUMBER="+18185550123") as client:
        yield client


def test_a_simulated_call_can_be_accepted(demo: TestClient) -> None:
    hold(demo)

    assert demo.post("/api/calls/CA-demo-1/accept").status_code == 200
    assert demo.get("/api/calls/CA-demo-1").json()["status"] == "accepted"


def test_a_simulated_call_can_be_rejected(demo: TestClient) -> None:
    hold(demo)

    assert demo.post("/api/calls/CA-demo-1/reject").status_code == 200
    assert demo.get("/api/calls/CA-demo-1").json()["status"] == "rejected"


def test_closing_the_line_clears_simulated_callers(demo: TestClient) -> None:
    hold(demo, "CA-a")
    hold(demo, "CA-b")

    body = demo.post("/api/line/close").json()

    assert sorted(body["endedCallIds"]) == ["CA-a", "CA-b"]
    assert body["failedCallIds"] == []


def test_works_without_twilio_credentials(make_client) -> None:
    """The point of a demo: nothing to set up."""
    client = make_client(DEMO_MODE="true", TWILIO_ACCOUNT_SID="", TWILIO_API_KEY_SID="")
    with client:
        hold(client)
        assert client.post("/api/calls/CA-demo-1/accept").status_code == 200


def test_the_real_number_is_never_shown(demo: TestClient) -> None:
    """So a screen recording cannot give away the show's number."""
    with demo.websocket_connect("/ws/frontend") as ws:
        snapshot = ws.receive_json()

    assert snapshot["data"]["screeningNumber"] == "+15555551234"


def test_refuses_to_start_outside_local(configure_env) -> None:
    """Real callers would be left on hold while the dashboard said they were
    on air."""
    configure_env(
        APP_ENV="production",
        DEMO_MODE="true",
        HOST_PHONE_NUMBER="+15035551234",
        DASHBOARD_PASSWORD="a-long-enough-password",
        VALIDATE_WEBHOOK_SIGNATURE="true",
        TWILIO_AUTH_TOKEN="tok",
    )
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="DEMO_MODE"), TestClient(create_app()):
        pass
