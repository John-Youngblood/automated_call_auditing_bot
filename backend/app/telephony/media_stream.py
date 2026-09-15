"""Framing for the provider's media-stream websocket.

Twilio Media Streams are JSON *text* frames with base64 audio inside -- not
binary frames, which surprises people. This module turns them into a neutral
:class:`MediaFrame` so the route handler never touches provider JSON, and
builds the outbound frames used to play audio back to the caller.

Inbound event sequence for a normal call::

    connected -> start -> media * N -> stop

``media`` frames arrive every 20ms per track (~50/sec/call), so anything on
this path should be allocation-light and must never block.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class MediaEvent(StrEnum):
    CONNECTED = "connected"
    START = "start"
    MEDIA = "media"
    DTMF = "dtmf"
    MARK = "mark"
    STOP = "stop"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class MediaFrame:
    event: MediaEvent
    stream_id: str | None = None
    #: Provider's call identifier (Twilio ``callSid``).
    call_id: str | None = None
    #: Decoded PCM/mu-law bytes, set only for :attr:`MediaEvent.MEDIA`.
    audio: bytes | None = None
    track: str | None = None
    digit: str | None = None
    #: Custom ``<Parameter>`` values from the TwiML, set on ``start``.
    parameters: dict[str, str] = field(default_factory=dict)
    media_format: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def parse_frame(message: str | bytes) -> MediaFrame:
    """Parse one inbound websocket message.

    Never raises: a malformed frame becomes :attr:`MediaEvent.UNKNOWN` and is
    logged. One bad frame out of fifty per second must not tear down a call.
    """
    try:
        raw = json.loads(message)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        logger.warning("unparseable media frame (%d bytes)", len(message or b""))
        return MediaFrame(event=MediaEvent.UNKNOWN)

    if not isinstance(raw, dict):
        return MediaFrame(event=MediaEvent.UNKNOWN)

    try:
        event = MediaEvent(raw.get("event", "unknown"))
    except ValueError:
        event = MediaEvent.UNKNOWN

    frame = MediaFrame(event=event, stream_id=raw.get("streamSid"), raw=raw)

    if event is MediaEvent.START:
        start = raw.get("start") or {}
        frame.call_id = start.get("callSid")
        frame.stream_id = start.get("streamSid") or frame.stream_id
        frame.parameters = {
            str(k): str(v) for k, v in (start.get("customParameters") or {}).items()
        }
        frame.media_format = start.get("mediaFormat") or {}

    elif event is MediaEvent.MEDIA:
        media = raw.get("media") or {}
        frame.track = media.get("track")
        payload = media.get("payload")
        if payload:
            try:
                frame.audio = base64.b64decode(payload, validate=True)
            except (binascii.Error, ValueError):
                logger.warning("media frame had undecodable base64 payload")

    elif event is MediaEvent.DTMF:
        dtmf = raw.get("dtmf") or {}
        frame.digit = dtmf.get("digit")
        frame.track = dtmf.get("track")

    elif event is MediaEvent.STOP:
        frame.call_id = (raw.get("stop") or {}).get("callSid")

    return frame


# ---------------------------------------------------------------------------
# Outbound: audio back to the caller (the "two-way" half of the stream)
# ---------------------------------------------------------------------------
def outbound_audio_frame(stream_id: str, audio: bytes) -> str:
    """Wrap raw audio for playback to the caller.

    ``audio`` must already be in the stream's own format (8kHz mono mu-law for
    Twilio) -- the provider does no conversion. Feed it whatever your TTS
    produces *after* resampling; sending 16kHz PCM here yields chipmunk noise.
    """
    return json.dumps(
        {
            "event": "media",
            "streamSid": stream_id,
            "media": {"payload": base64.b64encode(audio).decode("ascii")},
        }
    )


def clear_audio_frame(stream_id: str) -> str:
    """Discard audio already buffered for playback.

    This is the barge-in primitive: when the caller starts talking over the
    bot, clear the queue so the bot stops mid-sentence.
    """
    return json.dumps({"event": "clear", "streamSid": stream_id})


def mark_frame(stream_id: str, name: str) -> str:
    """Place a marker; the provider echoes a ``mark`` frame once playback of
    everything queued before it has finished. The only reliable way to know
    the caller actually heard a prompt."""
    return json.dumps({"event": "mark", "streamSid": stream_id, "mark": {"name": name}})
