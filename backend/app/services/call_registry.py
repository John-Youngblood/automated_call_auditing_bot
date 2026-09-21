"""Authoritative state for calls, live and recently finished.

Single writer: every mutation goes through a method here and publishes the
matching event, so no code path can change a call without the dashboards
hearing about it.

One dict holds both the calls being screened and the last ``history_size``
finished ones -- a call does not move when it finishes, it just stops being
open. All in memory, so history resets on restart. See docs/architecture.md.
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
        """Finished calls, newest first. Bounded by ``history_size``."""
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

        Straight to ON_HOLD: being queued at Twilio means they are past the
        greeting. Returns ``None`` if the call is already known -- a caller can
        be mid-webhook while this runs, and clobbering their transcript with a
        blank recovered row would destroy the one thing that survived.
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
            # Their real start time, not ours -- the queue sorts by it, so
            # using "now" would send everyone recovered to the back.
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

        Arrives complete in one webhook, so there is no partial state.
        """
        call = self._calls.get(call_id)
        if call is None:
            logger.warning("speech result for unknown call_id=%s, dropping", call_id)
            return None

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
        self._publish(ServerEventType.CALL_UPDATED, call)
        return call

    def set_status(self, call_id: str, status: CallStatus) -> Call:
        call = self.require(call_id)
        if call.status is status:
            return call
        call.status = status
        if status is CallStatus.ACCEPTED:
            call.was_accepted = True
        # A terminal status ends *screening*, so stamp the time here -- without
        # it the queue timer keeps counting on calls nobody is screening.
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

    def end_on_air(self, call_id: str, seconds: int | None = None) -> Call | None:
        """Record how long a caller was on air, and mark the call over.

        ``None`` seconds is meaningful: the bridge never connected. Not zero.
        """
        call = self._calls.get(call_id)
        if call is None:
            return None
        call.on_air_seconds = seconds
        return self.set_status(call_id, CallStatus.ENDED)

    def end(self, call_id: str) -> Call | None:
        """Mark a call finished. Safe to call more than once."""
        call = self._calls.get(call_id)
        if call is None:
            return None
        if call.ended_at is None:
            call.ended_at = datetime.now(UTC)
        # REJECTED survives -- we hung up on them, so the call ending is a
        # consequence of that. ACCEPTED does not: it means "on air right now".
        if call.status is not CallStatus.REJECTED:
            call.status = CallStatus.ENDED
        self._publish(ServerEventType.CALL_ENDED, call)
        self._prune_terminal()
        return call

    def forget(self, call_id: str) -> None:
        """Evict a call from memory."""
        self._calls.pop(call_id, None)

    def publish(self, event: ServerEvent) -> None:
        """Publish an event that is not a state change. Keeps the broadcaster
        private to this class."""
        self._broadcaster.publish(event.to_wire())

    # -- internals ----------------------------------------------------------
    def _prune_terminal(self) -> None:
        """Drop the oldest finished calls. The only thing bounding memory."""
        finished = sorted(
            (call for call in self._calls.values() if not call.is_open),
            key=lambda call: call.ended_at or call.started_at,
        )
        for call in finished[: max(0, len(finished) - self._history_size)]:
            self.forget(call.call_id)

    def _publish(self, event_type: ServerEventType, call: Call) -> None:
        self._broadcaster.publish(ServerEvent.call_event(event_type, call).to_wire())  # type: ignore[arg-type]
