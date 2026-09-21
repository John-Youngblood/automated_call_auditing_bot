"""Whether the show is taking calls.

One boolean, but it needs a home rather than a loose flag on ``app.state``,
because changing it has to reach every dashboard. Two operators disagreeing
about whether the line is open is how somebody gets put on air after the show
has ended.

Closed does not mean stopped. The service keeps running, keeps serving the
dashboard, and keeps whatever callers are already on hold -- it just turns new
callers away with a spoken message instead of queueing them. That distinction
is the whole point: an unreachable webhook makes Twilio play a caller a
generic error, while a closed line tells them when to call back.
"""

from __future__ import annotations

import logging

from app.schemas.events import ServerEvent
from app.services.broadcaster import Broadcaster

logger = logging.getLogger(__name__)


class LineState:
    def __init__(self, broadcaster: Broadcaster, *, is_open: bool = True) -> None:
        self._broadcaster = broadcaster
        self._is_open = is_open

    @property
    def is_open(self) -> bool:
        return self._is_open

    def set_open(self, is_open: bool) -> bool:
        """Open or close the line. Returns whether this actually changed it.

        Idempotent on purpose: two operators clicking "close" a second apart
        should not produce two events, and the second click should not look
        like a failure to the person who made it.
        """
        if self._is_open == is_open:
            return False

        self._is_open = is_open
        logger.info("line %s to new callers", "opened" if is_open else "closed")
        self._broadcaster.publish(ServerEvent.line_changed(is_open).to_wire())
        return True
