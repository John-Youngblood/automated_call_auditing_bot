"""Clearing whoever is still holding.

Reached by closing the line, which does both halves in one action so no caller
is left waiting on a line nobody is watching. Each is played a goodbye rather
than cut off, since they have been waiting to get on air.

An operator action, never a shutdown hook: a deploy and a wrap-up arrive as the
same SIGTERM. See docs/architecture.md.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.config import Settings
from app.schemas.calls import CallStatus
from app.services.call_registry import CallRegistry
from app.telephony import speak_and_hangup
from app.telephony.rest import client_from_settings

logger = logging.getLogger(__name__)

#: Concurrent hang-ups. The operator is waiting on this request, so it runs
#: wider than reconciliation's background lookups.
_CONCURRENCY = 20

#: Hard ceiling on the whole operation. An operator who clicked this needs an
#: answer, not a spinner, and any caller we fail to reach is reported rather
#: than silently retried forever.
_DEADLINE_SECONDS = 20.0


@dataclass(slots=True)
class DrainResult:
    """``failed`` matters: those callers are still connected and still hearing
    hold music, so silence must not imply success."""

    ended: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    #: True when the deadline cut the operation short, so `failed` may
    #: understate the problem -- some callers were never attempted.
    timed_out: bool = False


async def hang_up_holders(registry: CallRegistry, settings: Settings) -> DrainResult:
    """Hang up on everyone currently being screened or holding.

    Closing the line sets the flag first, so a caller dialling while this runs
    is turned away rather than joining a queue being emptied.

    Never raises. Marks ENDED only *after* Twilio confirms, so a caller we
    failed to reach stays visible rather than vanishing while still connected.
    """
    open_calls = registry.open_calls()
    if not open_calls:
        return DrainResult()

    client = client_from_settings(settings)
    if client is None:
        logger.error(
            "cannot hang up holders: no Twilio REST credentials "
            "(set TWILIO_ACCOUNT_SID and an API key)"
        )
        return DrainResult(failed=[c.call_id for c in open_calls])

    twiml = speak_and_hangup(
        audio_url=settings.resolved_closing_audio_url,
        text=settings.closing_message,
        tts_voice=settings.tts_voice,
    ).body
    result = DrainResult()
    semaphore = asyncio.Semaphore(_CONCURRENCY)

    async def one(call_id: str) -> tuple[str, bool]:
        async with semaphore:
            return call_id, await client.end_call(call_id, twiml)

    logger.info("clearing the queue: %s caller(s) to hang up", len(open_calls))
    async with client:
        try:
            outcomes = await asyncio.wait_for(
                asyncio.gather(
                    *(one(c.call_id) for c in open_calls), return_exceptions=True
                ),
                timeout=_DEADLINE_SECONDS,
            )
        except TimeoutError:
            logger.error("clearing the queue timed out after %ss", _DEADLINE_SECONDS)
            return DrainResult(failed=[c.call_id for c in open_calls], timed_out=True)

    for call, outcome in zip(open_calls, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning("could not hang up call_id=%s: %s", call.call_id, outcome)
            result.failed.append(call.call_id)
            continue
        _, ok = outcome
        if ok:
            registry.set_status(call.call_id, CallStatus.ENDED)
            result.ended.append(call.call_id)
        else:
            logger.error(
                "could not hang up call_id=%s -- caller may still be holding",
                call.call_id,
            )
            result.failed.append(call.call_id)

    logger.info("queue cleared: %s ended, %s failed", len(result.ended), len(result.failed))
    return result
