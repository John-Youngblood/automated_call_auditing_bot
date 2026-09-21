"""Shared dependencies.

Long-lived singletons hang off ``app.state`` (populated in the lifespan) rather
than module globals, so tests can build an isolated app per test and nothing
leaks between them.

``HTTPConnection`` is the common base of ``Request`` and ``WebSocket``, which
lets one dependency serve both HTTP routes and websocket routes.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from starlette.requests import HTTPConnection

from app.config import Settings, get_settings
from app.services.broadcaster import Broadcaster
from app.services.call_registry import CallRegistry
from app.services.line_state import LineState


def provide_settings() -> Settings:
    return get_settings()


def provide_broadcaster(conn: HTTPConnection) -> Broadcaster:
    return conn.app.state.broadcaster


def provide_registry(conn: HTTPConnection) -> CallRegistry:
    return conn.app.state.registry


def provide_line(conn: HTTPConnection) -> LineState:
    return conn.app.state.line


SettingsDep = Annotated[Settings, Depends(provide_settings)]
BroadcasterDep = Annotated[Broadcaster, Depends(provide_broadcaster)]
RegistryDep = Annotated[CallRegistry, Depends(provide_registry)]
LineDep = Annotated[LineState, Depends(provide_line)]
