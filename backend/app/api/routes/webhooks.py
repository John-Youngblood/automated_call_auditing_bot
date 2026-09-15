"""Twilio voice webhooks -- the whole call flow, in three handlers.

    POST /webhook/incoming-call     a call arrives
    POST /webhook/speech-result     the caller finished describing their reason
    POST /webhook/call-status       the call ended

The flow:

    ring ──▶ blocklist?  ── yes ──▶ <Reject>            (never answered, never billed)
              │
              no
              ▼
         <Gather input="speech"> greeting plays, Twilio listens
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
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response

from app.api.deps import BlocklistDep, HistoryDep, RegistryDep, SettingsDep
from app.schemas.calls import Call, Caller, CallStatus
from app.telephony import answer_and_gather, hold, reject
from app.telephony.signature import verify_twilio_signature

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telephony"])

SPEECH_RESULT_PATH = "/webhook/speech-result"


async def _form(request: Request) -> dict[str, str]:
    """Twilio posts ``application/x-www-form-urlencoded``, always."""
    form = await request.form()
    return {str(k): str(v) for k, v in form.items()}


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
    blocklist: BlocklistDep,
    history: HistoryDep,
) -> Response:
    params = await _form(request)
    _check_signature(request, params, settings)

    # Twilio's own id, falling back to a generated one so a malformed payload
    # still produces a traceable call rather than a 500.
    call_id = params.get("CallSid") or f"local-{uuid.uuid4().hex[:12]}"
    caller = Caller(
        number=params.get("From") or None,
        name=params.get("CallerName") or None,
        city=params.get("FromCity") or None,
        country=params.get("FromCountry") or None,
    )

    # Blocklist first, before anything expensive. An in-memory set lookup, so
    # the caller is not kept waiting on a disk read while the phone rings.
    if blocklist.is_blocked(caller.number):
        return await _refuse_blocked_call(call_id, caller, params.get("To"), settings, history)

    registry.register_incoming(call_id, caller=caller, to_number=params.get("To"))
    logger.info("call ringing call_id=%s from=%s", call_id, caller.number)

    return _twiml(
        answer_and_gather(
            greeting_url=settings.resolved_greeting_url,
            action_url=f"{settings.public_base_url.rstrip('/')}{SPEECH_RESULT_PATH}",
            speech_model=settings.speech_model,
            language=settings.speech_language,
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

    await registry.set_transcript(call_id, text, confidence)

    # Hold them. The call stays open until an operator accepts, rejects, or
    # the caller hangs up.
    return _twiml(hold(settings.hold_queue_name))


@router.post("/webhook/call-status", summary="Provider call-progress callbacks")
async def call_status(request: Request, registry: RegistryDep) -> dict[str, str]:
    """Terminal call events.

    Point Twilio's status callback here so the queue clears itself when a
    caller hangs up while on hold -- otherwise their card sits there until
    someone acts on a call that is already gone.
    """
    params = await _form(request)
    call_id = params.get("CallSid")
    state = params.get("CallStatus", "unknown")
    if call_id and state in {"completed", "busy", "failed", "no-answer", "canceled"}:
        await registry.end(call_id)
    return {"status": "ok"}


async def _refuse_blocked_call(
    call_id: str,
    caller: Caller,
    to_number: str | None,
    settings: SettingsDep,
    history: HistoryDep,
) -> Response:
    """Turn a blocked caller away and leave a trace of the attempt.

    Deliberately does **not** register the call: it never rings on anyone's
    dashboard, which is the point of a blocklist. It goes straight to history
    instead, because repeat attempts by a blocked number are exactly what a
    moderator wants to see later -- a block that silently swallows evidence of
    harassment is worse than no record at all.
    """
    now = datetime.now(UTC)
    await history.record(
        Call(
            call_id=call_id,
            status=CallStatus.BLOCKED,
            caller=caller,
            to_number=to_number,
            started_at=now,
            ended_at=now,
        )
    )
    logger.info("refused blocked caller call_id=%s from=%s", call_id, caller.number)
    return _twiml(reject(settings.blocked_call_reject_reason))
