"""Authoritative in-memory state for calls currently in the system.

Single writer of truth: every mutation goes through a method here, and every
method publishes the matching event. Route handlers stay thin and no code path
can change a call without the dashboards hearing about it.

Persistence: in-flight calls live only in memory -- screening decisions are
made within seconds, so losing them to a restart is acceptable. *Finished*
calls are written to ``call_history`` as they reach a terminal status, which
is what the moderation history view reads.

That write is the reason :meth:`CallRegistry.set_status` and
:meth:`CallRegistry.end` are async while everything else here is sync: they
fire once per call, well off the audio hot path, so awaiting a sub-millisecond
SQLite write there is simpler and safer than a background queue that would
swallow its own errors.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.schemas.calls import TERMINAL_STATUSES, Call, Caller, CallStatus
from app.schemas.events import ServerEvent, ServerEventType
from app.services.broadcaster import Broadcaster
from app.services.call_history import CallHistoryRepository
from app.services.phone import try_normalize

logger = logging.getLogger(__name__)

#: Terminal statuses reported to the dashboard as "this call is over" rather
#: than "this call changed" -- everything except ACCEPTED, which is a handover.
_ENDED_STATUSES = frozenset({CallStatus.REJECTED, CallStatus.ENDED, CallStatus.BLOCKED})

#: Finished calls kept in memory for late lookups before being evicted. They
#: are durable in ``call_history`` by then, so this only bounds RAM.
_RETAINED_TERMINAL_CALLS = 50


class CallRegistry:
    def __init__(
        self,
        broadcaster: Broadcaster,
        history: CallHistoryRepository | None = None,
    ) -> None:
        self._broadcaster = broadcaster
        #: Optional so tests and tooling can build a registry with no database.
        self._history = history
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

    def snapshot_event(self) -> ServerEvent:
        return ServerEvent.snapshot(self.open_calls())

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

    def update_caller(self, call_id: str, *, name: str | None, is_favorite: bool) -> Call | None:
        """Re-apply contact details to a call already in the queue.

        Called when someone names or stars a caller mid-call: the call was
        resolved when it arrived, so it holds stale details until this runs.
        """
        call = self._calls.get(call_id)
        if call is None:
            return None
        call.caller.name = name or call.caller.name
        call.caller.is_favorite = is_favorite
        self._publish(ServerEventType.CALL_UPDATED, call)
        return call

    async def set_transcript(
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
        if call.status is CallStatus.RINGING:
            call.status = CallStatus.SCREENING

        logger.info(
            "transcript call_id=%s confidence=%s chars=%s", call_id, confidence, len(text or "")
        )
        self._publish(ServerEventType.CALL_UPDATED, call)
        return call

    async def set_status(self, call_id: str, status: CallStatus) -> Call:
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
        await self._persist(call, status)
        return call

    async def end(self, call_id: str) -> Call | None:
        """Mark a call finished. Safe to call more than once."""
        call = self._calls.get(call_id)
        if call is None:
            return None
        if call.ended_at is None:
            call.ended_at = datetime.now(UTC)
        # A call already resolved by a human or by the blocklist keeps that
        # outcome -- the stream closing afterwards is a consequence of the
        # decision, not a new one.
        if call.status not in (CallStatus.ACCEPTED, CallStatus.REJECTED, CallStatus.BLOCKED):
            call.status = CallStatus.ENDED
        self._publish(ServerEventType.CALL_ENDED, call)
        await self._persist(call, call.status)
        return call

    def find_by_number(self, raw_number: str) -> list[Call]:
        """Open calls from a given caller, normalised on both sides.

        Used when a moderator blocks a number mid-show: the block has to reach
        whatever that caller has in flight right now, and the number they
        typed will not match the provider's formatting byte for byte.
        """
        target = try_normalize(raw_number)
        if target is None:
            return []
        return [
            call
            for call in self._calls.values()
            if call.is_open and try_normalize(call.caller.number) == target
        ]

    def forget(self, call_id: str) -> None:
        """Evict a finished call from memory (call from a reaper task)."""
        self._calls.pop(call_id, None)

    def publish(self, event: ServerEvent) -> None:
        """Publish an event that is not itself a state change (stream health,
        errors). Keeps the broadcaster private to this class."""
        self._broadcaster.publish(event.to_wire())

    # -- internals ----------------------------------------------------------
    async def _persist(self, call: Call, status: CallStatus) -> None:
        """Write the call to history once it is finished.

        Upserts on call id, so a call that is rejected and then confirmed
        ended by the provider's status callback updates one row rather than
        creating two.
        """
        if status not in TERMINAL_STATUSES:
            return
        if self._history is not None:
            await self._history.record(call)
        self._prune_terminal()

    def _prune_terminal(self) -> None:
        """Drop the oldest finished calls from memory.

        Without this, ``_calls`` grows for the life of the process: nothing
        else removes an entry. Safe because anything evicted has already been
        written to ``call_history``.
        """
        finished = sorted(
            (call for call in self._calls.values() if not call.is_open),
            key=lambda call: call.ended_at or call.started_at,
        )
        for call in finished[: max(0, len(finished) - _RETAINED_TERMINAL_CALLS)]:
            self.forget(call.call_id)

    def _publish(self, event_type: ServerEventType, call: Call) -> None:
        self._broadcaster.publish(ServerEvent.call_event(event_type, call).to_wire())  # type: ignore[arg-type]
