"""Sizing the hold queue to HOLD_QUEUE_MAX_SIZE.

The size lives on Twilio's queue, not in any TwiML, so it is set once at
startup. A caller who arrives when the queue is full is dropped by Twilio
(``QueueResult=queue-full``), which is what makes the size a cap on how much
holding a show can cost.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import Settings
from app.telephony.rest import client_from_settings

logger = logging.getLogger(__name__)


async def size_hold_queue(settings: Settings) -> bool:
    """Apply HOLD_QUEUE_MAX_SIZE. Never raises, like reconciliation: a Twilio
    that cannot be reached must leave the queue at its old size, not stop the
    service from starting."""
    client = client_from_settings(settings)
    if client is None:
        logger.info("skipping hold queue sizing: no Twilio REST credentials")
        return False

    try:
        async with client:
            ok = await client.size_queue(settings.hold_queue_name, settings.hold_queue_max_size)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("hold queue sizing failed")
        return False

    if ok:
        logger.info(
            "hold queue %r holds at most %s callers",
            settings.hold_queue_name,
            settings.hold_queue_max_size,
        )
    else:
        logger.warning(
            "could not set hold queue %r to %s callers; Twilio's current size stands",
            settings.hold_queue_name,
            settings.hold_queue_max_size,
        )
    return ok
