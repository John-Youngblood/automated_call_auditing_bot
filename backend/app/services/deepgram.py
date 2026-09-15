"""Streaming speech-to-text. This is where call audio leaves the building.

    provider media websocket  ->  /ws/audio-stream  ->  SttSession.send_audio()
                                                              |
                                        Deepgram streaming API |
                                                              v
                                       on_transcript(...)  ->  CallRegistry
                                                              ->  Broadcaster
                                                              ->  /ws/frontend

Two implementations behind one protocol:

* :class:`DeepgramSession` -- a real streaming connection. Written against
  Deepgram's documented listen/v1 websocket protocol, but **not yet exercised
  against the live API in this scaffold**; treat the parameter tuning
  (endpointing, interim results, keepalive cadence) as a starting point.
* :class:`MockSttSession` -- emits canned transcripts on a timer so the whole
  stack demos end-to-end with no credentials. Default for local dev.

Pick with :func:`create_stt_session`; callers never branch on which is which.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode

import websockets

from app.config import Settings

logger = logging.getLogger(__name__)

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"
#: Deepgram closes a connection that has been silent for ~10s; keep it warm.
KEEPALIVE_INTERVAL_SECONDS = 5.0


@dataclass(slots=True)
class SttResult:
    """One recognition result, provider-agnostic."""

    text: str
    is_final: bool
    confidence: float | None = None
    start_time: float | None = None
    speaker: str | None = None


#: Called for every result. Must not raise -- it runs inside the reader task.
OnTranscript = Callable[[SttResult], Awaitable[None]]
#: ``(state, detail)`` where state is e.g. "connected" / "degraded" / "closed".
OnStatus = Callable[[str, str], Awaitable[None]]


@runtime_checkable
class SttSession(Protocol):
    """Lifecycle of one call's transcription.

    ``start`` -> many ``send_audio`` -> ``finish``. Implementations must make
    ``send_audio`` cheap and non-raising: it sits directly on the audio hot
    path, and a transcription failure should degrade the call, not drop it.
    """

    async def start(self) -> None: ...
    async def send_audio(self, chunk: bytes) -> None: ...
    async def finish(self) -> None: ...


# ---------------------------------------------------------------------------
# Real Deepgram client
# ---------------------------------------------------------------------------
class DeepgramSession:
    def __init__(
        self,
        settings: Settings,
        on_transcript: OnTranscript,
        on_status: OnStatus | None = None,
    ) -> None:
        self._settings = settings
        self._on_transcript = on_transcript
        self._on_status = on_status
        self._ws: Any | None = None
        self._reader: asyncio.Task[None] | None = None
        self._keepalive: asyncio.Task[None] | None = None
        #: Set when the socket is unusable; send_audio then becomes a no-op so
        #: one STT outage cannot take the call down with it.
        self._degraded = False

    # -- setup --------------------------------------------------------------
    def _build_url(self) -> str:
        s = self._settings
        params = {
            # Audio format -- must match exactly what the provider sends, or
            # you get confident-sounding garbage back. Twilio Media Streams
            # are 8kHz mono mu-law.
            "encoding": s.stt_encoding,
            "sample_rate": str(s.stt_sample_rate),
            "channels": str(s.stt_channels),
            "model": s.deepgram_model,
            "language": s.deepgram_language,
            # Interim results are what make the dashboard feel live.
            "interim_results": "true",
            "punctuate": "true",
            "smart_format": "true",
            # Emit an endpoint after 200ms of silence -- tuned for screening,
            # where reacting fast matters more than perfect sentence breaks.
            "endpointing": "200",
            "vad_events": "true",
        }
        return f"{DEEPGRAM_WS_URL}?{urlencode(params)}"

    async def _connect(self) -> Any:
        """Open the socket, tolerating the websockets header-kwarg rename.

        websockets <14 takes ``extra_headers``; >=14 takes
        ``additional_headers``. Supporting both keeps the pin range wide.
        """
        url = self._build_url()
        headers = {"Authorization": f"Token {self._settings.deepgram_api_key}"}
        try:
            return await websockets.connect(url, additional_headers=headers)
        except TypeError:
            return await websockets.connect(url, extra_headers=headers)  # type: ignore[call-arg]

    async def start(self) -> None:
        try:
            self._ws = await self._connect()
        except Exception as exc:  # network, auth, DNS -- all non-fatal to the call
            self._degraded = True
            logger.exception("deepgram connect failed; continuing without transcription")
            await self._status("degraded", f"stt unavailable: {exc}")
            return

        self._reader = asyncio.create_task(self._read_loop(), name="deepgram-reader")
        self._keepalive = asyncio.create_task(self._keepalive_loop(), name="deepgram-keepalive")
        logger.info(
            "deepgram connected model=%s encoding=%s@%sHz",
            self._settings.deepgram_model,
            self._settings.stt_encoding,
            self._settings.stt_sample_rate,
        )
        await self._status("connected", self._settings.deepgram_model)

    # -- hot path -----------------------------------------------------------
    async def send_audio(self, chunk: bytes) -> None:
        """Forward one decoded audio chunk to Deepgram.

        Raw binary frame, no envelope: the connection was opened with the
        encoding/sample-rate already declared in the query string.
        """
        if self._degraded or self._ws is None:
            return
        try:
            await self._ws.send(chunk)
        except Exception as exc:
            self._degraded = True
            logger.warning("deepgram send failed, degrading call: %s", exc)
            await self._status("degraded", str(exc))

    # -- teardown -----------------------------------------------------------
    async def finish(self) -> None:
        """Flush, wait briefly for trailing finals, then close."""
        if self._keepalive is not None:
            self._keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive

        if self._ws is not None and not self._degraded:
            with contextlib.suppress(Exception):
                # Tells Deepgram to finalise rather than truncate mid-word.
                await self._ws.send(json.dumps({"type": "CloseStream"}))

        if self._reader is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._reader, timeout=3.0)
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader

        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

        await self._status("closed", "")

    # -- internals ----------------------------------------------------------
    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    await self._handle_message(raw)
                except Exception:
                    logger.exception("malformed deepgram message, skipping")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("deepgram read loop ended: %s", exc)
            self._degraded = True
            await self._status("degraded", str(exc))

    async def _handle_message(self, raw: str | bytes) -> None:
        msg = json.loads(raw)
        kind = msg.get("type")

        if kind != "Results":
            # Metadata / SpeechStarted / UtteranceEnd. Useful for barge-in and
            # turn detection -- hook them up here when you need them.
            logger.debug("deepgram event %s", kind)
            return

        alternatives = msg.get("channel", {}).get("alternatives") or []
        if not alternatives:
            return
        best = alternatives[0]
        text = (best.get("transcript") or "").strip()
        if not text:
            # Silence still produces empty results; nothing to show.
            return

        words = best.get("words") or []
        speaker = None
        if words and "speaker" in words[0]:
            speaker = f"speaker_{words[0]['speaker']}"

        await self._on_transcript(
            SttResult(
                text=text,
                is_final=bool(msg.get("is_final")),
                confidence=best.get("confidence"),
                start_time=msg.get("start"),
                speaker=speaker,
            )
        )

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_SECONDS)
            if self._ws is None or self._degraded:
                return
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"type": "KeepAlive"}))

    async def _status(self, state: str, detail: str) -> None:
        if self._on_status is not None:
            with contextlib.suppress(Exception):
                await self._on_status(state, detail)


# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------
class MockSttSession:
    """Fake transcriber: turns audio *volume* into fake words.

    Emits one interim then one final line roughly every second of received
    audio, cycling a canned script, so the dashboard can be developed without
    Deepgram credentials or a live phone call.
    """

    SCRIPT: tuple[str, ...] = (
        "Hi, this is Dana calling from Northgate Medical billing.",
        "I'm following up on an outstanding invoice for one of your patients.",
        "Is this a good time to talk, or should I call back later?",
        "No problem, I can hold for a moment.",
        "Thanks, I appreciate you taking the call today.",
    )

    def __init__(
        self, settings: Settings, on_transcript: OnTranscript, on_status: OnStatus | None = None
    ) -> None:
        self._on_transcript = on_transcript
        self._on_status = on_status
        #: One second of audio, in bytes, for the configured format.
        self._bytes_per_second = max(1, settings.stt_sample_rate * settings.stt_channels)
        self._buffered = 0
        self._index = 0
        self._elapsed = 0.0

    async def start(self) -> None:
        logger.info("STT running in MOCK mode -- set STT_MOCK=false with a key for real results")
        if self._on_status is not None:
            await self._on_status("connected", "mock")

    async def send_audio(self, chunk: bytes) -> None:
        self._buffered += len(chunk)
        if self._buffered < self._bytes_per_second:
            return
        self._buffered = 0

        text = self.SCRIPT[self._index % len(self.SCRIPT)]
        self._index += 1
        start = self._elapsed
        self._elapsed += 1.0

        # Interim first, then the same line committed -- mirrors the real
        # interim/final sequence the dashboard has to cope with.
        await self._on_transcript(
            SttResult(text=text[: max(4, len(text) // 2)], is_final=False, start_time=start)
        )
        await self._on_transcript(
            SttResult(text=text, is_final=True, confidence=0.97, start_time=start)
        )

    async def finish(self) -> None:
        if self._on_status is not None:
            await self._on_status("closed", "mock")


def create_stt_session(
    settings: Settings,
    on_transcript: OnTranscript,
    on_status: OnStatus | None = None,
) -> SttSession:
    """Return a real or mock session depending on configuration."""
    if settings.stt_enabled:
        return DeepgramSession(settings, on_transcript, on_status)
    if not settings.deepgram_api_key and not settings.stt_mock:
        logger.warning("DEEPGRAM_API_KEY is unset -- falling back to mock transcription")
    return MockSttSession(settings, on_transcript, on_status)
