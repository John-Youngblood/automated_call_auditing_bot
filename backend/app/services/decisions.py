"""Accepting and rejecting a caller -- the half that reaches the caller.

Ordering rule, shared with :mod:`app.services.drain`: Twilio acts first, local
state follows. A decision we could not deliver leaves the call where it was, so
the dashboard is never more optimistic than the phone line.

That matters most for reject. REJECTED takes a call out of ``open_calls``, so
it leaves the dashboard -- if the hang-up silently failed, the caller would
still be in Twilio's hold queue, invisible and billed, until they gave up.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.telephony import dial, speak_and_hangup
from app.telephony.rest import client_from_settings

logger = logging.getLogger(__name__)


class TelephonyUnavailable(RuntimeError):
    """No usable Twilio credentials, so a decision cannot reach the caller.

    An error rather than a silent success: the point of the dashboard is that
    what it shows matches what the caller is experiencing.
    """


#: Where Twilio reports the outcome. Declared here rather than imported from
#: the route, so services do not depend on routes.
DIAL_COMPLETE_PATH = "/webhook/dial-complete"


async def put_on_air(call_id: str, destination: str, settings: Settings) -> bool:
    """Bridge a held caller to the host.

    Strict about success: a call that has already ended is a *failure* here,
    because nobody is being connected.
    """
    client = client_from_settings(settings)
    if client is None:
        raise TelephonyUnavailable(
            "no Twilio REST credentials -- set TWILIO_ACCOUNT_SID and an API key"
        )

    twiml = dial(
        destination,
        action_url=f"{settings.public_base_url.rstrip('/')}{DIAL_COMPLETE_PATH}",
    ).body
    async with client:
        ok = await client.send_twiml(call_id, twiml)

    if ok:
        logger.info("bridged call_id=%s to %s", call_id, destination)
    else:
        logger.error("could not bridge call_id=%s to %s", call_id, destination)
    return ok


async def turn_away(call_id: str, settings: Settings) -> bool:
    """Tell a caller they are not getting on air, then hang up.

    Lenient about a call that has already ended: the goal is "no longer
    holding", which a caller who hung up first has satisfied.
    """
    client = client_from_settings(settings)
    if client is None:
        raise TelephonyUnavailable(
            "no Twilio REST credentials -- set TWILIO_ACCOUNT_SID and an API key"
        )

    twiml = speak_and_hangup(
        audio_url=settings.resolved_reject_audio_url,
        text=settings.reject_message,
        tts_voice=settings.tts_voice,
    ).body
    async with client:
        ok = await client.end_call(call_id, twiml)

    if ok:
        logger.info("declined call_id=%s", call_id)
    else:
        logger.error("could not decline call_id=%s -- caller may still be holding", call_id)
    return ok
