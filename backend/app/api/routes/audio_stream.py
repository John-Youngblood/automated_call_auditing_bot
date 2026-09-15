"""Provider-facing media websocket.

    WS /ws/audio-stream

The telecom provider connects here after the webhook's ``<Connect><Stream>``
and streams the caller's audio for the life of the call. This module is only a
frame pump: parse, dispatch, clean up. Transcription lives in
:class:`~app.services.screening.CallScreeningSession`, provider framing in
:mod:`app.telephony.media_stream`.

Concurrency notes
-----------------
* One task per call, and the receive loop **never** awaits on anything slower
  than the socket itself -- audio goes into the screening session's bounded
  queue and a separate pump talks to Deepgram. A stalled STT upstream can
  therefore not stall frame reading.
* ``finally`` teardown is not optional. Calls end by disconnect far more often
  than by a clean ``stop`` frame, and a leaked STT socket per dropped call will
  exhaust the connection pool within an hour of real traffic.
* No authentication is possible on this socket -- providers do not send
  credentials. Treat every frame as untrusted, identify the call from the
  ``callId`` parameter we planted in our own webhook response, and, in
  production, restrict ingress to the provider's published IP ranges.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.deps import RegistryDep, SettingsDep
from app.services.screening import CallScreeningSession
from app.telephony.media_stream import MediaEvent, parse_frame

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telephony"])


@router.websocket("/ws/audio-stream")
async def audio_stream(
    websocket: WebSocket,
    registry: RegistryDep,
    settings: SettingsDep,
) -> None:
    await websocket.accept()
    client = websocket.client.host if websocket.client else "unknown"
    logger.info("media stream connected from %s", client)

    session = CallScreeningSession(
        registry=registry,
        settings=settings,
        send_to_provider=websocket.send_text,
    )

    try:
        while True:
            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            # Twilio wraps audio in JSON text frames; Vonage sends raw binary
            # audio frames with a single JSON handshake. Handle both.
            if (text := message.get("text")) is not None:
                await _handle_text_frame(text, session)
            elif (payload := message.get("bytes")) is not None:
                session.feed_audio(payload)

    except WebSocketDisconnect as exc:
        logger.info("media stream disconnected call_id=%s code=%s", session.call_id, exc.code)
    except Exception:
        logger.exception("media stream failed call_id=%s", session.call_id)
    finally:
        # Always: flush the transcript tail, close the STT socket, mark the
        # call ended. Runs on disconnect, error, and shutdown alike.
        await session.close()


async def _handle_text_frame(text: str, session: CallScreeningSession) -> None:
    frame = parse_frame(text)

    match frame.event:
        case MediaEvent.CONNECTED:
            # Protocol handshake only; the call is identified on `start`.
            logger.debug("media stream handshake: %s", frame.raw)

        case MediaEvent.START:
            await session.handle_start(frame)

        case MediaEvent.MEDIA:
            if frame.audio:
                # >>> This is the hand-off to speech-to-text. <<<
                # Non-blocking by design: enqueue and return so the next 20ms
                # frame is read on time. See CallScreeningSession.feed_audio
                # and services/deepgram.py for the outbound half.
                session.feed_audio(frame.audio)

        case MediaEvent.DTMF:
            if frame.digit:
                await session.handle_dtmf(frame.digit)

        case MediaEvent.MARK:
            # Echo of a marker we placed -- playback of queued audio finished.
            logger.debug("mark reached call_id=%s %s", session.call_id, frame.raw.get("mark"))

        case MediaEvent.STOP:
            logger.info("media stream stop frame call_id=%s", session.call_id)

        case _:
            logger.debug("ignoring unknown media frame")
