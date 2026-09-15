"""The dashboard websocket protocol.

One envelope type in each direction. Adding a field is backwards-compatible;
renaming or removing one is not -- keep this file and
``frontend/src/types/events.ts`` in lockstep.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from app.schemas.calls import Call, CamelModel


class ServerEventType(StrEnum):
    """Backend -> dashboard."""

    #: Full state, sent once immediately on connect so a dashboard that joins
    #: mid-call is not blank.
    SNAPSHOT = "state.snapshot"
    CALL_INCOMING = "call.incoming"
    CALL_UPDATED = "call.updated"
    CALL_ENDED = "call.ended"
    PONG = "pong"
    ERROR = "error"


class ClientCommandType(StrEnum):
    """Dashboard -> backend."""

    PING = "ping"
    ACCEPT_CALL = "call.accept"
    REJECT_CALL = "call.reject"


class ServerEvent(CamelModel):
    type: ServerEventType
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    call_id: str | None = None
    #: Shape depends on ``type``; see the constructors below.
    data: dict[str, Any] = Field(default_factory=dict)

    # -- constructors: the only sanctioned way to build an event, so the
    #    payload shape for each type lives in exactly one place ------------
    @classmethod
    def snapshot(cls, calls: list[Call]) -> ServerEvent:
        return cls(
            type=ServerEventType.SNAPSHOT,
            data={"calls": [c.model_dump(by_alias=True, mode="json") for c in calls]},
        )

    @classmethod
    def call_event(
        cls,
        type_: Literal[
            ServerEventType.CALL_INCOMING,
            ServerEventType.CALL_UPDATED,
            ServerEventType.CALL_ENDED,
        ],
        call: Call,
    ) -> ServerEvent:
        return cls(
            type=type_,
            call_id=call.call_id,
            data={"call": call.model_dump(by_alias=True, mode="json")},
        )

    @classmethod
    def error(cls, message: str, call_id: str | None = None) -> ServerEvent:
        return cls(type=ServerEventType.ERROR, call_id=call_id, data={"message": message})

    @classmethod
    def pong(cls) -> ServerEvent:
        return cls(type=ServerEventType.PONG)

    def to_wire(self) -> dict[str, Any]:
        """JSON-ready dict with camelCase keys."""
        return self.model_dump(by_alias=True, mode="json")


class ClientCommand(CamelModel):
    """Inbound dashboard message. Validate before acting on it -- this arrives
    from a browser and is untrusted."""

    type: ClientCommandType
    call_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
