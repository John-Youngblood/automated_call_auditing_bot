"""In-process pub/sub hub for fanning events out to dashboard websockets.

Design constraint that drives everything here: **the audio path must never
wait on a dashboard.** A single agent with a backgrounded browser tab, a flaky
VPN, or a paused debugger must not be able to stall transcription for the call
that is currently ringing.

So:

* every subscriber owns a *bounded* queue;
* :meth:`Broadcaster.publish` is synchronous and never blocks -- it only ever
  calls ``put_nowait``;
* when a queue is full the **oldest** event is discarded, not the newest. A
  slow dashboard shows a gap in history rather than falling further behind
  real time, which is the right trade-off for live screening.

Scaling out: this hub is process-local. Two uvicorn workers would each see
only their own share of subscribers, so the deployment runs one worker
deliberately. To go wider, keep this interface and back ``publish`` with Redis
pub/sub (or NATS), having each process subscribe to the channel and forward
into these local queues.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Final

logger = logging.getLogger(__name__)

#: Pushed into a queue to tell its writer task to shut down cleanly.
CLOSE_SENTINEL: Final = object()

_ids = count(1)


# eq=False keeps the default identity hash/equality: subscribers live in a set
# and must be distinguishable even when two clients hold equal field values.
@dataclass(slots=True, eq=False)
class Subscriber:
    """One dashboard connection's outbound mailbox."""

    id: int
    queue: asyncio.Queue[Any]
    dropped: int = 0
    tags: dict[str, str] = field(default_factory=dict)

    async def next_event(self) -> Any:
        """Block until the next event (or :data:`CLOSE_SENTINEL`) is ready."""
        return await self.queue.get()


class Broadcaster:
    def __init__(self, queue_max: int = 250) -> None:
        self._queue_max = queue_max
        self._subscribers: set[Subscriber] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @asynccontextmanager
    async def subscribe(self, **tags: str) -> AsyncIterator[Subscriber]:
        """Register a subscriber for the duration of the ``async with`` block.

        The context manager guarantees de-registration even if the connection
        drops mid-send, which is the usual way websockets end.
        """
        sub = Subscriber(id=next(_ids), queue=asyncio.Queue(maxsize=self._queue_max), tags=tags)
        self._subscribers.add(sub)
        logger.info("dashboard subscribed id=%s total=%s", sub.id, self.subscriber_count)
        try:
            yield sub
        finally:
            self._subscribers.discard(sub)
            logger.info(
                "dashboard unsubscribed id=%s total=%s dropped=%s",
                sub.id,
                self.subscriber_count,
                sub.dropped,
            )

    def publish(self, payload: dict[str, Any]) -> None:
        """Fan ``payload`` out to every subscriber. Never blocks, never raises.

        Synchronous on purpose: with no ``await`` inside, the subscriber set
        cannot change underneath us, so no lock is needed and callers on the
        hot audio path pay only a dict copy per subscriber.
        """
        for sub in tuple(self._subscribers):
            try:
                sub.queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop the oldest so the client converges on "now".
                try:
                    sub.queue.get_nowait()
                    sub.queue.task_done()
                except asyncio.QueueEmpty:  # pragma: no cover - drained concurrently
                    pass
                sub.dropped += 1
                if sub.dropped == 1 or sub.dropped % 100 == 0:
                    logger.warning(
                        "dashboard id=%s is slow, dropped %s event(s)", sub.id, sub.dropped
                    )
                # Lost a race with another publisher; skipping is fine.
                with contextlib.suppress(asyncio.QueueFull):
                    sub.queue.put_nowait(payload)

    def close_all(self) -> None:
        """Ask every writer task to finish. Used on application shutdown."""
        for sub in tuple(self._subscribers):
            try:
                sub.queue.put_nowait(CLOSE_SENTINEL)
            except asyncio.QueueFull:
                # Make room -- shutdown outranks any queued event.
                try:
                    sub.queue.get_nowait()
                    sub.queue.put_nowait(CLOSE_SENTINEL)
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                    pass
