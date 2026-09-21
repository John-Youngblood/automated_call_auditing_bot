"""Guarding the dashboard surface.

Applied to ``/api/*`` and the dashboard websocket. Never to ``/webhook/*`` --
Twilio carries no credentials of ours, and those are authenticated by
signature instead.

The websocket takes its token through ``Sec-WebSocket-Protocol`` rather than a
query string, because browsers cannot set headers on a WebSocket and a token
in the URL would be written to every access log it passes through.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, WebSocket, status
from starlette.requests import HTTPConnection

from app.services.sessions import Sessions

#: Sent by the browser as `new WebSocket(url, ["bearer", token])`.
WS_PROTOCOL = "bearer"


def provide_sessions(conn: HTTPConnection) -> Sessions:
    return conn.app.state.sessions


SessionsDep = Annotated[Sessions, Depends(provide_sessions)]


def bearer_token(conn: HTTPConnection) -> str | None:
    """The token from an ``Authorization: Bearer`` header, if there is one."""
    header = conn.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else None


def websocket_token(websocket: WebSocket) -> str | None:
    """The token from the subprotocol list, if there is one."""
    offered = [
        p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",")
    ]
    if len(offered) < 2 or offered[0] != WS_PROTOCOL:
        return None
    return offered[1]


def require_session(conn: HTTPConnection, sessions: SessionsDep) -> None:
    """401 unless the request carries a valid token."""
    if sessions.is_valid(bearer_token(conn)):
        return
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "sign in to the dashboard first",
        headers={"WWW-Authenticate": "Bearer"},
    )


RequireSession = Depends(require_session)
