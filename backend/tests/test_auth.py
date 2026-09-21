"""Dashboard auth: one shared password, exchanged for a session token.

The split under test: the dashboard surface is guarded, Twilio's webhooks are
not. Twilio carries no credentials of ours, so requiring a token there would
simply break every call.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

PASSWORD = "correct-horse-battery-staple"
GUARDED = {"DASHBOARD_PASSWORD": PASSWORD}


@pytest.fixture
def secured(make_client):
    client = make_client(**GUARDED)
    with client:
        yield client


def token(client: TestClient) -> str:
    return client.post("/api/session", json={"password": PASSWORD}).json()["token"]


class TestLogIn:
    def test_the_right_password_returns_a_token(self, secured: TestClient) -> None:
        response = secured.post("/api/session", json={"password": PASSWORD})

        assert response.status_code == 200
        assert len(response.json()["token"]) > 20

    def test_the_wrong_password_does_not(self, secured: TestClient) -> None:
        response = secured.post("/api/session", json={"password": "hunter2"})

        assert response.status_code == 401
        assert "token" not in response.json()

    def test_every_login_gets_a_different_token(self, secured: TestClient) -> None:
        assert token(secured) != token(secured)


class TestGuardedSurface:
    def test_the_api_is_closed_without_a_token(self, secured: TestClient) -> None:
        assert secured.get("/api/calls").status_code == 401

    def test_a_bogus_token_is_closed(self, secured: TestClient) -> None:
        headers = {"Authorization": "Bearer not-a-real-token"}
        assert secured.get("/api/calls", headers=headers).status_code == 401

    def test_a_real_token_opens_it(self, secured: TestClient) -> None:
        headers = {"Authorization": f"Bearer {token(secured)}"}
        assert secured.get("/api/calls", headers=headers).status_code == 200

    def test_logging_out_invalidates_the_token(self, secured: TestClient) -> None:
        headers = {"Authorization": f"Bearer {token(secured)}"}
        assert secured.get("/api/calls", headers=headers).status_code == 200

        secured.delete("/api/session", headers=headers)

        assert secured.get("/api/calls", headers=headers).status_code == 401


class TestWebhooksStayOpen:
    """Twilio has no token to send. Guarding these would break every call."""

    def test_an_incoming_call_needs_no_token(self, secured: TestClient) -> None:
        response = secured.post(
            "/webhook/incoming-call",
            data={"CallSid": "CA-auth", "From": "+15035551234", "To": "+15039990000"},
        )
        assert response.status_code == 200

    def test_the_status_callback_needs_no_token(self, secured: TestClient) -> None:
        response = secured.post(
            "/webhook/call-status", data={"CallSid": "CA-auth", "CallStatus": "completed"}
        )
        assert response.status_code == 204

    def test_the_liveness_probe_needs_no_token(self, secured: TestClient) -> None:
        """Cloud Run's probe carries no credentials either."""
        assert secured.get("/healthz").status_code == 200


class TestWebsocket:
    def test_it_is_refused_without_a_token(self, secured: TestClient) -> None:
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect), secured.websocket_connect("/ws/frontend"):
            pass

    def test_the_token_travels_as_a_subprotocol(self, secured: TestClient) -> None:
        """Not a query string: browsers cannot set headers on a WebSocket, and
        a token in the URL lands in every access log on the way."""
        with secured.websocket_connect(
            "/ws/frontend", subprotocols=["bearer", token(secured)]
        ) as ws:
            assert ws.receive_json()["type"] == "state.snapshot"


class TestOpenByDefault:
    def test_no_password_leaves_the_dashboard_open(self, client: TestClient) -> None:
        """Local convenience. main.py refuses to start like this elsewhere."""
        assert client.get("/api/calls").status_code == 200


class TestStartupGuard:
    def test_production_refuses_to_start_without_a_password(self, configure_env) -> None:
        from app.config import get_settings
        from app.main import create_app

        configure_env(APP_ENV="production", HOST_PHONE_NUMBER="+15035551234")
        get_settings.cache_clear()

        with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"), TestClient(create_app()):
            pass

    def test_production_refuses_a_short_password(self, configure_env) -> None:
        """A shared secret on a public URL is exactly as good as its length,
        and nothing here rate-limits guesses."""
        from app.config import get_settings
        from app.main import create_app

        configure_env(
            APP_ENV="production", HOST_PHONE_NUMBER="+15035551234", DASHBOARD_PASSWORD="short"
        )
        get_settings.cache_clear()

        with pytest.raises(RuntimeError, match="characters"), TestClient(create_app()):
            pass
