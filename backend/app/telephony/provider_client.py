"""Outbound control of a live call: the provider's REST API.

Everything so far has been the provider talking to us. This is the other
direction -- telling the provider to do something to a call that is already in
progress: bridge it to a human, or hang it up.

    dashboard decision  ->  TelephonyClient  ->  provider REST API  ->  caller

**All of this is a placeholder.** The methods log exactly what they would send
and report success, so the moderation flow can be exercised end to end without
credentials. Nothing reaches a carrier. Swap in :class:`TwilioRestClient` (or
your own) once you have an account, and the call sites do not change.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from app.config import Settings

logger = logging.getLogger(__name__)


@runtime_checkable
class TelephonyClient(Protocol):
    """Commands issued against a call that is already up.

    Implementations must not raise: every method returns a bool, because the
    callers are moderation actions where a provider outage should be reported
    to the operator, not turned into a 500.
    """

    #: True when calls are only simulated. Surfaced so callers can be honest
    #: in logs and responses rather than implying a carrier was contacted.
    is_placeholder: bool

    async def hangup(self, call_id: str, *, reason: str = "") -> bool: ...
    async def bridge(self, call_id: str, destination: str) -> bool: ...
    async def decline(self, call_id: str, *, message: str = "") -> bool: ...


class PlaceholderTelephonyClient:
    """Logs the intended REST call and pretends it worked.

    The log lines carry the real request shape, so wiring up the live client
    is a matter of transcribing them rather than re-deriving the API.
    """

    is_placeholder = True

    def __init__(self, settings: Settings) -> None:
        self._provider = settings.telephony_provider

    async def hangup(self, call_id: str, *, reason: str = "") -> bool:
        """Terminate a live call.

        Real implementation, Twilio::

            # POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Calls/{call_id}.json
            #   Status=completed
            #
            # from twilio.rest import Client
            # Client(account_sid, auth_token).calls(call_id).update(status="completed")

        Vonage::

            # PUT https://api.nexmo.com/v1/calls/{call_id}   {"action": "hangup"}

        Two things to handle when you make it real: the provider returns 404
        for a call that already ended (treat that as success -- the goal was
        "this call is not connected", and it is not), and the request is
        network I/O that can time out, so give it a short timeout and report
        the failure rather than hanging the moderator's click.
        """
        logger.warning(
            "[PLACEHOLDER] would hang up call_id=%s via %s REST API (reason=%r) "
            "-- no carrier was contacted",
            call_id,
            self._provider,
            reason or "unspecified",
        )
        return True

    async def bridge(self, call_id: str, destination: str) -> bool:
        """Connect the call to a human.

        Real implementation, Twilio -- redirect the live call out of the media
        stream and into a dial leg::

            # Client(...).calls(call_id).update(
            #     twiml=f"<Response><Dial>{destination}</Dial></Response>"
            # )

        Note this ends the media stream, so ``CallScreeningSession.close``
        runs; it deliberately does not overwrite an ACCEPTED status.
        """
        logger.warning(
            "[PLACEHOLDER] would bridge call_id=%s to %s via %s REST API "
            "-- no carrier was contacted",
            call_id,
            destination,
            self._provider,
        )
        return True

    async def decline(self, call_id: str, *, message: str = "") -> bool:
        """Decline a call the operator rejected.

        Prefer a spoken decline over a bare hangup -- a silent drop is
        indistinguishable from a network fault to the person on the other end::

            # Client(...).calls(call_id).update(
            #     twiml="<Response><Say>Sorry, no one is available.</Say><Hangup/></Response>"
            # )
        """
        logger.warning(
            "[PLACEHOLDER] would decline call_id=%s via %s REST API (message=%r) "
            "-- no carrier was contacted",
            call_id,
            self._provider,
            message or "none",
        )
        return True


def create_telephony_client(settings: Settings) -> TelephonyClient:
    """Return the client for the configured provider.

    Only the placeholder exists today. When you add a real one, gate it on
    credentials being present and fall back here, so a missing key degrades to
    "logs instead of acting" rather than crashing at startup.
    """
    return PlaceholderTelephonyClient(settings)
