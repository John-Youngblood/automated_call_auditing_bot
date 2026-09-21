"""Dashboard-facing websocket.

    WS /ws/frontend

Broadcasts call state and live transcripts to every connected dashboard, and
accepts a small set of commands back (ping, accept, reject).

The two-task pattern
--------------------
Each connection runs a **reader** and a **writer** concurrently, and the first
one to finish cancels the other. This matters: a single loop that only wrote
would not notice a client vanishing until its next send (possibly minutes into
a silent call, holding a socket and a queue open the whole time), and a single
loop that only read could never push events. ``asyncio.wait`` with
``FIRST_COMPLETED`` is the standard shape for a duplex socket.

Events are pulled from a per-client bounded queue owned by the
:class:`~app.services.broadcaster.Broadcaster`, so a backgrounded browser tab
slows only itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.api.deps import BroadcasterDep, LineDep, RegistryDep, SettingsDep
from app.config import Settings
from app.schemas.calls import CallStatus
from app.schemas.events import ClientCommand, ClientCommandType, ServerEvent
from app.services.broadcaster import CLOSE_SENTINEL, Subscriber
from app.services.call_registry import CallRegistry
from app.services.decisions import TelephonyUnavailable, put_on_air, turn_away

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])


@router.websocket("/ws/frontend")
async def frontend_stream(
    websocket: WebSocket,
    broadcaster: BroadcasterDep,
    registry: RegistryDep,
    line: LineDep,
    settings: SettingsDep,
) -> None:
    await websocket.accept()

    async with broadcaster.subscribe(kind="dashboard") as subscriber:
        # Replay current state first. Without this, a dashboard opened
        # mid-call shows an empty queue until the next event happens to fire.
        await websocket.send_json(registry.snapshot_event(line.is_open).to_wire())

        reader = asyncio.create_task(
            _read_commands(websocket, registry, settings), name="dash-reader"
        )
        writer = asyncio.create_task(_write_events(websocket, subscriber), name="dash-writer")

        done, pending = await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            # Surface a genuine crash; a disconnect is ordinary.
            if (exc := task.exception()) is not None and not isinstance(exc, WebSocketDisconnect):
                logger.exception("dashboard task failed", exc_info=exc)

    # A close that fails just means the socket is already gone -- the client
    # vanished (WebSocketDisconnect, which is what Starlette raises when
    # uvicorn reports ClientDisconnected) or we already closed it
    # (RuntimeError). Neither is an error: there is nothing left to close.
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        await websocket.close()


async def _write_events(websocket: WebSocket, subscriber: Subscriber) -> None:
    """Drain this client's queue onto the wire until told to stop."""
    while True:
        payload = await subscriber.next_event()
        if payload is CLOSE_SENTINEL:
            return
        await websocket.send_json(payload)


async def _read_commands(
    websocket: WebSocket, registry: CallRegistry, settings: Settings
) -> None:
    """Handle inbound dashboard commands.

    Also the connection's liveness detector -- ``receive_json`` raising
    ``WebSocketDisconnect`` is how we learn the tab closed.
    """
    while True:
        try:
            raw = await websocket.receive_json()
        except WebSocketDisconnect:
            raise
        except ValueError:
            await websocket.send_json(ServerEvent.error("malformed JSON").to_wire())
            continue

        try:
            command = ClientCommand.model_validate(raw)
        except ValidationError as exc:
            await websocket.send_json(
                ServerEvent.error(f"unrecognised command: {exc.error_count()} problem(s)").to_wire()
            )
            continue

        await _dispatch(command, websocket, registry, settings)


async def _dispatch(
    command: ClientCommand,
    websocket: WebSocket,
    registry: CallRegistry,
    settings: Settings,
) -> None:
    match command.type:
        case ClientCommandType.PING:
            # Application-level heartbeat. Uvicorn also sends protocol pings,
            # but those are invisible to browser JS, so the client cannot use
            # them to tell "quiet" from "dead".
            await websocket.send_json(ServerEvent.pong().to_wire())

        case ClientCommandType.ACCEPT_CALL | ClientCommandType.REJECT_CALL:
            if not command.call_id:
                await websocket.send_json(ServerEvent.error("callId is required").to_wire())
                return
            accepting = command.type is ClientCommandType.ACCEPT_CALL
            call = registry.get(command.call_id)
            if call is None:
                await websocket.send_json(
                    ServerEvent.error("unknown call", call_id=command.call_id).to_wire()
                )
                return

            # Twilio first, local state second -- the same ordering the HTTP
            # routes use. This branch used to skip the carrier entirely, so a
            # reject over the socket left the caller holding while the
            # dashboard filed them as resolved.
            try:
                if accepting:
                    delivered = await put_on_air(
                        call.call_id, settings.host_phone_number, settings
                    )
                else:
                    delivered = await turn_away(call.call_id, settings)
            except TelephonyUnavailable as exc:
                await websocket.send_json(
                    ServerEvent.error(str(exc), call_id=command.call_id).to_wire()
                )
                return

            if not delivered:
                await websocket.send_json(
                    ServerEvent.error(
                        "could not reach the caller at Twilio; they may still be holding",
                        call_id=command.call_id,
                    ).to_wire()
                )
                return

            registry.set_status(
                command.call_id,
                CallStatus.ACCEPTED if accepting else CallStatus.REJECTED,
            )
