"""Typed application settings, loaded once from the environment.

Everything environment-dependent is funnelled through here so no other module
has to reach for ``os.environ``. Import :func:`get_settings`, never construct
``Settings`` directly -- the cache keeps the object identical across requests.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---------------------------------------------------------------
    app_env: Literal["local", "staging", "production"] = "local"
    log_level: str = "INFO"

    #: Public origin of this backend as the telecom provider sees it. Used to
    #: build absolute callback URLs -- providers dial in from the internet, so
    #: "localhost" only works when they are mocked.
    public_base_url: str = "http://localhost:8000"

    # --- Telephony ---------------------------------------------------------
    greeting_audio_url: str = ""

    #: Twilio's speech model and language for <Gather input="speech">.
    #: "phone_call" is tuned for 8kHz telephony audio; the default model is
    #: trained on wideband and does noticeably worse down a phone line.
    speech_model: str = "phone_call"
    speech_language: str = "en-US"

    #: Seconds of silence before Twilio decides the caller has finished.
    #: Must be a positive integer, NOT "auto": Twilio warns (error 13335) if
    #: speechTimeout="auto" is combined with a speechModel. A few seconds also
    #: suits this use case better -- "auto" stops at the *first* pause, which
    #: truncates a caller mid-explanation.
    speech_timeout_seconds: int = Field(default=3, ge=1, le=60)

    #: Twilio queue callers wait in while an operator reads their transcript.
    #: Created on demand -- nothing to set up in the console.
    hold_queue_name: str = "screening"
    validate_webhook_signature: bool = False
    twilio_auth_token: str = ""

    #: Where an accepted call is bridged to. A real deployment would look this
    #: up per operator rather than using one station number.
    agent_forward_number: str = "+15550000000"

    # --- Moderation --------------------------------------------------------
    #: Async driver on purpose -- a sync DB call would block the event loop
    #: that is also pumping call audio.
    database_url: str = "sqlite+aiosqlite:///./call_screener.db"
    database_echo: bool = False

    #: How a blocked caller is turned away. "rejected" plays a
    #: not-accepting-calls treatment; "busy" returns a busy signal, which looks
    #: like an ordinary failed call rather than a deliberate block.
    blocked_call_reject_reason: Literal["rejected", "busy"] = "rejected"

    #: Rows returned by the call-history view.
    call_history_page_size: int = Field(default=100, ge=1, le=500)

    # --- Dashboard fan-out -------------------------------------------------
    frontend_queue_max: int = Field(default=250, ge=1)
    cors_allow_origins: str = "http://localhost:5173"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def websocket_base_url(self) -> str:
        """``public_base_url`` with the scheme swapped to ws/wss.

        The provider connects to this for the media stream, so the scheme has
        to match the HTTP one: https -> wss, http -> ws.
        """
        base = self.public_base_url.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :]
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :]
        return base

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_greeting_url(self) -> str:
        """Configured greeting, else the placeholder served by this backend."""
        if self.greeting_audio_url:
            return self.greeting_audio_url
        return f"{self.public_base_url.rstrip('/')}/static/greeting.mp3"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cleared in tests via ``cache_clear``)."""
    return Settings()
