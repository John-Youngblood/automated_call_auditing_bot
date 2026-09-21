"""Twilio voice webhooks -- the whole call flow, in three handlers.

    POST /webhook/incoming-call     a call arrives
    POST /webhook/speech-result     the caller finished describing their reason
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

Twilio owns the hard part -- deciding when the caller stopped speaking -- which
is why there is no audio streaming anywhere in this service.

Keep these handlers fast. Twilio waits a couple of seconds and then plays an
error to the caller, so CRM lookups and anything else slow belongs elsewhere.
"""

from __future__ import annotations

import contextlib
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response

from app.api.deps import RegistryDep, SettingsDep
from app.schemas.calls import Caller
from app.services.phone import format_location
from app.telephony import RenderedResponse, answer_and_gather, hold
from app.telephony.signature import verify_twilio_signature

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telephony"])

SPEECH_RESULT_PATH = "/webhook/speech-result"
QUEUE_EXIT_PATH = "/webhook/queue-exit"

#: QueueResult values meaning the caller is gone rather than connected.
#: "bridged" and "redirected" mean they reached a human, so whatever decision
#: was already recorded stands.
ABANDONED_QUEUE_RESULTS = frozenset({"hangup", "leave", "error", "system-error", "queue-full"})


async def _form(request: Request) -> dict[str, str]:
    """Twilio posts ``application/x-www-form-urlencoded``, always."""
    form = await request.form()
    params = {str(k): str(v) for k, v in form.items()}
    # At DEBUG only: these carry the caller's number. Invaluable when a field
    # you expected is missing -- Twilio sends some geographic parameters as
    # empty strings rather than omitting them, which is indistinguishable from
    # "absent" unless you can see the raw body.
    logger.debug("%s params=%r", request.url.path, params)
    return params


def _check_signature(request: Request, params: dict[str, str], settings: SettingsDep) -> None:
    """Reject anything Twilio did not send, when validation is enabled.

    The URL is rebuilt from ``PUBLIC_BASE_URL`` rather than ``request.url``:
    the signature covers the URL Twilio *requested*, which behind a tunnel or
    load balancer is not the one this process observes. Trusting
    ``X-Forwarded-*`` instead would let a caller forge it.
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
) -> Response:
    params = await _form(request)
    _check_signature(request, params, settings)

    # Twilio's own id, falling back to a generated one so a malformed payload
    # still produces a traceable call rather than a 500.
    call_id = params.get("CallSid") or f"local-{uuid.uuid4().hex[:12]}"
    number = params.get("From") or None
    caller = Caller(
        number=number,
        # Twilio only sends CallerName if Caller ID Lookup is enabled on the
        # number (VoiceCallerIdLookup), which is a paid per-lookup feature.
        # Off by default -- so without it this is always None and every caller
        # shows as a bare number.
        name=params.get("CallerName") or None,
        # Twilio derives these from the number's rate centre, not from where
        # the caller is -- see format_location.
        location=format_location(
            params.get("FromCity"),
            params.get("FromState"),
            params.get("FromCountry"),
        ),
    )

    registry.register_incoming(call_id, caller=caller, to_number=params.get("To"))
    logger.info("call screening call_id=%s from=%s", call_id, caller.number)

    return _twiml(
        answer_and_gather(
            greeting_url=settings.resolved_greeting_url,
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

    ``SpeechResult`` is the finished transcript and ``Confidence`` is how much
    Twilio trusts it. Both can be absent -- a caller who stayed silent still
    reaches this endpoint (``actionOnEmptyResult``), and they should still show
    up on the dashboard so an operator can deal with them rather than having
    the call vanish.
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
            wait_url=settings.hold_music_url,
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

    ``QueueResult`` says why. ``hangup`` is the one that matters: the caller
    gave up waiting, and this is the only thing that tells us -- nothing keeps
    a connection to this service while they hold. ``QueueTime`` is how long
    they waited, worth logging because a rising abandon time means the queue
    is too slow.

    ``bridged`` and ``redirected`` mean they were connected to a human, so the
    decision already recorded stands and this leaves it alone.
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

    # An action URL controls call flow, so this returns TwiML rather than the
    # 204 a status callback wants. An empty document means "nothing further":
    # correct for a caller who already hung up, and harmless for one being
    # bridged, whose new instructions came from the redirect that dequeued
    # them. Worth re-checking once the REST accept path is real.
    return _twiml(RenderedResponse(body='<?xml version="1.0" encoding="UTF-8"?><Response />'))


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

    Point Twilio's status callback at this URL so the queue clears itself when
    a caller hangs up while on hold -- otherwise their card sits there until
    someone tries to act on a call that is already gone. It is configured per
    number in the Twilio console; nothing here can set it.

    Returns 204 deliberately. A status callback does not control call flow, so
    Twilio wants either 204 or an empty ``<Response/>`` as text/xml -- anything
    else (a JSON body, as this used to send) is logged as a warning in the
    Twilio Debugger on every single call.
    """
    params = await _form(request)
    call_id = params.get("CallSid")
    state = params.get("CallStatus", "unknown")
    if call_id and state in TERMINAL_CALL_STATUSES:
        registry.end(call_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

