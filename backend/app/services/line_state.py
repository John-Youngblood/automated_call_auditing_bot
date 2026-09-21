"""Whether the show is taking calls.

One boolean, but changing it has to reach every dashboard -- two operators
disagreeing about this is how someone gets put on air after the show ended.

Closed does not mean stopped: the service keeps running and keeps serving the
dashboard. It just turns new callers away with a message telling them when to
call back, instead of an unreachable webhook and Twilio's generic error.
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
        """Open or close the line. Returns whether this changed anything.

        Idempotent: two operators clicking close a second apart should not
        produce two events, nor look like a failure to the second one.
        """
        if self._is_open == is_open:
            return False

        self._is_open = is_open
        logger.info("line %s to new callers", "opened" if is_open else "closed")
        self._broadcaster.publish(ServerEvent.line_changed(is_open).to_wire())
        return True
