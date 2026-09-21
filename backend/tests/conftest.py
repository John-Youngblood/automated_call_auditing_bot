"""Test fixtures.

Each test gets a freshly-constructed app, so call state never leaks between
tests. Settings are driven through the environment and the settings cache is
cleared around every test -- otherwise the first test to import the module
would pin configuration for the whole session.

Settings also read ../.env by default, which made the suite depend on whoever
ran it. That is not theoretical: adding HOLD_MUSIC_URL to a local .env broke
two assertions, and a TWILIO_ACCOUNT_SID in one sent the suite off to
api.twilio.com for real. `isolate_dotenv` cuts that link for every test.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from app.telephony.rest import TwilioRestClient

BASE_ENV = {
    "APP_ENV": "local",
    # Credentials so screening decisions reach the (stubbed) carrier. Without
    # these, accept and reject raise TelephonyUnavailable and every test about
    # queue behaviour would be a test about configuration instead.
    "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
    "TWILIO_API_KEY_SID": "SK" + "0" * 32,
    "TWILIO_API_KEY_SECRET": "test-secret",
    "LOG_LEVEL": "WARNING",
    "PUBLIC_BASE_URL": "https://calls.example.test",
    "VALIDATE_WEBHOOK_SIGNATURE": "false",
    "GREETING_AUDIO_URL": "",
}


@pytest.fixture(autouse=True)
def isolate_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build Settings from the environment only, never from a .env file."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture
def configure_env(monkeypatch: pytest.MonkeyPatch):
    """Apply base settings; return a callable to override individual keys."""

    def _apply(**overrides: str) -> None:
        for key, value in {**BASE_ENV, **overrides}.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    _apply()
    yield _apply
    get_settings.cache_clear()


def patch_twilio(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    """Point the REST client at ``transport`` instead of api.twilio.com."""
    original = TwilioRestClient.__aenter__

    async def entered(self: TwilioRestClient) -> TwilioRestClient:
        await original(self)
        self._client = httpx.AsyncClient(transport=transport)  # noqa: SLF001
        return self

    monkeypatch.setattr(TwilioRestClient, "__aenter__", entered)


@pytest.fixture(autouse=True)
def twilio(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A stand-in Twilio that accepts every command, and records the SIDs.

    Autouse, because almost every test here is about screening behaviour and
    should not also be a test of whether the carrier is reachable. Tests that
    care about Twilio *failing* call :func:`patch_twilio` with their own
    transport, which wins by being applied later.
    """
    commanded: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sid = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        if request.method == "POST":
            commanded.append(sid)
            return httpx.Response(200, json={"sid": sid, "status": "completed"})
        return httpx.Response(
            200,
            json={
                "sid": sid,
                "from": "+15035551234",
                "to": "+15039990000",
                "caller_name": None,
                "start_time": None,
                "status": "in-progress",
            },
        )

    patch_twilio(monkeypatch, httpx.MockTransport(handler))
    return commanded


@pytest.fixture
def client(configure_env) -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def make_client(configure_env):
    """Build a client after changing settings (e.g. a different provider)."""

    def _make(**overrides: str) -> TestClient:
        configure_env(**overrides)
        return TestClient(create_app())

    return _make
