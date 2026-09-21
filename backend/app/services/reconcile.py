"""Rebuilding the queue from Twilio after a restart.

The problem this solves
-----------------------
Call state lives only in this process (see :mod:`app.services.call_registry`).
Callers on hold live in a Twilio ``<Enqueue>`` queue, which is *not* in this
process. A restart therefore desynchronises the two in the worst direction:
Twilio keeps playing hold music to people no operator can see, and nothing
ever fires to tell us they are there. They are not disconnected -- they are
stranded, which is harder to notice and worse for the caller.

Note that a fallback URL does not help here. Fallbacks fire when a webhook
request fails; a caller sitting in hold music is not making one.

What comes back, and what does not
----------------------------------
Recoverable from Twilio:   call SID, caller number, the number they dialled,
                           the carrier's caller-ID name, when the call started.
Gone for good:             **the transcript**, and the city/state/country
                           labels -- those arrive as webhook parameters and
                           Twilio does not keep them on the Call resource.

So a recovered caller reappears in the queue, correctly positioned by wait
time, with no stated reason for calling. ``Call.recovered`` marks them so the
dashboard says that plainly instead of showing what looks like a caller who
said nothing.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import Settings
from app.schemas.calls import Caller
from app.services.call_registry import CallRegistry
from app.telephony.rest import (
    LIVE_CALL_STATUSES,
    CallDetails,
    QueueMember,
    TwilioRestClient,
    client_from_settings,
)

logger = logging.getLogger(__name__)

#: Concurrent Call lookups. One request per waiting caller, so this is polite
#: to Twilio's rate limits while still finishing a full queue quickly.
_LOOKUP_CONCURRENCY = 10

async def reconcile_hold_queue(registry: CallRegistry, settings: Settings) -> int:
    """Put everyone Twilio still has on hold back on the dashboard.

    Returns how many calls were restored. Never raises: this runs in the
    background at boot, and a failure to reach Twilio must degrade to an empty
    queue with a log line, not a service that will not start.
    """
    try:
        return await _reconcile(registry, settings)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("hold-queue reconciliation failed; starting with an empty queue")
        return 0


async def _reconcile(registry: CallRegistry, settings: Settings) -> int:
    client = client_from_settings(settings)
    if client is None:
        logger.info(
            "skipping reconciliation: no Twilio REST credentials "
            "(set TWILIO_ACCOUNT_SID and an API key)"
        )
        return 0

    async with client:
        queue_sid = await client.find_queue_sid(settings.hold_queue_name)
        if queue_sid is None:
            return 0

        members = await client.list_queue_members(queue_sid)
        if not members:
            logger.info("hold queue %r is empty", settings.hold_queue_name)
            return 0

        logger.info("reconciling %s caller(s) still on hold", len(members))
        details = await _fetch_all(client, members)

    recovered = 0
    for member in members:
        detail = details.get(member.call_sid)
        if detail is not None and detail.status not in LIVE_CALL_STATUSES:
            logger.info(
                "skipping call_id=%s: Twilio reports status=%s", member.call_sid, detail.status
            )
            continue
        if _restore(registry, member, detail) is not None:
            recovered += 1

    logger.info("reconciliation complete: %s of %s restored", recovered, len(members))
    return recovered


async def _fetch_all(
    client: TwilioRestClient, members: list[QueueMember]
) -> dict[str, CallDetails]:
    """Look up each member's Call resource, bounded and fault-tolerant.

    A lookup that fails costs that caller their number, not their place in the
    queue -- better an anonymous row an operator can still act on than a
    caller who stays invisible.
    """
    semaphore = asyncio.Semaphore(_LOOKUP_CONCURRENCY)

    async def one(member: QueueMember) -> CallDetails | None:
        async with semaphore:
            return await client.fetch_call(member.call_sid)

    results = await asyncio.gather(
        *(one(m) for m in members),
        return_exceptions=True,
    )

    found: dict[str, CallDetails] = {}
    for member, result in zip(members, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("could not look up call_id=%s: %s", member.call_sid, result)
        elif result is not None:
            found[member.call_sid] = result
    return found


def _restore(registry: CallRegistry, member: QueueMember, detail: CallDetails | None):
    caller = Caller(
        number=detail.from_number if detail else None,
        name=detail.caller_name if detail else None,
        # No location: FromCity/FromState/FromCountry are webhook-only.
        location=None,
    )
    # Prefer the call's own start time over the enqueue time -- the caller has
    # been waiting since they dialled, through the greeting and the gather,
    # not just since Twilio parked them.
    started_at = (detail.started_at if detail else None) or member.enqueued_at
    return registry.register_recovered(
        member.call_sid,
        caller=caller,
        to_number=detail.to_number if detail else None,
        started_at=started_at,
    )
