"""Twilio voice webhooks -- the whole call flow, in three handlers.

    POST /webhook/incoming-call     a call arrives
    POST /webhook/speech-result     the caller finished describing their reason
    POST /webhook/queue-exit        the caller left the hold queue
    POST /webhook/dial-complete     an accepted caller's time with the host ended
    POST /webhook/call-status       the call ended

The flow:

    ring ──▶ <Gather input="speech"> greeting plays, Twilio listens
              │
              │  caller stops talking; Twilio detects it and posts the text
              ▼
         /webhook/speech-result ──▶ transcript to the dashboard
              │
              ▼
         <Enqueue> caller holds while an operator reads it and decides

Twilio owns the hard part -- deciding when the caller stopped speaking -- so
there is no audio streaming anywhere in this service.

Keep these handlers fast: Twilio times out and plays the caller an error.
"""

from __future__ import annotations

import contextlib
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response

from app.api.deps import LineDep, RegistryDep, SettingsDep
from app.schemas.calls import Caller
from app.services.decisions import DIAL_COMPLETE_PATH
from app.services.phone import format_location
from app.telephony import RenderedResponse, answer_and_gather, hang_up, hold, speak_and_hangup
from app.telephony.signature import verify_twilio_signature

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telephony"])

SPEECH_RESULT_PATH = "/webhook/speech-result"
QUEUE_EXIT_PATH = "/webhook/queue-exit"

#: QueueResult values meaning the caller is gone rather than connected.
#: "bridged" and "redirected" mean they reached a human, so whatever decision
#: was already recorded stands.
ABANDONED_QUEUE_RESULTS = frozenset({"hangup", "leave", "error", "system-error", "queue-full"})

#: "Nothing further" -- ends the call. Used wherever an action URL has been
#: handed control of a leg we have no more plans for.
_EMPTY_RESPONSE = '<?xml version="1.0" encoding="UTF-8"?><Response />' 


async def _form(request: Request) -> dict[str, str]:
    """Twilio posts ``application/x-www-form-urlencoded``, always."""
    form = await request.form()
    params = {str(k): str(v) for k, v in form.items()}
    # DEBUG only -- these carry the caller's number. Worth having: Twilio
    # sends some fields as empty strings rather than omitting them.
    logger.debug("%s params=%r", request.url.path, params)
    return params


def _check_signature(request: Request, params: dict[str, str], settings: SettingsDep) -> None:
    """Reject anything Twilio did not send, when validation is enabled.

    The URL is rebuilt from ``PUBLIC_BASE_URL``, not ``request.url``: the
    signature covers the URL Twilio *requested*, which behind a tunnel is not
    what this process sees. Trusting ``X-Forwarded-*`` would let it be forged.
    """
    if not settings.validate_webhook_signature:
        return

    url = f"{settings.public_base_url.rstrip('/')}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    if not verify_twilio_signature(
        auth_token=settings.twilio_auth_token,
        url=url,
        params=params,
        signature=request.headers.get("X-Twilio-Signature"),
    ):
        logger.warning("rejected unsigned/invalid webhook from %s", request.client)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid webhook signature")


def _twiml(rendered) -> Response:
    return Response(content=rendered.body, media_type=rendered.media_type)


@router.post(
    "/webhook/incoming-call",
    summary="Answer a call, play the greeting, and listen for the caller's reason",
    response_class=Response,
    responses={
        200: {"content": {"application/xml": {}}, "description": "TwiML"},
        403: {"description": "Signature verification failed"},
    },
)
async def incoming_call(
    request: Request,
    registry: RegistryDep,
    settings: SettingsDep,
    line: LineDep,
) -> Response:
    params = await _form(request)
    _check_signature(request, params, settings)

    # Twilio's own id, falling back to a generated one so a malformed payload
    # still produces a traceable call rather than a 500.
    call_id = params.get("CallSid") or f"local-{uuid.uuid4().hex[:12]}"
    number = params.get("From") or None
    caller = Caller(
        number=number,
        # Only sent when Caller ID Lookup is enabled on the number (paid, off
        # by default), so usually None.
        name=params.get("CallerName") or None,
        # Where the *number* is registered, not the caller -- see phone.py.
        location=format_location(
            params.get("FromCity"),
            params.get("FromState"),
            params.get("FromCountry"),
        ),
    )

    # Off air. Deliberately not registered: a caller who was never screened
    # does not belong in the queue or in history.
    if not line.is_open:
        logger.info("line closed, turning away call_id=%s from=%s", call_id, caller.number)
        return _twiml(
            speak_and_hangup(
                audio_url=settings.resolved_closed_line_audio_url,
                text=settings.closed_line_message,
                tts_voice=settings.tts_voice,
            )
        )

    registry.register_incoming(call_id, caller=caller, to_number=params.get("To"))
    logger.info("call screening call_id=%s from=%s", call_id, caller.number)

    return _twiml(
        answer_and_gather(
            greeting_audio_url=settings.resolved_greeting_url,
            greeting_text=settings.greeting_message,
            tts_voice=settings.tts_voice,
            action_url=f"{settings.public_base_url.rstrip('/')}{SPEECH_RESULT_PATH}",
            speech_model=settings.speech_model,
            language=settings.speech_language,
            speech_timeout_seconds=settings.speech_timeout_seconds,
        )
    )


@router.post(
    "/webhook/speech-result",
    summary="Receive the caller's transcribed reason and put them on hold",
    response_class=Response,
)
async def speech_result(
    request: Request,
    registry: RegistryDep,
    settings: SettingsDep,
) -> Response:
    """Twilio posts here once it detects the caller has stopped speaking.

    Both ``SpeechResult`` and ``Confidence`` can be absent: a silent caller
    still reaches here (``actionOnEmptyResult``) and should still reach the
    dashboard rather than vanishing.
    """
    params = await _form(request)
    _check_signature(request, params, settings)

    call_id = params.get("CallSid", "")
    text = (params.get("SpeechResult") or "").strip()

    confidence: float | None = None
    with contextlib.suppress(KeyError, TypeError, ValueError):
        confidence = float(params["Confidence"])

    if not text:
        logger.info("no speech detected call_id=%s", call_id)

    registry.set_transcript(call_id, text, confidence)

    # Hold them. The call stays open until an operator accepts, rejects, or
    # the caller hangs up.
    return _twiml(
        hold(
            settings.hold_queue_name,
            f"{settings.public_base_url.rstrip('/')}{QUEUE_EXIT_PATH}",
            wait_url=settings.resolved_hold_music_url,
        )
    )


@router.post(
    "/webhook/queue-exit",
    summary="The caller left the hold queue",
    response_class=Response,
)
async def queue_exit(
    request: Request,
    registry: RegistryDep,
    settings: SettingsDep,
) -> Response:
    """Twilio requests this when a call leaves the hold queue.

    ``QueueResult=hangup`` is the one that matters -- the only thing that
    tells us a caller gave up, since nothing keeps a connection while they
    hold. ``bridged`` and ``redirected`` mean they reached a human, so the
    decision already recorded stands.
    """
    params = await _form(request)
    _check_signature(request, params, settings)

    call_id = params.get("CallSid")
    result = params.get("QueueResult", "unknown")
    waited = params.get("QueueTime")

    if call_id and result in ABANDONED_QUEUE_RESULTS:
        logger.info("caller left the queue call_id=%s result=%s after=%ss", call_id, result, waited)
        registry.end(call_id)
    else:
        logger.info("queue exit call_id=%s result=%s after=%ss", call_id, result, waited)

    # An action URL controls call flow, so TwiML rather than the 204 a status
    # callback wants. Empty means "nothing further".
    return _twiml(RenderedResponse(body=_EMPTY_RESPONSE))


#: DialCallStatus values meaning the host and the caller actually spoke.
#: Everything else -- busy, no-answer, failed, canceled -- means the bridge
#: never happened, which matters because the call is already marked ACCEPTED.
BRIDGED_DIAL_RESULTS = frozenset({"completed", "answered"})


@router.post(
    DIAL_COMPLETE_PATH,
    summary="An accepted caller's time with the host ended",
    response_class=Response,
)
async def dial_complete(
    request: Request,
    registry: RegistryDep,
    settings: SettingsDep,
) -> Response:
    """Twilio reports how the bridge to the host went.

    Either way the call is over, so it is marked ENDED. ``completed`` means
    they talked, and ``DialCallDuration`` is the only place that length
    exists. Anything else means the host never picked up -- usually because
    they were already on air -- so that caller was never connected despite the
    dashboard saying ACCEPTED.

    The ``<Hangup>`` is load-bearing: an ``action`` URL makes Twilio keep the
    *caller's* leg alive and hand control back, so after the host hangs up
    that caller sits connected to silence until we end it.
    """
    params = await _form(request)
    _check_signature(request, params, settings)

    call_id = params.get("CallSid")
    result = params.get("DialCallStatus", "unknown")
    seconds = params.get("DialCallDuration")

    if not call_id:
        return _twiml(RenderedResponse(body=_EMPTY_RESPONSE))

    on_air_seconds: int | None = None
    if result in BRIDGED_DIAL_RESULTS:
        with contextlib.suppress(TypeError, ValueError):
            on_air_seconds = int(seconds)
        logger.info("on-air call ended call_id=%s after=%ss", call_id, seconds)
    else:
        # ACCEPTED said they got on air; they did not.
        logger.warning(
            "bridge to the host did not connect call_id=%s result=%s "
            "(the host may already be on a call)",
            call_id,
            result,
        )

    registry.end_on_air(call_id, on_air_seconds)

    return _twiml(hang_up())


#: Twilio CallStatus values that mean the call is over. The non-terminal ones
#: (queued, initiated, ringing, in-progress) are ignored.
TERMINAL_CALL_STATUSES = frozenset({"completed", "busy", "failed", "no-answer", "canceled"})


@router.post(
    "/webhook/call-status",
    summary="Provider call-progress callbacks",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def call_status(request: Request, registry: RegistryDep) -> Response:
    """Terminal call events.

    Configured per number in the Twilio console ("Call status changes"), and
    the only thing that clears the queue when a caller hangs up while holding.

    204 deliberately: a status callback does not control call flow, so Twilio
    wants 204 or empty TwiML. A JSON body logs a Debugger warning per call.
    """
    params = await _form(request)
    call_id = params.get("CallSid")
    state = params.get("CallStatus", "unknown")
    if call_id and state in TERMINAL_CALL_STATUSES:
        registry.end(call_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

