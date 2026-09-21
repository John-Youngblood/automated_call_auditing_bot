"""Accepting and rejecting a caller -- the half that reaches the caller.

Both decisions were placeholders until now, and the reject one was the more
dangerous of the two. Marking a call REJECTED takes it out of ``open_calls``,
so it leaves the dashboard; without a carrier command the caller stayed in
Twilio's hold queue hearing music. They were stranded *and* invisible: not in
the queue, not cleared by closing the line (which only walks open calls), not
recovered by reconciliation, still billed by the minute, and still holding a
slot against Twilio's 1000-call queue cap -- until they gave up.

Accept had the same shape but fails loudly in practice: nobody comes on air
and you notice within seconds. Reject looked completely fine on the dashboard,
which is exactly why it needed fixing first.

Ordering rule, shared with :mod:`app.services.drain`: the carrier acts first
and local state follows. A decision we could not deliver leaves the call where
it was, so the dashboard is never more optimistic than the phone line.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.telephony import dial, speak_and_hangup
from app.telephony.rest import client_from_settings

logger = logging.getLogger(__name__)


class TelephonyUnavailable(RuntimeError):
    """No usable Twilio credentials, so a decision cannot reach the caller.

    Deliberately an error rather than a silent success. An earlier placeholder
    client logged the command and returned True, which made accept and reject
    look like they worked in any environment without credentials -- the whole
    point of a screening dashboard is that what it shows matches what the
    caller is experiencing.
    """


#: Where Twilio reports the outcome of a bridge. Kept beside the route that
#: serves it rather than imported, to avoid services depending on routes.
DIAL_COMPLETE_PATH = "/webhook/dial-complete"


async def put_on_air(call_id: str, destination: str, settings: Settings) -> bool:
    """Bridge a held caller to a human.

    ``destination`` is whatever the operator answers on. Strict about
    success: a call that has already ended is a *failure* here, because
    nobody is being connected and showing the operator a live guest who hung
    up thirty seconds ago is worse than showing them an error.
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

    Lenient about a call that has already ended -- the goal is "this caller is
    no longer holding", which a caller who hung up first has satisfied.
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
