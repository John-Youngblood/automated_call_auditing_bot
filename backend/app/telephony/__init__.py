"""Twilio-specific code.

The containment boundary: TwiML documents, webhook signature verification, and
the REST client for controlling a live call. Everything outside this package
deals only in :mod:`app.schemas` types.
"""

from app.telephony.twiml import (
    TWILIO_HOLD_MUSIC,
    RenderedResponse,
    answer_and_gather,
    dial,
    hang_up,
    hold,
    hold_music,
    leave_queue,
    speak_and_hangup,
)

__all__ = [
    "TWILIO_HOLD_MUSIC",
    "RenderedResponse",
    "answer_and_gather",
    "dial",
    "hang_up",
    "hold",
    "hold_music",
    "leave_queue",
    "speak_and_hangup",
]
