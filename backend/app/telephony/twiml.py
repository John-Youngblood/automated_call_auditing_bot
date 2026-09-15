"""TwiML (Twilio, XML) renderer."""

from __future__ import annotations

from xml.etree.ElementTree import Element, SubElement, tostring

from app.telephony.base import CallPlan, RenderedResponse


def _document(response: Element) -> RenderedResponse:
    xml = tostring(response, encoding="unicode", short_empty_elements=True)
    return RenderedResponse(
        body=f'<?xml version="1.0" encoding="UTF-8"?>{xml}',
        media_type="application/xml",
    )


class TwimlRenderer:
    """Builds the XML document Twilio expects from a voice webhook.

    Produces::

        <Response>
          <Play>https://host/static/greeting.mp3</Play>
          <Connect>
            <Stream url="wss://host/ws/audio-stream">
              <Parameter name="callId" value="CAxxxx"/>
            </Stream>
          </Connect>
        </Response>

    Two things worth knowing:

    * ``<Connect><Stream>`` is the **two-way** verb -- Twilio forwards caller
      audio to the socket *and* plays back media frames the socket sends. The
      similar-looking ``<Start><Stream>`` is a one-way fork and cannot talk
      back, so it is the wrong verb for screening.
    * ``<Connect>`` blocks the call until the websocket closes, so it must come
      last. ``<Play>`` finishes before the stream opens, which is why the
      greeting is heard in full before transcription starts.

    The document is assembled with ElementTree rather than f-strings: caller
    names and numbers end up in attributes, and string-built XML is an
    injection waiting to happen.
    """

    name = "twilio"

    def render(self, plan: CallPlan) -> RenderedResponse:
        response = Element("Response")

        play = SubElement(response, "Play")
        play.text = plan.greeting_url

        connect = SubElement(response, "Connect")
        stream = SubElement(connect, "Stream", {"url": plan.stream_url})
        for key, value in plan.stream_parameters.items():
            SubElement(stream, "Parameter", {"name": key, "value": str(value)})

        return _document(response)

    def render_reject(self, reason: str = "rejected") -> RenderedResponse:
        """``<Reject>`` -- refuse the call without answering it.

        This is the cheap path, and the reason it matters: Twilio never
        connects the call, so there is no answered leg, no media stream, no
        Deepgram session, and no per-minute charge. Answering and then hanging
        up would bill for the answered call and spin up a transcription
        connection for a caller nobody wants to hear.

        ``reason`` is "rejected" (the caller hears a "not accepting calls"
        treatment) or "busy" (a busy signal). Busy is the quieter option: it
        looks like an ordinary failed call rather than a deliberate block,
        which is often what you want with a hostile caller.
        """
        response = Element("Response")
        SubElement(response, "Reject", {"reason": reason})
        return _document(response)
