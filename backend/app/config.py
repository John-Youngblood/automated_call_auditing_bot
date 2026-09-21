"""Typed application settings, loaded once from the environment.

Everything environment-dependent is funnelled through here so no other module
has to reach for ``os.environ``. Import :func:`get_settings`, never construct
``Settings`` directly -- the cache keeps the object identical across requests.
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

    #: Public origin of this backend as the telecom provider sees it. Used to
    #: build absolute callback URLs -- providers dial in from the internet, so
    #: "localhost" only works when they are mocked.
    public_base_url: str = "http://localhost:8000"

    # --- What the caller hears ---------------------------------------------
    # Every prompt below is a pair: an audio file, and text to fall back on.
    # A recording wins when one is set; otherwise Twilio speaks the text in
    # TTS_VOICE. That means a fresh deployment says something sensible with
    # nothing recorded, and a produced show can replace each line one at a
    # time without touching code.
    #
    # Audio settings take an absolute URL or a bare filename served from
    # app/static/ -- see _audio_url.

    #: Twilio text-to-speech voice for every spoken fallback. Amazon Polly
    #: names (Polly.Joanna, Polly.Matthew, ...) or Twilio's basic man/woman.
    #: One setting, not one per prompt: a show that speaks in two different
    #: synthetic voices sounds broken rather than varied.
    tts_voice: str = "Polly.Joanna"

    #: The greeting, which is also the *prompt* -- it plays inside <Gather>,
    #: so whatever is said here has to ask the caller to state their reason.
    greeting_audio_url: str = ""
    greeting_message: str = (
        "Thanks for calling the show. After the beep, tell us your name and "
        "what you would like to talk about, then stay on the line."
    )

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

    #: Played to anyone who calls while the line is closed. Tells them when to
    #: try again rather than leaving them with a busy signal they will read as
    #: a broken number.
    closed_line_audio_url: str = ""
    closed_line_message: str = (
        "Thanks for calling. We are not taking calls right now. "
        "Please try again during the next live show."
    )

    #: Played to a caller an operator decides not to put on air. Without this
    #: a rejected caller hears nothing at all and holds until they give up --
    #: invisible on the dashboard, and still being billed.
    reject_audio_url: str = ""
    reject_message: str = (
        "Thanks for calling. We are not able to take you on air this time. "
        "Please do try us again on the next show."
    )

    #: Played to anyone still holding when an operator closes the line. They
    #: have been waiting to get on air, so they are told rather than dropped.
    closing_audio_url: str = ""
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

    #: The host's phone. Accepting a caller dials this number and bridges
    #: them to it. One number by design -- there is one host and one on-air
    #: slot -- which is why this is a setting and not a lookup.
    #:
    #: The default is an obviously-fake placeholder rather than a blank,
    #: because a blank <Dial> is a TwiML error the caller hears. The lifespan
    #: refuses to start on it outside local.
    host_phone_number: str = "+15550000000"

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

    def _audio_url(self, configured: str, bundled: str = "") -> str:
        """Absolute URL for something the caller will hear.

        Accepts either an absolute URL or a bare filename served from this
        backend's static directory. The second form is the one that matters in
        development: PUBLIC_BASE_URL is a tunnel hostname that rotates, and
        .env does not interpolate, so an absolute URL pasted in there goes
        stale every time the tunnel restarts. A filename is rebuilt against
        the current base on every request.
        """
        target = configured.strip() or bundled
        if not target:
            return ""
        if "://" in target:
            return target
        return f"{self.public_base_url.rstrip('/')}/static/{target.lstrip('/')}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_greeting_url(self) -> str:
        """Configured greeting, else the recording bundled with this backend.

        Falls back to the bundled recording only when it is actually on disk.
        If someone removes it, this returns "" and the greeting is spoken from
        ``greeting_message`` instead -- a synthesised prompt is a far better
        failure than <Play> pointing at a 404, which is dead air.
        """
        if not self.greeting_audio_url and (_STATIC_DIR / "greeting.mp3").is_file():
            return self._audio_url("", "greeting.mp3")
        return self._audio_url(self.greeting_audio_url)

    @property
    def resolved_reject_audio_url(self) -> str:
        return self._audio_url(self.reject_audio_url)

    @property
    def resolved_closed_line_audio_url(self) -> str:
        return self._audio_url(self.closed_line_audio_url)

    @property
    def resolved_closing_audio_url(self) -> str:
        return self._audio_url(self.closing_audio_url)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_hold_music_url(self) -> str:
        """Configured hold music, or nothing.

        Deliberately has no bundled default, which is where it differs from
        the greeting: with nothing set, <Enqueue> omits waitUrl entirely and
        Twilio plays its own classical playlist. Falling back to a file that
        might not exist would turn pleasant default music into a failed fetch.
        """
        return self._audio_url(self.hold_music_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cleared in tests via ``cache_clear``)."""
    return Settings()
