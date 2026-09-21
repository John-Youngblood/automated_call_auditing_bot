"""Authoritative state for calls, live and recently finished.

Single writer of truth: every mutation goes through a method here, and every
method publishes the matching event. Route handlers stay thin and no code path
can change a call without the dashboards hearing about it.

Everything lives in memory. One dict holds both the calls being screened and
the last :attr:`history_size` finished ones, which is what the Call History
view reads -- so a finished call is not moved or copied anywhere, it simply
stops being open. Nothing is written to disk.

That means history resets when the process restarts. Acceptable for a screening
queue: decisions are made within seconds, a show runs for a couple of hours, and
the alternative is a database whose only reader is a list of recent calls.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.schemas.calls import TERMINAL_STATUSES, Call, Caller, CallStatus
from app.schemas.events import ServerEvent, ServerEventType
from app.services.broadcaster import Broadcaster

logger = logging.getLogger(__name__)

#: Terminal statuses reported to the dashboard as "this call is over" rather
#: than "this call changed" -- everything except ACCEPTED, which is a handover.
_ENDED_STATUSES = frozenset({CallStatus.REJECTED, CallStatus.ENDED})

#: Finished calls kept when no size is configured.
DEFAULT_HISTORY_SIZE = 200


class CallRegistry:
    def __init__(self, broadcaster: Broadcaster, history_size: int = DEFAULT_HISTORY_SIZE) -> None:
        self._broadcaster = broadcaster
        self._history_size = max(0, history_size)
        self._calls: dict[str, Call] = {}

    # -- reads --------------------------------------------------------------
    def get(self, call_id: str) -> Call | None:
        return self._calls.get(call_id)

    def require(self, call_id: str) -> Call:
        call = self._calls.get(call_id)
        if call is None:
            raise KeyError(f"unknown call {call_id!r}")
        return call

    def open_calls(self) -> list[Call]:
        """Calls a dashboard should currently show, oldest first."""
        return sorted(
            (c for c in self._calls.values() if c.is_open),
            key=lambda c: c.started_at,
        )

    def recent_calls(self, limit: int | None = None) -> list[Call]:
        """Finished calls, newest first. The Call History view.

        Bounded by ``history_size`` regardless of ``limit``: anything older has
        already been evicted by :meth:`_prune_terminal`.
        """
        finished = sorted(
            (c for c in self._calls.values() if not c.is_open),
            key=lambda c: c.ended_at or c.started_at,
            reverse=True,
        )
        return finished[:limit] if limit else finished

    def snapshot_event(self, line_open: bool = True) -> ServerEvent:
        return ServerEvent.snapshot(self.open_calls(), line_open=line_open)

    # -- writes -------------------------------------------------------------
    def register_incoming(
        self,
        call_id: str,
        caller: Caller | None = None,
        to_number: str | None = None,
    ) -> Call:
        """Record a call from the provider webhook.

        Idempotent: providers retry webhooks, and a retry must not create a
        duplicate row in the queue.
        """
        existing = self._calls.get(call_id)
        if existing is not None:
            logger.info("duplicate incoming webhook for call_id=%s, ignoring", call_id)
            return existing

        call = Call(call_id=call_id, caller=caller or Caller(), to_number=to_number)
        self._calls[call_id] = call
        logger.info("call queued call_id=%s from=%s", call_id, call.caller.number)
        self._publish(ServerEventType.CALL_INCOMING, call)
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

        Goes straight to ON_HOLD: they are past the greeting and the gather,
        which is why Twilio has them queued at all. There is no transcript to
        restore -- it only ever lived in the previous process's memory -- so
        ``recovered`` is set and the UI says as much.

        Returns ``None`` when the call is already known, which is the race
        worth getting right: a caller can be mid-webhook while reconciliation
        runs, and overwriting a live call that already has its transcript with
        a blank recovered one would destroy the very thing that survived.
        """
        if call_id in self._calls:
            return None

        call = Call(
            call_id=call_id,
            status=CallStatus.ON_HOLD,
            caller=caller or Caller(),
            to_number=to_number,
            recovered=True,
        )
        if started_at is not None:
            # Keep the caller's real start time so the queue timer shows how
            # long they have actually been waiting, not how long since we
            # rebooted. That number is what decides who to take first.
            call.started_at = started_at
        self._calls[call_id] = call
        logger.info(
            "recovered call call_id=%s from=%s waiting_since=%s",
            call_id,
            call.caller.number,
            call.started_at.isoformat(),
        )
        self._publish(ServerEventType.CALL_INCOMING, call)
        return call

    def set_transcript(
        self, call_id: str, text: str, confidence: float | None = None
    ) -> Call | None:
        """Record what the caller said and move them to awaiting a decision.

        Arrives complete, in one webhook, once the caller stops speaking --
        so there is nothing partial to reconcile and no ordering to get right.
        """
        call = self._calls.get(call_id)
        if call is None:
            logger.warning("speech result for unknown call_id=%s, dropping", call_id)
            return None

        call.transcript = text or None
        call.transcript_confidence = confidence
        if call.status is CallStatus.SCREENING:
            call.status = CallStatus.ON_HOLD

        # Length at INFO, content at DEBUG. A transcript is a member of the
        # public talking about themselves, so writing it into container logs
        # should be a choice someone makes rather than the default. Set
        # LOG_LEVEL=DEBUG locally when you want to read them.
        logger.info(
            "transcript call_id=%s confidence=%s chars=%s", call_id, confidence, len(text or "")
        )
        logger.debug("transcript call_id=%s text=%r", call_id, text)
        self._publish(ServerEventType.CALL_UPDATED, call)
        return call

    def set_status(self, call_id: str, status: CallStatus) -> Call:
        call = self.require(call_id)
        if call.status is status:
            return call
        call.status = status
        # A terminal status ends *screening*, so stamp the time here. An
        # accepted call may well continue with a human, but as far as this
        # dashboard is concerned it is done -- without this the queue's
        # duration timer keeps counting up on calls nobody is screening.
        if status in TERMINAL_STATUSES and call.ended_at is None:
            call.ended_at = datetime.now(UTC)
        logger.info("call status call_id=%s -> %s", call_id, status)
        event = (
            ServerEventType.CALL_ENDED
            if status in _ENDED_STATUSES
            else ServerEventType.CALL_UPDATED
        )

        self._publish(event, call)
        self._prune_terminal()
        return call

    def end(self, call_id: str) -> Call | None:
        """Mark a call finished. Safe to call more than once."""
        call = self._calls.get(call_id)
        if call is None:
            return None
        if call.ended_at is None:
            call.ended_at = datetime.now(UTC)
        # A call already resolved by a human keeps that outcome -- the caller
        # hanging up afterwards is a consequence of the decision, not a new one.
        if call.status not in (CallStatus.ACCEPTED, CallStatus.REJECTED):
            call.status = CallStatus.ENDED
        self._publish(ServerEventType.CALL_ENDED, call)
        self._prune_terminal()
        return call

    def forget(self, call_id: str) -> None:
        """Evict a call from memory."""
        self._calls.pop(call_id, None)

    def publish(self, event: ServerEvent) -> None:
        """Publish an event that is not itself a state change (stream health,
        errors). Keeps the broadcaster private to this class."""
        self._broadcaster.publish(event.to_wire())

    # -- internals ----------------------------------------------------------
    def _prune_terminal(self) -> None:
        """Drop the oldest finished calls.

        This is the only thing bounding memory, and it is also what makes
        ``history_size`` the real retention limit: nothing else removes an
        entry, so without it ``_calls`` would grow for the life of the process.
        """
        finished = sorted(
            (call for call in self._calls.values() if not call.is_open),
            key=lambda call: call.ended_at or call.started_at,
        )
        for call in finished[: max(0, len(finished) - self._history_size)]:
            self.forget(call.call_id)

    def _publish(self, event_type: ServerEventType, call: Call) -> None:
        self._broadcaster.publish(ServerEvent.call_event(event_type, call).to_wire())  # type: ignore[arg-type]
