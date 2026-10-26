"""Authoritative state for the calls in play.

Single writer: every mutation goes through a method here, which saves the call
and publishes the matching event, so no code path can change a call without it
being stored and the dashboards hearing about it.

Memory holds only the calls still in play -- being screened, holding, or on
air. A call that finishes is saved and let go; history is read from the store
(app/services/store.py). See docs/architecture.md.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.schemas.calls import TERMINAL_STATUSES, Call, Caller, CallStatus, Contact, utc_now
from app.schemas.events import ServerEvent, ServerEventType
from app.services.broadcaster import Broadcaster
from app.services.store import Store

logger = logging.getLogger(__name__)

#: Terminal statuses reported to the dashboard as "this call is over" rather
#: than "this call changed" -- everything except ACCEPTED, which is a handover.
#: Also when a call leaves memory: an accepted call is still on air.
_ENDED_STATUSES = frozenset({CallStatus.REJECTED, CallStatus.ENDED})


class CallRegistry:
    def __init__(self, broadcaster: Broadcaster, store: Store) -> None:
        self._broadcaster = broadcaster
        self._store = store
        self._calls: dict[str, Call] = {}
        # A warning from opening the store reaches dashboards in the snapshot;
        # only one that appears after this is news.
        self._announced_warning = store.warning

    # -- reads --------------------------------------------------------------
    def get(self, call_id: str) -> Call | None:
        return self._calls.get(call_id)

    def open_calls(self) -> list[Call]:
        """Calls a dashboard should currently show, oldest first."""
        return sorted(
            (c for c in self._calls.values() if c.is_open),
            key=lambda c: c.started_at,
        )

    # -- writes -------------------------------------------------------------
    def register_incoming(
        self,
        call_id: str,
        caller: Caller | None = None,
        to_number: str | None = None,
    ) -> Call:
        """Record a call from the provider webhook.

        Idempotent: Twilio retries webhooks, and a retry must not duplicate
        the queue row.
        """
        existing = self._calls.get(call_id)
        if existing is not None:
            logger.info("duplicate incoming webhook for call_id=%s, ignoring", call_id)
            return existing

        call = Call(call_id=call_id, caller=caller or Caller(), to_number=to_number)
        self._calls[call_id] = call
        logger.info("call queued call_id=%s from=%s", call_id, call.caller.number)
        self._commit(ServerEventType.CALL_INCOMING, call)
        return call

    def register_recovered(
        self,
        call_id: str,
        *,
        caller: Caller | None = None,
        to_number: str | None = None,
        started_at: datetime | None = None,
    ) -> Call | None:
        """Put a caller Twilio still has on hold back on the dashboard.

        Straight to ON_HOLD: being queued at Twilio means they are past the
        greeting. What they said, and where their number is from, come from
        the saved row when there is one -- Twilio never had either. Returns
        ``None`` if the call is already known: a caller can be mid-webhook
        while this runs, and clobbering their live row with a rebuilt one
        would throw away whatever arrived since. Likewise if the saved row
        has finished: they hung up while Twilio was being asked, and the
        answer is already out of date.
        """
        if call_id in self._calls:
            return None
        saved = self._store.get_call(call_id)
        if saved is not None and _finished(saved):
            logger.info("not recovering call_id=%s: it finished meanwhile", call_id)
            return None

        call = Call(
            call_id=call_id,
            status=CallStatus.ON_HOLD,
            caller=caller or Caller(),
            to_number=to_number,
            recovered=True,
        )
        if started_at is not None:
            # Their real start time, not ours -- the queue sorts by it, so
            # using "now" would send everyone recovered to the back.
            call.started_at = started_at
        if saved is not None:
            call.transcript = saved.transcript
            call.transcript_confidence = saved.transcript_confidence
            call.started_at = saved.started_at
            call.caller.number = saved.caller.number
            call.caller.location = saved.caller.location
            call.caller.verified = saved.caller.verified
            # A row that reached ON_HOLD had its speech result saved, so no
            # transcript there means they said nothing -- not that it was lost.
            call.recovered = saved.transcript is None and saved.status is not CallStatus.ON_HOLD
        self._profile(call)
        self._calls[call_id] = call
        logger.info(
            "recovered call call_id=%s from=%s waiting_since=%s transcript=%s",
            call_id,
            call.caller.number,
            call.started_at.isoformat(),
            "saved" if saved else "lost",
        )
        self._commit(ServerEventType.CALL_INCOMING, call)
        return call

    def set_transcript(
        self,
        call_id: str,
        text: str,
        confidence: float | None = None,
        *,
        caller: Caller | None = None,
    ) -> Call | None:
        """Record what the caller said and move them to awaiting a decision.

        Arrives complete in one webhook, so there is no partial state. For a
        call this process has not seen, ``caller`` (built from the same
        webhook) lets it be revived -- see :meth:`_revive`.
        """
        call = self._calls.get(call_id)
        revived = call is None
        if revived:
            call = self._revive(call_id, caller)
            if call is None:
                logger.warning("speech result for finished call_id=%s, dropping", call_id)
                return None
            self._calls[call_id] = call

        call.transcript = text or None
        call.transcript_confidence = confidence
        if call.status is CallStatus.SCREENING:
            call.status = CallStatus.ON_HOLD

        # Content at DEBUG only: a transcript is a member of the public
        # talking about themselves, so logging it should be a choice.
        logger.info(
            "transcript call_id=%s confidence=%s chars=%s", call_id, confidence, len(text or "")
        )
        logger.debug("transcript call_id=%s text=%r", call_id, text)
        self._commit(
            ServerEventType.CALL_INCOMING if revived else ServerEventType.CALL_UPDATED, call
        )
        return call

    def set_status(self, call_id: str, status: CallStatus) -> Call:
        """Move a call to ``status``. Raises KeyError for a call never seen.

        A call that finished while a decision was on its way to Twilio -- the
        caller hung up first -- comes back as saved, unchanged: what they did
        stands, and the decision has nobody left to reach.
        """
        call = self._calls.get(call_id)
        if call is None:
            if (saved := self._store.get_call(call_id)) is not None:
                return saved
            raise KeyError(f"unknown call {call_id!r}")
        if call.status is status:
            return call
        call.status = status
        if status is CallStatus.ACCEPTED:
            call.was_accepted = True
        # A terminal status ends *screening*, so stamp the time here -- without
        # it the queue timer keeps counting on calls nobody is screening.
        if status in TERMINAL_STATUSES and call.ended_at is None:
            call.ended_at = utc_now()
        logger.info("call status call_id=%s -> %s", call_id, status)
        event = (
            ServerEventType.CALL_ENDED
            if status in _ENDED_STATUSES
            else ServerEventType.CALL_UPDATED
        )

        self._commit(event, call)
        return call

    def end_on_air(self, call_id: str, seconds: int | None = None) -> Call | None:
        """Record how long a caller was on air, and mark the call over.

        ``None`` seconds is meaningful: the bridge never connected. Not zero.
        """
        call = self._calls.get(call_id)
        if call is None:
            self._close_saved(call_id, seconds)
            return None
        call.on_air_seconds = seconds
        return self.set_status(call_id, CallStatus.ENDED)

    def end(self, call_id: str) -> Call | None:
        """Mark a call finished. Safe to call more than once."""
        call = self._calls.get(call_id)
        if call is None:
            self._close_saved(call_id, None)
            return None
        if call.ended_at is None:
            call.ended_at = utc_now()
        # REJECTED survives -- we hung up on them, so the call ending is a
        # consequence of that. ACCEPTED does not: it means "on air right now".
        if call.status is not CallStatus.REJECTED:
            call.status = CallStatus.ENDED
        self._commit(ServerEventType.CALL_ENDED, call)
        return call

    def apply_contact(self, number: str, contact: Contact | None) -> None:
        """Show a contact change on the calls in play from ``number``."""
        for call in list(self._calls.values()):
            if call.caller.number == number:
                call.caller.contact = contact
                self._commit(ServerEventType.CALL_UPDATED, call)

    def sweep_stale(self) -> int:
        """End saved calls still open from before a restart that nobody has
        come back for.

        Only once the hold queue has been reconciled: anyone still holding is
        in memory by then, so whatever else was open ended while nobody was
        listening. Those never decided keep a NULL ``ended_at``, so history
        can say they ended during a restart rather than pretend to know when.
        """
        stale = [sid for sid in self._store.open_call_sids() if sid not in self._calls]
        swept = self._store.mark_ended_during_restart(stale)
        if swept:
            logger.info("%s call(s) ended while the backend was restarting", swept)
        self.announce_storage_warning()
        return swept

    def publish(self, event: ServerEvent) -> None:
        """Publish an event that is not a state change. Keeps the broadcaster
        private to this class."""
        self._broadcaster.publish(event.to_wire())

    def announce_storage_warning(self) -> None:
        """Tell every open dashboard, once, that storage has started failing.

        The store records the first failed write in its ``warning``; call this
        after a write that may have failed. Only a warning not yet sent goes
        out. Dashboards that connect later get it in their snapshot instead.
        """
        warning = self._store.warning
        if warning is not None and warning != self._announced_warning:
            self._announced_warning = warning
            self.publish(ServerEvent.error(warning, storage=True))

    # -- internals ----------------------------------------------------------
    def _revive(self, call_id: str, caller: Caller | None) -> Call | None:
        """Rebuild a call whose transcript arrived at a process that never
        saw it ring -- the caller was mid-greeting through a restart.

        From the saved row if there is one, which has their real start time.
        Not if that row has finished: a late or retried result must not put
        a finished caller back in the queue.
        """
        saved = self._store.get_call(call_id)
        if saved is not None:
            if _finished(saved):
                return None
            call = saved
        elif caller is not None:
            call = Call(call_id=call_id, caller=caller)
        else:
            return None
        call.status = CallStatus.ON_HOLD
        self._profile(call)
        logger.info("revived call_id=%s from=%s after a restart", call_id, call.caller.number)
        return call

    def _profile(self, call: Call) -> None:
        """Attach what the store knows about the caller's number."""
        if call.caller.number:
            call.caller.contact, call.caller.history = self._store.caller_profile(
                call.caller.number, exclude_sid=call.call_id
            )

    def _close_saved(self, call_id: str, on_air_seconds: int | None) -> None:
        """End a call that is not in memory: finished already, or begun
        before a restart. The store keeps whatever the row has settled."""
        if not self._store.close_call(call_id, on_air_seconds=on_air_seconds):
            self.announce_storage_warning()

    def _commit(self, event_type: ServerEventType, call: Call) -> None:
        """Save, tell the dashboards, then let go of a call that is over.

        Saved first, so nothing is broadcast that a restart would take back.
        A failed save still broadcasts: the call is real either way, and the
        operator needs to see it.
        """
        if not self._store.save_call(call):
            self.announce_storage_warning()
        self._broadcaster.publish(ServerEvent.call_event(event_type, call).to_wire())  # type: ignore[arg-type]
        if call.status in _ENDED_STATUSES:
            self._calls.pop(call.call_id, None)


def _finished(saved: Call) -> bool:
    """A saved call that is decided, or ended for real -- not to be put back
    in the queue. A row ended *during a restart* has not: that was a guess,
    and a caller Twilio still has proves it wrong."""
    return saved.status in (CallStatus.ACCEPTED, CallStatus.REJECTED) or (
        saved.status is CallStatus.ENDED and saved.ended_at is not None
    )
