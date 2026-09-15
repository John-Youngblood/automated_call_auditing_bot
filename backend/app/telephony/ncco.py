"""NCCO (Vonage, JSON) renderer -- the JSON counterpart to TwiML."""

from __future__ import annotations

import json

from app.telephony.base import CallPlan, RenderedResponse


class NccoRenderer:
    """Builds a Vonage Call Control Object.

    Same three instructions as :class:`~app.telephony.twiml.TwimlRenderer`, in
    JSON: ``stream`` plays the greeting, ``connect`` to a websocket endpoint
    opens the two-way audio leg.

    Note the different audio contract -- Vonage websockets carry 16-bit linear
    PCM at 16kHz, not mu-law at 8kHz. Set ``STT_ENCODING=linear16`` and
    ``STT_SAMPLE_RATE=16000`` when switching, or Deepgram will happily
    transcribe noise.
    """

    name = "vonage"

    def render(self, plan: CallPlan) -> RenderedResponse:
        ncco = [
            {"action": "stream", "streamUrl": [plan.greeting_url], "bargeIn": False},
            {
                "action": "connect",
                "endpoint": [
                    {
                        "type": "websocket",
                        "uri": plan.stream_url,
                        "content-type": "audio/l16;rate=16000",
                        "headers": dict(plan.stream_parameters),
                    }
                ],
            },
        ]
        return RenderedResponse(body=json.dumps(ncco), media_type="application/json")

    def render_reject(self, reason: str = "rejected") -> RenderedResponse:
        """Vonage has no ``Reject`` verb -- an NCCO that runs out of actions
        ends the call, so an empty document is the equivalent.

        The trade-off versus TwiML's ``<Reject>``: Vonage has already answered
        the call by the time it fetches the NCCO, so this does not avoid the
        answered leg the way ``<Reject>`` does. It still avoids the media
        stream and the transcription session, which is the larger cost.
        """
        return RenderedResponse(body=json.dumps([]), media_type="application/json")
