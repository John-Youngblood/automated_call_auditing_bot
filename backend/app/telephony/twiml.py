"""The XML documents Twilio executes during a call.

    answer_and_gather()  greet, then listen for why they are calling
    hold()               park them while an operator reads the transcript
    hold_music()         one track, while they hold
    leave_queue()        take them out of the queue (their hold ran too long)
    dial()               put an accepted caller through to the host
    speak_and_hangup()   turn someone away with a reason
    hang_up()            end a leg we have no further plans for

Anything the caller hears goes through :func:`voice`: a recording if one is
configured, Twilio text-to-speech otherwise.

Built with ElementTree, never f-strings -- configured text ends up in these
documents and string-built XML is an injection waiting to happen.

See docs/architecture.md for why the call flow is shaped this way.
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
    """Append a recording, or spoken text, or nothing.

    Nothing is a real case: hold music may be absent so Twilio plays its own.
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
    """Greet the caller, then listen for why they are calling."""
    response = Element("Response")
    gather = SubElement(
        response,
        "Gather",
        {
            "input": "speech",
            "action": action_url,
            "method": "POST",
            # Seconds, never "auto": Twilio warns (error 13335) when "auto" is
            # paired with a speechModel, and "auto" stops at the first pause,
            # cutting callers off mid-explanation.
            "speechTimeout": str(speech_timeout_seconds),
            "speechModel": speech_model,
            "language": language,
            # Fire the action even for a silent caller, so they still reach the
            # dashboard instead of the call hanging.
            "actionOnEmptyResult": "true",
        },
    )
    # Inside <Gather>, so the greeting is also the prompt and Twilio is already
    # listening as it finishes.
    voice(gather, audio_url=greeting_audio_url, text=greeting_text, tts_voice=tts_voice)

    # If <Gather> ever falls through, land back here rather than running off the
    # end of the document, which would hang up on a waiting caller.
    SubElement(response, "Redirect", {"method": "POST"}).text = action_url
    return _document(response)


#: What Twilio plays by default while a caller holds -- its own classical
#: playlist, taken from the twimlet it would otherwise use. Named here because
#: the waitUrl is now ours (see :func:`hold`), so blank hold music has to mean
#: "one of these" explicitly. Plain http: the bucket name has dots, so S3's
#: wildcard certificate does not cover it, and this is how Twilio fetches them.
TWILIO_HOLD_MUSIC = tuple(
    f"http://com.twilio.music.classical.s3.amazonaws.com/{track}"
    for track in (
        "ClockworkWaltz.mp3",
        "oldDog_-_endless_goodbye_%28instr.%29.mp3",
        "MARKOVICHAMP-Borghestral.mp3",
        "BusyStrings.mp3",
        "ith_chopin-15-2.mp3",
        "Mellotroniac_-_Flight_Of_Young_Hearts_Flute.mp3",
        "ith_brahms-116-4.mp3",
    )
)


def hold(queue_name: str, action_url: str, wait_url: str) -> RenderedResponse:
    """Park the caller while an operator reads their transcript.

    ``action`` is the only thing that reports how they left: Twilio posts
    ``QueueResult`` and ``QueueTime``.

    ``waitUrl`` is our webhook, not the music. Twilio requests it again each
    time what it returned finishes playing, with ``QueueTime`` attached --
    which is what lets the answer be :func:`leave_queue` once a hold has run
    too long, with no timer of our own. POST, so a TwiML answer is never
    cached; the audio inside it still is.
    """
    response = Element("Response")
    SubElement(
        response,
        "Enqueue",
        {"action": action_url, "method": "POST", "waitUrl": wait_url, "waitUrlMethod": "POST"},
    ).text = queue_name
    return _document(response)


def hold_music(audio_url: str) -> RenderedResponse:
    """One track. Twilio asks the waitUrl again when it ends, so this loops."""
    response = Element("Response")
    SubElement(response, "Play").text = audio_url
    return _document(response)


def leave_queue() -> RenderedResponse:
    """End the hold. Twilio then requests ``<Enqueue action>`` with
    ``QueueResult=leave``, and the caller is still on the line for it."""
    response = Element("Response")
    SubElement(response, "Leave")
    return _document(response)


def dial(destination: str, *, action_url: str = "") -> RenderedResponse:
    """Put an accepted caller through to the host.

    ``action`` is the only thing that reports the outcome -- whether they
    talked, or the host's line was busy. It also changes what happens when the
    dial ends: Twilio keeps the caller's leg alive and hands control back, so
    whatever serves ``action_url`` must hang up on them.
    """
    attributes = {"action": action_url, "method": "POST"} if action_url else {}
    response = Element("Response")
    SubElement(response, "Dial", attributes).text = destination
    return _document(response)


def speak_and_hangup(*, audio_url: str = "", text: str = "", tts_voice: str) -> RenderedResponse:
    """Say one thing, then end the call.

    Spoken rather than a bare hangup or a ``<Reject>`` busy signal: silence
    reads as a broken number, and these callers are the show's audience. The
    trade is that answering bills a few seconds where ``<Reject>`` would not.
    """
    response = Element("Response")
    voice(response, audio_url=audio_url, text=text, tts_voice=tts_voice)
    SubElement(response, "Hangup")
    return _document(response)


def hang_up() -> RenderedResponse:
    """End the call explicitly.

    An empty ``<Response/>`` ends a call too, but only as a side effect of
    running out of verbs. Used where hanging up is the point.
    """
    response = Element("Response")
    SubElement(response, "Hangup")
    return _document(response)
