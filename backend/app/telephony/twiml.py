"""TwiML: the XML documents Twilio takes as call-control instructions.

Twilio-only, deliberately. An earlier version of this package had a provider
abstraction with a Vonage renderer beside this one, but only the response
dialect was ever implemented -- frame parsing, signature verification and
outbound audio all assumed Twilio -- so "multi-provider" was true of one file
out of four and false everywhere it mattered. Supporting a second provider is
a real project; pretending to support one is worse than not.

Two documents, one per moment in a screened call:

    answer_and_gather()  greet, then listen for why they are calling
    hold()               park them while an operator reads the transcript

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
    speech_timeout_seconds: int = 3,
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

    ``speechTimeout`` is what makes this turn-based: Twilio decides when the
        caller has stopped and posts the finished transcript to ``action_url``.
        That end-of-speech detection is the entire reason this design needs no
        audio streaming.

        It is a number of seconds, never ``"auto"``, for two reasons. Twilio warns
        (error 13335) when ``auto`` is combined with a ``speechModel``, and we set
        one. And ``auto`` stops at the *first* pause in speech, which would cut a
        caller off mid-explanation -- fine for "say your account number", wrong for
        "tell us why you are calling".

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
            "speechTimeout": str(speech_timeout_seconds),
            "speechModel": speech_model,
            "language": language,
            "actionOnEmptyResult": "true",
        },
    )
    SubElement(gather, "Play").text = greeting_url

    redirect = SubElement(response, "Redirect", {"method": "POST"})
    redirect.text = action_url

    return _document(response)


def hold(queue_name: str, action_url: str, wait_url: str = "") -> RenderedResponse:
    """Park the caller while an operator reads their transcript.

    ``<Enqueue>`` earns its place: one verb holds the call open indefinitely
    with hold music -- no queue to pre-create and no redirect loop to keep
    alive. Accepting the call dequeues it.

    ``action`` is how we find out the caller gave up. Twilio requests it when
    the call leaves the queue for any reason and passes ``QueueResult``
    (``hangup`` when they hung up while waiting) plus ``QueueTime``. Without
    it, a caller who abandons the queue leaves no trace and their card sits on
    the dashboard until someone tries to put a dead line on air.

    ``wait_url`` points at one audio file, looped by Twilio for as long as the
    caller waits. Omitting it gets Twilio's default classical playlist.

    Note the method split: ``action`` is POSTed like every other webhook here,
    but ``waitUrl`` is deliberately a GET, because Twilio only caches a static
    audio file when it fetches it with GET. POST it and the same MP3 is
    re-downloaded on every loop, for every caller.
    """
    response = Element("Response")
    attributes = {"action": action_url, "method": "POST"}
    if wait_url:
        attributes["waitUrl"] = wait_url
        attributes["waitUrlMethod"] = "GET"
    enqueue = SubElement(response, "Enqueue", attributes)
    enqueue.text = queue_name
    return _document(response)

