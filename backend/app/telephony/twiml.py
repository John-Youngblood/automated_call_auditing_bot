"""TwiML: the XML documents Twilio takes as call-control instructions.

Twilio-only, deliberately. An earlier version of this package had a provider
abstraction with a Vonage renderer beside this one, but only the response
dialect was ever implemented -- frame parsing, signature verification and
outbound audio all assumed Twilio -- so "multi-provider" was true of one file
out of four and false everywhere it mattered. Supporting a second provider is
a real project; pretending to support one is worse than not.

Three documents, one per moment in a screened call:

    answer_and_gather()  greet, then listen for why they are calling
    hold()               park them while an operator reads the transcript
    reject()             refuse a blocked caller outright

Built with ElementTree rather than f-strings: caller-supplied values end up in
these documents, and string-built XML is an injection waiting to happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree.ElementTree import Element, SubElement, tostring


@dataclass(slots=True)
class RenderedResponse:
    body: str
    media_type: str = "application/xml"


def _document(response: Element) -> RenderedResponse:
    xml = tostring(response, encoding="unicode", short_empty_elements=True)
    return RenderedResponse(body=f'<?xml version="1.0" encoding="UTF-8"?>{xml}')


def answer_and_gather(
    *,
    greeting_url: str,
    action_url: str,
    speech_model: str = "phone_call",
    language: str = "en-US",
) -> RenderedResponse:
    """Greet the caller, then listen for why they are calling.

        <Response>
          <Gather input="speech" action="…" speechTimeout="auto"
                  actionOnEmptyResult="true">
            <Play>…/greeting.mp3</Play>
          </Gather>
          <Redirect>…</Redirect>
        </Response>

    ``<Play>`` sits *inside* ``<Gather>`` so the greeting doubles as the prompt
    and Twilio is already listening as it finishes -- a caller who talks over
    the greeting is still heard.

    ``speechTimeout="auto"`` is what makes this turn-based: Twilio decides when
    the caller has stopped and posts the finished transcript to ``action_url``.
    That end-of-speech detection is the entire reason this design needs no
    audio streaming.

    ``actionOnEmptyResult`` makes the action fire even when the caller says
    nothing, so a silent call still reaches the dashboard rather than hanging.
    The trailing ``<Redirect>`` covers the same risk from the other side: if
    ``<Gather>`` ever falls through, the call lands on the same endpoint
    instead of running off the end of the document, which would hang up on
    a caller who is still waiting.
    """
    response = Element("Response")

    gather = SubElement(
        response,
        "Gather",
        {
            "input": "speech",
            "action": action_url,
            "method": "POST",
            "speechTimeout": "auto",
            "speechModel": speech_model,
            "language": language,
            "actionOnEmptyResult": "true",
        },
    )
    SubElement(gather, "Play").text = greeting_url

    redirect = SubElement(response, "Redirect", {"method": "POST"})
    redirect.text = action_url

    return _document(response)


def hold(queue_name: str) -> RenderedResponse:
    """Park the caller while an operator reads their transcript.

    ``<Enqueue>`` earns its place: one verb holds the call open indefinitely
    with Twilio's built-in hold music -- no queue to pre-create, no hold audio
    to host, and no redirect loop to keep alive. Accepting the call dequeues
    it.
    """
    response = Element("Response")
    SubElement(response, "Enqueue").text = queue_name
    return _document(response)


def reject(reason: str = "rejected") -> RenderedResponse:
    """Refuse a blocked caller without answering.

    The cheap path, and the reason it matters: Twilio never connects the call,
    so there is no answered leg and no per-minute charge. Answering and then
    hanging up would bill for the call.

    ``reason`` is "rejected" (a not-accepting-calls treatment) or "busy" (a
    busy signal). Busy is the quieter option -- it looks like an ordinary
    failed call rather than a deliberate block.
    """
    response = Element("Response")
    SubElement(response, "Reject", {"reason": reason})
    return _document(response)
