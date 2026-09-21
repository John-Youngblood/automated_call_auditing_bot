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

    #: Absolute URL of a single audio file to play on hold. Blank leaves
    #: <Enqueue> without a waitUrl, which gets Twilio's default classical
    #: playlist. Twilio loops whichever it is for as long as the caller waits.
    hold_music_url: str = ""

    #: Whether the line accepts calls when the process starts. True keeps a
    #: restart invisible mid-show, which is the case that matters most -- a
    #: deploy must not silently stop taking calls while you are on air. Set
    #: false for a deployment that should come up off-air and be opened by
    #: hand. Not persisted either way; this is the only source of truth at boot.
    line_open_on_start: bool = True

    #: Spoken to anyone who calls while the line is closed. Tells them when to
    #: try again rather than leaving them with a busy signal they will read as
    #: a broken number.
    closed_line_message: str = (
        "Thanks for calling. We are not taking calls right now. "
        "Please try again during the next live show."
    )

    #: Spoken to anyone still holding when an operator clears the queue. They
    #: have been waiting to get on air, so they are told rather than dropped.
    closing_message: str = (
        "Thanks for calling. The show has ended for tonight, so we are closing the line. "
        "Please call back next time."
    )
    validate_webhook_signature: bool = False

    #: Signature verification is HMAC'd with the *auth token* specifically --
    #: an API key secret will not validate. Kept separate from the REST
    #: credentials below so rotating one does not break the other.
    twilio_auth_token: str = ""

    # --- Twilio REST -------------------------------------------------------
    #: Credentials for talking *to* Twilio. An API key is preferred over the
    #: auth token because it can be revoked on its own; leave the key blank to
    #: fall back to account SID + auth token.
    twilio_account_sid: str = ""
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: str = ""

    #: Short on purpose. Every REST call here happens off the webhook path, so
    #: a slow Twilio should make us give up and log rather than pile up.
    twilio_api_timeout_seconds: float = Field(default=5.0, gt=0, le=30)

    #: On boot, ask Twilio who is still holding in the queue and put them back
    #: on the dashboard. Without it a restart mid-show strands every waiting
    #: caller: Twilio keeps playing them hold music while no operator can see
    #: them. See app/services/reconcile.py for what can and cannot be
    #: recovered. Needs the REST credentials above; skipped silently without.
    reconcile_on_startup: bool = True

    #: Where an accepted call is bridged to. A real deployment would look this
    #: up per operator rather than using one station number.
    agent_forward_number: str = "+15550000000"

    # --- Call history ------------------------------------------------------
    #: Finished calls kept in memory, and the default page size of the history
    #: endpoint. One number because there is nowhere else for a call to live:
    #: retaining more than is served, or serving more than is retained, would
    #: both be fiction. History is lost on restart by design -- see
    #: app.services.call_registry.
    call_history_size: int = Field(default=200, ge=1, le=1000)

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
