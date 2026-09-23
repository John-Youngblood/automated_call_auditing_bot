"""Typed settings, loaded once from the environment.

The only module that reads the environment. Import :func:`get_settings`,
never construct ``Settings`` -- the cache keeps one object per process.

Each setting is documented for operators in ``.env.example``; the notes here
are only for things that would be a bug to get wrong in code.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Bundled audio, served at /static by main.py.
_STATIC_DIR = Path(__file__).parent / "static"


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

    #: This backend's public origin, as Twilio sees it. Every callback URL is
    #: built from it, so "localhost" only works against the simulator.
    public_base_url: str = "http://localhost:8000"

    # --- What the caller hears ---------------------------------------------
    # Each prompt is a pair: audio if set, else the text spoken in TTS_VOICE.
    # Audio takes an absolute URL or a filename under app/static (_audio_url).
    tts_voice: str = "Polly.Joanna"

    #: Plays inside <Gather>, so it is also the prompt -- it has to ask the
    #: caller to state their reason.
    greeting_audio_url: str = ""
    greeting_message: str = (
        "Thanks for calling the show. After the beep, tell us your name and "
        "what you would like to talk about, then stay on the line."
    )

    #: Blank means no waitUrl, which gets Twilio's own classical playlist.
    hold_music_url: str = ""

    reject_audio_url: str = ""
    reject_message: str = (
        "Thanks for calling. We are not able to take you on air this time. "
        "Please do try us again on the next show."
    )

    closed_line_audio_url: str = ""
    closed_line_message: str = (
        "Thanks for calling. We are not taking calls right now. "
        "Please try again during the next live show."
    )

    closing_audio_url: str = ""
    closing_message: str = (
        "Thanks for calling. The show has ended for tonight, so we are closing the line. "
        "Please call back next time."
    )

    # --- Call flow ---------------------------------------------------------
    #: "phone_call" is tuned for 8kHz telephony; the default model is trained
    #: on wideband and does noticeably worse down a phone line.
    speech_model: str = "phone_call"
    speech_language: str = "en-US"

    #: Seconds of silence before Twilio decides the caller has finished. Never
    #: "auto" -- see app/telephony/twiml.py.
    speech_timeout_seconds: int = Field(default=3, ge=1, le=60)

    #: Created on demand by <Enqueue>; nothing to set up in the console.
    hold_queue_name: str = "screening"

    #: Open on start, so a deploy cannot silently take the show off air.
    line_open_on_start: bool = True

    #: The host's phone. One number by design: one host, one on-air slot. The
    #: default is a placeholder main.py refuses to start on outside local.
    host_phone_number: str = "+15550000000"

    #: The number listeners dial, shown in the dashboard header so an operator
    #: can read it out on air. Display only -- nothing routes on it, and Twilio
    #: reports the dialled number per call as ``to_number``. Blank hides it.
    twilio_phone_number: str = ""

    #: Shared password for the dashboard. Blank leaves it open, which
    #: main.py only tolerates locally. Exchanged for a session token; see
    #: app/services/sessions.py.
    dashboard_password: str = ""

    # --- Twilio credentials ------------------------------------------------
    validate_webhook_signature: bool = False

    #: Signature verification is HMAC'd with the auth token specifically -- an
    #: API key secret will not validate. Separate from the REST credentials so
    #: rotating one does not break the other.
    twilio_auth_token: str = ""

    #: For talking *to* Twilio. An API key is preferred because it can be
    #: revoked alone; blank falls back to account SID + auth token.
    twilio_account_sid: str = ""
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: str = ""

    #: Short on purpose: these calls are off the webhook path, so a slow
    #: Twilio should make us give up and log rather than pile up.
    twilio_api_timeout_seconds: float = Field(default=5.0, gt=0, le=30)

    #: On boot, rebuild the queue from Twilio -- see services/reconcile.py.
    reconcile_on_startup: bool = True

    # --- Dashboard ---------------------------------------------------------
    #: Finished calls kept in memory, and the history endpoint's page size.
    #: One number: serving more than is retained would be fiction.
    call_history_size: int = Field(default=200, ge=1, le=1000)

    #: Per-dashboard outbound buffer; overflow drops oldest.
    frontend_queue_max: int = Field(default=250, ge=1)
    cors_allow_origins: str = "http://localhost:5173"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    def _audio_url(self, configured: str, bundled: str = "") -> str:
        """Absolute URL for something the caller will hear.

        Takes an absolute URL, or a bare filename served from app/static. The
        filename form matters in development: PUBLIC_BASE_URL is a tunnel
        hostname that rotates and .env cannot interpolate, so a pasted URL goes
        stale on every restart. A filename is rebuilt against the current base.
        """
        target = configured.strip() or bundled
        if not target:
            return ""
        if "://" in target:
            return target
        return f"{self.public_base_url.rstrip('/')}/static/{target.lstrip('/')}"

    @property
    def resolved_greeting_url(self) -> str:
        """Configured greeting, else the bundled recording if it is on disk.

        Returning "" falls through to speaking ``greeting_message``, which is a
        better failure than <Play> pointing at a 404.
        """
        if not self.greeting_audio_url and (_STATIC_DIR / "greeting.mp3").is_file():
            return self._audio_url("", "greeting.mp3")
        return self._audio_url(self.greeting_audio_url)

    @property
    def resolved_hold_music_url(self) -> str:
        """No bundled default, unlike the greeting: blank gets Twilio's own
        playlist, which beats a <Play> pointing at a file that may not exist."""
        return self._audio_url(self.hold_music_url)

    @property
    def resolved_reject_audio_url(self) -> str:
        return self._audio_url(self.reject_audio_url)

    @property
    def resolved_closed_line_audio_url(self) -> str:
        return self._audio_url(self.closed_line_audio_url)

    @property
    def resolved_closing_audio_url(self) -> str:
        return self._audio_url(self.closing_audio_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Tests clear it via ``cache_clear``."""
    return Settings()
