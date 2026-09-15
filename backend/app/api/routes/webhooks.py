"""Inbound call webhook.

    POST /webhook/incoming-call

The provider hits this the instant a call arrives and waits (a couple of
seconds, then it gives up and plays an error to the caller) for a document
describing what to do. We answer with three instructions:

1. **answer** -- implicit; returning a 200 with a valid document answers it,
2. **play** the MP3 greeting,
3. **connect** a two-way media stream to ``/ws/audio-stream``.

...unless the caller is on the blocklist, in which case the call is refused
before any of that happens. The blocklist is checked first and read from
memory, so a blocked caller never reaches the media stream, never opens a
Deepgram session, and never appears in the live queue.

The response dialect follows ``TELEPHONY_PROVIDER``: TwiML (XML) for Twilio,
NCCO (JSON) for Vonage. See :mod:`app.telephony`.

Keep this handler fast and side-effect-light. Anything slow -- CRM lookups,
LLM calls, database writes -- belongs on the websocket side or in a background
task, not between the caller and their greeting.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response

from app.api.deps import BlocklistDep, HistoryDep, RegistryDep, SettingsDep
from app.schemas.calls import Call, Caller, CallStatus
from app.telephony import CallPlan, get_renderer
from app.telephony.signature import verify_twilio_signature

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telephony"])

AUDIO_STREAM_PATH = "/ws/audio-stream"


async def _read_parameters(request: Request) -> dict[str, str]:
    """Normalise the request body to a flat string map.

    Twilio posts ``application/x-www-form-urlencoded``; Vonage posts JSON.
    Accept either so the endpoint works unchanged across providers.
    """
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid JSON body") from None
        if not isinstance(payload, dict):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "expected a JSON object")
        return {str(k): str(v) for k, v in payload.items()}

    form = await request.form()
    return {str(k): str(v) for k, v in form.items()}


def _public_url(request: Request, settings: SettingsDep) -> str:
    """The URL the provider *thinks* it requested.

    Signature validation hashes the URL, so behind a tunnel or load balancer
    the locally-observed ``request.url`` (http://backend:8000/...) is wrong.
    Rebuild it from the configured public origin instead of trusting
    X-Forwarded-* headers, which a caller could also set.
    """
    base = settings.public_base_url.rstrip("/")
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{base}{request.url.path}{query}"


def _extract_caller(params: dict[str, str]) -> tuple[str, Caller, str | None]:
    """Pull the provider's call id and caller identity out of the payload."""
    # Twilio field names first, then Vonage, then a generated fallback so a
    # malformed payload still produces a traceable call rather than a 500.
    call_id = params.get("CallSid") or params.get("uuid") or f"local-{uuid.uuid4().hex[:12]}"
    caller = Caller(
        number=params.get("From") or params.get("from"),
        name=params.get("CallerName") or None,
        city=params.get("FromCity") or None,
        country=params.get("FromCountry") or None,
    )
    to_number = params.get("To") or params.get("to")
    return call_id, caller, to_number


@router.post(
    "/webhook/incoming-call",
    summary="Answer an inbound call, play the greeting, open the media stream",
    response_class=Response,
    responses={
        200: {
            "content": {"application/xml": {}, "application/json": {}},
            "description": "Provider call-control document (TwiML or NCCO)",
        },
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
    params = await _read_parameters(request)

    if settings.validate_webhook_signature:
        ok = verify_twilio_signature(
            auth_token=settings.twilio_auth_token,
            url=_public_url(request, settings),
            params=params,
            signature=request.headers.get("X-Twilio-Signature"),
        )
        if not ok:
            logger.warning("rejected unsigned/invalid call webhook from %s", request.client)
            raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid webhook signature")

    call_id, caller, to_number = _extract_caller(params)

    # Blocklist first, before anything expensive. An in-memory set lookup, so
    # the provider is not kept waiting on a disk read while the phone rings.
    if blocklist.is_blocked(caller.number):
        return await _refuse_blocked_call(
            call_id=call_id,
            caller=caller,
            to_number=to_number,
            settings=settings,
            history=history,
        )

    registry.register_incoming(call_id, caller=caller, to_number=to_number)

    plan = CallPlan(
        call_id=call_id,
        greeting_url=settings.resolved_greeting_url,
        stream_url=f"{settings.websocket_base_url}{AUDIO_STREAM_PATH}",
        # Echoed back on the stream's `start` frame. This is how the audio
        # socket learns which call it belongs to -- don't rely on the socket
        # telling us, and don't put anything secret in here.
        stream_parameters={
            "callId": call_id,
            **({"from": caller.number} if caller.number else {}),
        },
    )

    rendered = get_renderer(settings.telephony_provider).render(plan)
    logger.info(
        "answering call_id=%s from=%s -> stream %s",
        call_id,
        caller.number,
        plan.stream_url,
    )
    return Response(content=rendered.body, media_type=rendered.media_type)


async def _refuse_blocked_call(
    *,
    call_id: str,
    caller: Caller,
    to_number: str | None,
    settings: SettingsDep,
    history: HistoryDep,
) -> Response:
    """Turn a blocked caller away and leave a trace of the attempt.

    Deliberately does **not** put the call in the registry: it never rings on
    anyone's dashboard, which is the whole point of a blocklist. It is written
    straight to history instead, because repeat attempts by a blocked number
    are exactly what a moderator wants to see later -- a block that silently
    swallows evidence of harassment is worse than no record at all.
    """
    now = datetime.now(UTC)
    call = Call(
        call_id=call_id,
        status=CallStatus.BLOCKED,
        caller=caller,
        to_number=to_number,
        started_at=now,
        ended_at=now,
    )
    await history.record(call)

    rendered = get_renderer(settings.telephony_provider).render_reject(
        settings.blocked_call_reject_reason
    )
    logger.info(
        "refused blocked caller call_id=%s from=%s reason=%s",
        call_id,
        caller.number,
        settings.blocked_call_reject_reason,
    )
    return Response(content=rendered.body, media_type=rendered.media_type)


@router.post("/webhook/call-status", summary="Provider call-progress callbacks")
async def call_status(request: Request, registry: RegistryDep) -> dict[str, str]:
    """Terminal call events (``completed``, ``busy``, ``no-answer``).

    Point the provider's status callback here so the queue clears itself when a
    caller hangs up before anyone picks up -- the media stream closing is a
    good signal, but this one is authoritative.
    """
    params = await _read_parameters(request)
    call_id = params.get("CallSid") or params.get("uuid")
    call_state = params.get("CallStatus") or params.get("status") or "unknown"
    if call_id and call_state in {"completed", "busy", "failed", "no-answer", "canceled"}:
        await registry.end(call_id)
    return {"status": "ok"}
