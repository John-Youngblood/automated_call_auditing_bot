"""Per-call orchestration: media stream in, transcript out.

Owns one call's transcription lifecycle so the websocket route stays a thin
frame pump. Nothing here knows about FastAPI; nothing in the route knows about
Deepgram.

Buffering, and why it exists
----------------------------
Audio arrives on a hard 20ms clock. If we awaited the STT socket directly from
the receive loop, a slow upstream would stall reads, the provider's send buffer
would back up, and it would start discarding frames on its side -- with no
visibility on ours. So received audio goes into a small bounded queue drained
by a dedicated pump task, and when that queue overflows we drop the **oldest**
audio: in live screening, five-second-old speech has no value.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from app.config import Settings
from app.schemas.calls import Caller, CallStatus
from app.schemas.events import ServerEvent
from app.services.call_registry import CallRegistry
from app.services.deepgram import SttResult, SttSession, create_stt_session
from app.telephony.media_stream import MediaFrame, outbound_audio_frame

logger = logging.getLogger(__name__)

#: ~2 seconds of 20ms frames. Enough to ride out a GC pause or a TLS
#: renegotiation, short enough that we never transcribe stale audio.
AUDIO_QUEUE_MAX = 100

SendToProvider = Callable[[str], Awaitable[None]]


class CallScreeningSession:
    def __init__(
        self,
        registry: CallRegistry,
        settings: Settings,
        send_to_provider: SendToProvider | None = None,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._send_to_provider = send_to_provider

        self.call_id: str | None = None
        self.stream_id: str | None = None

        self._stt: SttSession | None = None
        self._audio: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=AUDIO_QUEUE_MAX)
        self._pump: asyncio.Task[None] | None = None
        self._dropped_chunks = 0
        self._bytes_in = 0

    # -- lifecycle ----------------------------------------------------------
    async def handle_start(self, frame: MediaFrame) -> None:
        """Bind the stream to a call and open the transcription connection.

        Prefers the ``callId`` custom parameter we set in the TwiML over
        anything else in the frame: it came from our own webhook response, so
        it ties this socket to a call we actually queued.
        """
        self.call_id = frame.parameters.get("callId") or frame.call_id or frame.stream_id
        self.stream_id = frame.stream_id
        if self.call_id is None:
            logger.error("media stream start frame carried no usable call identifier")
            return

        self._registry.attach_stream(self.call_id, self.stream_id or self.call_id)
        logger.info(
            "screening started call_id=%s stream_id=%s format=%s",
            self.call_id,
            self.stream_id,
            frame.media_format or "unspecified",
        )

        self._stt = create_stt_session(
            self._settings,
            on_transcript=self._on_transcript,
            on_status=self._on_stt_status,
        )
        await self._stt.start()
        self._pump = asyncio.create_task(self._drain_audio(), name=f"stt-pump-{self.call_id}")

    async def close(self) -> None:
        """Flush and tear down. Safe to call twice; runs on every exit path."""
        if self._pump is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._audio.put_nowait(None)  # poison pill -> pump exits
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._pump, timeout=2.0)
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
            self._pump = None

        if self._stt is not None:
            await self._stt.finish()
            self._stt = None

        if self.call_id is not None:
            call = self._registry.get(self.call_id)
            # An accepted call lives on past the screening stream; only close
            # out calls that were still being screened.
            if call is not None and call.status in (CallStatus.RINGING, CallStatus.SCREENING):
                await self._registry.end(self.call_id)

        logger.info(
            "screening finished call_id=%s bytes=%s dropped_chunks=%s",
            self.call_id,
            self._bytes_in,
            self._dropped_chunks,
        )

    # -- inbound ------------------------------------------------------------
    def feed_audio(self, chunk: bytes) -> None:
        """Hand one audio chunk to the STT pump. Synchronous and non-blocking.

        This is called ~50x/second per call, so it does no I/O and never
        awaits -- see the module docstring for why.
        """
        if not chunk or self._stt is None:
            return
        self._bytes_in += len(chunk)
        try:
            self._audio.put_nowait(chunk)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._audio.get_nowait()  # discard oldest
            self._dropped_chunks += 1
            if self._dropped_chunks % 50 == 1:
                logger.warning(
                    "stt backlog on call_id=%s, dropped %s chunk(s)",
                    self.call_id,
                    self._dropped_chunks,
                )
            with contextlib.suppress(asyncio.QueueFull):
                self._audio.put_nowait(chunk)

    async def handle_dtmf(self, digit: str) -> None:
        """Keypad press during screening.

        Placeholder: a natural hook for IVR-style screening ("press 1 if this
        is urgent"). Wire it to :meth:`CallRegistry.set_status` or your own
        routing rules.
        """
        logger.info("dtmf digit=%s call_id=%s", digit, self.call_id)

    # -- outbound (the two-way half) ---------------------------------------
    async def play_to_caller(self, audio: bytes) -> None:
        """Send audio back down the media stream to the caller.

        Placeholder for the bot's own voice -- hold messages, "one moment
        please", TTS from an LLM. ``audio`` must already be in the stream's
        native format (8kHz mono mu-law for Twilio); nothing resamples for you.
        """
        if self._send_to_provider is None or self.stream_id is None:
            logger.debug("no outbound channel for call_id=%s", self.call_id)
            return
        await self._send_to_provider(outbound_audio_frame(self.stream_id, audio))

    # -- internals ----------------------------------------------------------
    async def _drain_audio(self) -> None:
        """Move queued chunks into the STT session, one await at a time."""
        assert self._stt is not None
        try:
            while True:
                chunk = await self._audio.get()
                if chunk is None:
                    return
                await self._stt.send_audio(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stt pump failed for call_id=%s", self.call_id)

    async def _on_transcript(self, result: SttResult) -> None:
        if self.call_id is None:
            return
        self._registry.add_transcript(
            self.call_id,
            result.text,
            is_final=result.is_final,
            speaker=result.speaker,
            confidence=result.confidence,
            start_time=result.start_time,
        )

    async def _on_stt_status(self, state: str, detail: str) -> None:
        # Surface STT health to the dashboard so an agent can tell "silent
        # caller" apart from "transcription is down".
        self._registry.publish(ServerEvent.stream_status(self.call_id, state, detail))


def caller_from_parameters(params: dict[str, str]) -> Caller:
    """Best-effort caller identity from media-stream custom parameters."""
    return Caller(
        number=params.get("from") or params.get("From"),
        name=params.get("callerName") or params.get("CallerName"),
    )
