"""The fan-out hub's back-pressure policy.

The property under test is the one the whole design rests on: a dashboard that
stops reading must not be able to block a publisher.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.broadcaster import CLOSE_SENTINEL, Broadcaster


async def test_subscriber_receives_published_events() -> None:
    hub = Broadcaster(queue_max=10)

    async with hub.subscribe() as sub:
        hub.publish({"type": "call.incoming"})
        assert await sub.next_event() == {"type": "call.incoming"}


async def test_unsubscribes_on_context_exit() -> None:
    hub = Broadcaster(queue_max=10)

    async with hub.subscribe():
        assert hub.subscriber_count == 1
    assert hub.subscriber_count == 0


async def test_publish_never_blocks_on_a_full_queue() -> None:
    hub = Broadcaster(queue_max=2)

    async with hub.subscribe() as sub:
        # Publish well past capacity from a client that never reads.
        for n in range(50):
            hub.publish({"n": n})

        assert sub.dropped == 48
        # Oldest dropped, newest kept: the client converges on "now".
        assert await sub.next_event() == {"n": 48}
        assert await sub.next_event() == {"n": 49}


async def test_a_stalled_subscriber_does_not_starve_others() -> None:
    hub = Broadcaster(queue_max=2)

    async with hub.subscribe(kind="stalled") as stalled, hub.subscribe(kind="healthy") as healthy:
        for n in range(10):
            hub.publish({"n": n})
            # The healthy client keeps up.
            assert await healthy.next_event() == {"n": n}

    assert stalled.dropped == 8
    assert healthy.dropped == 0


async def test_close_all_wakes_writers() -> None:
    hub = Broadcaster(queue_max=4)

    async with hub.subscribe() as sub:
        hub.publish({"n": 1})
        hub.close_all()

        assert await sub.next_event() == {"n": 1}
        assert await sub.next_event() is CLOSE_SENTINEL


async def test_close_all_preempts_a_full_queue() -> None:
    """Shutdown outranks queued events, so it must land even when full."""
    hub = Broadcaster(queue_max=2)

    async with hub.subscribe() as sub:
        for n in range(10):
            hub.publish({"n": n})
        hub.close_all()

        events = [await asyncio.wait_for(sub.next_event(), timeout=1) for _ in range(2)]
        assert CLOSE_SENTINEL in events


@pytest.mark.parametrize("queue_max", [1, 5, 250])
async def test_queue_bound_is_respected(queue_max: int) -> None:
    hub = Broadcaster(queue_max=queue_max)

    async with hub.subscribe() as sub:
        for n in range(queue_max + 20):
            hub.publish({"n": n})
        assert sub.queue.qsize() == queue_max
