"""TwiML: the XML documents Twilio takes as call-control instructions.

Twilio-only, deliberately. An earlier version of this package had a provider
abstraction with a Vonage renderer beside this one, but only the response
dialect was ever implemented -- frame parsing, signature verification and
outbound audio all assumed Twilio -- so "multi-provider" was true of one file
out of four and false everywhere it mattered. Supporting a second provider is
a real project; pretending to support one is worse than not.

Four documents:

    answer_and_gather()  greet, then listen for why they are calling
    hold()               park them while an operator reads the transcript
    speak_and_hangup()   turn someone away with a reason
    dial()               put an accepted caller through to a human
    hang_up()            end a leg we have no further plans for

Anything the caller *hears* goes through :func:`voice`, which plays a
recording when one is configured and falls back to Twilio's text-to-speech
otherwise. Every prompt in this service works that way, so a deployment can
ship with nothing recorded and still say something sensible.

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


def voice(parent: Element, *, audio_url: str = "", text: str = "", tts_voice: str) -> None:
    """Append whatever this moment should sound like to ``parent``.

    A recording wins over words: audio is the whole reason a show has its own
    sound, and a synthesised voice in the middle of a produced podcast is a
    seam the audience hears. The text is the safety net, so a fresh deployment
    with nothing recorded still speaks rather than leaving dead air.

    Adds nothing at all when both are empty. That is a real case -- hold music
    is allowed to be absent so Twilio plays its own -- and an empty <Say> is
    worse than no element.

    ``text`` is built into the document, never interpolated: it is
    configuration someone edits under time pressure, and an apostrophe or an
    angle bracket must not be able to produce a different document.
    """
    if audio_url:
        SubElement(parent, "Play").text = audio_url
    elif text:
        SubElement(parent, "Say", {"voice": tts_voice}).text = text


def answer_and_gather(
    *,
    action_url: str,
    tts_voice: str,
    greeting_audio_url: str = "",
    greeting_text: str = "",
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
    voice(gather, audio_url=greeting_audio_url, text=greeting_text, tts_voice=tts_voice)

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



def speak_and_hangup(*, audio_url: str = "", text: str = "", tts_voice: str) -> RenderedResponse:
    """Say one thing, then end the call.

    Used wherever a caller is turned away: after the line closes, when the
    queue is cleared, and when an operator rejects someone. All three are
    deliberately spoken rather than a bare ``<Hangup>`` or a ``<Reject>`` busy
    signal -- a listener who gets silence assumes the number is broken and
    calls back, which is worse for them and for us.

    The cost of that choice: answering the call means these seconds are
    billed, where ``<Reject>`` would not be. A few seconds per turned-away
    caller is the right trade for a show whose callers are its audience.
    """
    response = Element("Response")
    voice(response, audio_url=audio_url, text=text, tts_voice=tts_voice)
    SubElement(response, "Hangup")
    return _document(response)


def dial(destination: str, *, action_url: str = "") -> RenderedResponse:
    """Put an accepted caller through to the host.

    Sent to a call that is already up, via the REST API, which pulls them out
    of the hold queue and into the dial leg.

    ``action`` is the only way to find out how that went. Twilio requests it
    when the dial ends -- whether the two of them talked and hung up, or the
    host's line was busy because they are already on air with someone else --
    and passes ``DialCallStatus``. Without it a call marked ACCEPTED sits on
    the dashboard as though it were live, with no way to tell a conversation
    in progress from one that never connected.
    """
    response = Element("Response")
    attributes = {"action": action_url, "method": "POST"} if action_url else {}
    SubElement(response, "Dial", attributes).text = destination
    return _document(response)


def hang_up() -> RenderedResponse:
    """End the call, explicitly.

    Twilio also ends a call that simply runs out of verbs, so an empty
    ``<Response/>`` would do the same thing -- but only as a side effect. This
    is used where hanging up is the *point*: after a ``<Dial>`` with an
    ``action`` URL, where the caller's leg is deliberately kept alive and
    handed back to us, and would otherwise sit connected to silence.
    """
    response = Element("Response")
    SubElement(response, "Hangup")
    return _document(response)
