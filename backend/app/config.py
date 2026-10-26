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

#: Where bare audio filenames live. main.py serves it at /static.
STATIC_DIR = Path(__file__).parent / "static"

#: Shown in the dashboard header in demo mode instead of TWILIO_PHONE_NUMBER,
#: so a recording never shows the real number.
DEMO_PHONE_NUMBER = "+15555551234"


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

    #: Played before <Dial>, so the host's phone only starts ringing once it
    #: ends -- keep it short, and promise no exact timing.
    accepted_audio_url: str = ""
    accepted_message: str = "You've been accepted, connecting you now."

    reject_audio_url: str = ""
    reject_message: str = (
        "Thanks for calling. We are not able to take you on air this time. "
        "Please do try us again on the next show."
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

    #: Most callers on hold at once; Twilio drops anyone past it. Applied to
    #: the queue at startup (see services/hold_queue.py). 1000 is Twilio's own
    #: default for a new queue, 5000 its ceiling; 200 keeps the bill in check.
    hold_queue_max_size: int = Field(default=200, ge=1, le=5000)

    #: Refuse calls whose caller ID the carrier signed but Twilio could not
    #: verify (StirVerstat ``TN-Validation-Failed*``) -- usually spoofed, the
    #: way floods are placed. Twilio warns it can also mean a certificate it
    #: failed to fetch, so this can turn away a real listener; hence a switch.
    reject_failed_caller_id: bool = True

    #: Longest a caller may hold before hearing the reject message and being
    #: hung up. 0 disables it, leaving only Twilio's own 4-hour cap on any call.
    #: Checked each time a hold track finishes, so it can run one track over.
    max_hold_minutes: int = Field(default=30, ge=0)

    #: Open on start, so a deploy cannot silently take the show off air.
    line_open_on_start: bool = True

    #: Accept, Reject and closing the line succeed without contacting Twilio,
    #: so simulated calls (which Twilio has never heard of) can be accepted.
    #: main.py refuses it outside local: a real caller would stay on hold
    #: while the dashboard said they were on air.
    demo_mode: bool = False

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

    # --- Storage -----------------------------------------------------------
    #: What callers said is personal, so it goes; the calls themselves stay.
    #: 0 keeps transcripts forever.
    transcript_retention_days: int = Field(default=90, ge=0)

    #: Beside the code, so /srv/data/screener.db in the containers (where the
    #: volume mounts) and backend/data/screener.db on the host. Only tests
    #: should set it.
    database_path: Path = Path(__file__).resolve().parent.parent / "data" / "screener.db"

    # --- Dashboard ---------------------------------------------------------
    #: Per-dashboard outbound buffer; overflow drops oldest.
    frontend_queue_max: int = Field(default=250, ge=1)
    cors_allow_origins: str = "http://localhost:5173"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def shown_phone_number(self) -> str:
        return DEMO_PHONE_NUMBER if self.demo_mode else self.twilio_phone_number

    def _audio_url(self, configured: str) -> str:
        """URL for something the caller will hear, or "" to speak the text.

        Takes an absolute URL, or a bare filename in STATIC_DIR. The filename
        form matters in development: PUBLIC_BASE_URL is a tunnel hostname that
        rotates and .env cannot interpolate, so a pasted URL goes stale on every
        restart. A filename is rebuilt against the current base.

        A filename that is not on disk also gives "", so the prompt falls back
        to its text -- a <Play> pointing at a 404 is skipped by Twilio and
        leaves the caller in silence. Absolute URLs are trusted as given:
        checking one would mean a network request inside the call webhook.
        """
        target = configured.strip().lstrip("/")
        if not target:
            return ""
        if "://" in target:
            return target
        if not (STATIC_DIR / target).is_file():
            return ""
        return f"{self.public_base_url.rstrip('/')}/static/{target}"

    def missing_audio_files(self) -> list[str]:
        """Audio settings naming a file that is not in STATIC_DIR, as KEY=value.

        Those prompts are spoken instead. main.py reports them at startup, so a
        typo is caught at deploy time rather than heard on air.
        """
        audio = {
            "GREETING_AUDIO_URL": self.greeting_audio_url,
            "HOLD_MUSIC_URL": self.hold_music_url,
            "ACCEPTED_AUDIO_URL": self.accepted_audio_url,
            "REJECT_AUDIO_URL": self.reject_audio_url,
            "CLOSING_AUDIO_URL": self.closing_audio_url,
        }
        return [f"{k}={v}" for k, v in audio.items() if v.strip() and not self._audio_url(v)]

    @property
    def resolved_greeting_url(self) -> str:
        """Blank means speak ``greeting_message``, like every other prompt.

        There used to be a fallback to static/greeting.mp3 when that file was
        on disk. It made blank mean two different things depending on what
        happened to be in the image, so clearing the setting did not stop the
        recording playing.
        """
        return self._audio_url(self.greeting_audio_url)

    @property
    def resolved_hold_music_url(self) -> str:
        """Blank gets Twilio's own playlist -- music has no text to fall back on."""
        return self._audio_url(self.hold_music_url)

    @property
    def resolved_accepted_audio_url(self) -> str:
        return self._audio_url(self.accepted_audio_url)

    @property
    def resolved_reject_audio_url(self) -> str:
        return self._audio_url(self.reject_audio_url)

    @property
    def resolved_closing_audio_url(self) -> str:
        return self._audio_url(self.closing_audio_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Tests clear it via ``cache_clear``."""
    return Settings()
