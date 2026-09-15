"""Test fixtures.

Each test gets a freshly-constructed app, so call state never leaks between
tests. Settings are driven through the environment and the settings cache is
cleared around every test -- otherwise the first test to import the module
would pin configuration for the whole session.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app

BASE_ENV = {
    "APP_ENV": "local",
    # In-memory SQLite, so every test gets an empty blocklist and empty call
    # history with no files to clean up between runs.
    "DATABASE_URL": "sqlite+aiosqlite://",
    "LOG_LEVEL": "WARNING",
    "PUBLIC_BASE_URL": "https://calls.example.test",
    "VALIDATE_WEBHOOK_SIGNATURE": "false",
    "GREETING_AUDIO_URL": "",
}


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
