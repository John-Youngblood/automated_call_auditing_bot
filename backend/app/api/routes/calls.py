"""Screening decisions, the recent-call log, and closing the line.

    GET  /api/calls
    GET  /api/calls/{call_id}
    POST /api/calls/{call_id}/accept
    POST /api/calls/{call_id}/reject
    GET  /api/call-history
    POST /api/line/open
    POST /api/line/close

The decision endpoints exist alongside the equivalent websocket commands on
purpose. A decision is a one-shot action that either succeeded or did not, and
HTTP gives the dashboard a status code and a retry story for it; the websocket
stays a streaming channel. The resulting state change still reaches every
dashboard through the broadcaster, so all of them stay in sync either way.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import Field

from app.api.deps import LineDep, RegistryDep, SettingsDep
from app.schemas.calls import Call, CallStatus, CamelModel
from app.services.call_registry import CallRegistry
from app.services.decisions import TelephonyUnavailable, put_on_air, turn_away
from app.services.drain import hang_up_holders as drain_queue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["calls"])


@router.get("/calls", summary="Calls currently in the screening queue")
async def list_calls(registry: RegistryDep) -> list[Call]:
    return registry.open_calls()


@router.get("/call-history", summary="Recent finished calls")
async def call_history(
    registry: RegistryDep,
    settings: SettingsDep,
    limit: int | None = Query(default=None, ge=1, le=500),
) -> list[Call]:
    """Accepted, rejected and dropped calls, newest first.

    Read straight out of the registry, so this is the same ``Call`` shape the
    queue serves -- one wire type for a call wherever it appears. Held in
    memory only: the list starts empty after a restart.
    """
    return registry.recent_calls(limit=limit or settings.call_history_size)


@router.get("/calls/{call_id}", summary="One call with its transcript")
async def get_call(call_id: str, registry: RegistryDep) -> Call:
    call = registry.get(call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown call")
    return call


@router.post("/calls/{call_id}/accept", summary="Connect the call to a human")
async def accept_call(call_id: str, registry: RegistryDep, settings: SettingsDep) -> Call:
    """Bridge the call to a human and mark it accepted.

    Twilio acts first: if the bridge fails the call keeps its current status
    and stays on the dashboard, rather than being marked accepted while the
    caller is still sitting on hold.
    """
    call = _require(registry, call_id)
    destination = settings.host_phone_number

    try:
        bridged = await put_on_air(call.call_id, destination, settings)
    except TelephonyUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if not bridged:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "could not connect the call at Twilio")

    return registry.set_status(call.call_id, CallStatus.ACCEPTED)


@router.post("/calls/{call_id}/reject", summary="Decline the call")
async def reject_call(call_id: str, registry: RegistryDep, settings: SettingsDep) -> Call:
    """Tell the caller they are not getting on air, then mark it rejected.

    Same ordering rule as :func:`accept_call`, and it matters more here. A
    rejected call leaves the live queue, so if the hang-up silently failed the
    caller would be stranded on hold *and* invisible -- see
    app/services/decisions.py.
    """
    call = _require(registry, call_id)

    try:
        declined = await turn_away(call.call_id, settings)
    except TelephonyUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    if not declined:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "could not decline the call at Twilio")

    return registry.set_status(call.call_id, CallStatus.REJECTED)


class LineStateResponse(CamelModel):
    """Whether the line is taking calls, and what closing it did to the queue.

    ``failed`` is reported separately because those callers are still
    connected and still hearing hold music. Silence would let an operator walk
    away believing the line was clear.
    """

    open: bool
    #: Callers hung up as part of closing. Empty when opening.
    ended_call_ids: list[str] = Field(default_factory=list)
    #: Callers we could not reach. They keep their place on the dashboard.
    failed_call_ids: list[str] = Field(default_factory=list)
    #: True when hanging up hit its deadline, so `failed` understates it.
    timed_out: bool = False


@router.post("/line/open", summary="Start accepting calls")
async def open_line(line: LineDep) -> LineStateResponse:
    line.set_open(True)
    return LineStateResponse(open=True)


@router.post("/line/close", summary="Stop accepting calls and clear the queue")
async def close_line(
    line: LineDep, registry: RegistryDep, settings: SettingsDep
) -> LineStateResponse:
    """End the show, in one action.

    Closes the line to new callers *and* hangs up on anyone still holding,
    always -- there is no version of this where some callers are left on a
    line nobody is watching. The order matters: close first, so a caller
    dialling during the hang-ups is turned away rather than joining a queue
    that is being emptied.

    Deliberately not a shutdown hook: a deploy and a wrap-up arrive as the
    same signal, and draining on every restart would hang up on live callers
    each time someone ships. See app/services/drain.py.
    """
    line.set_open(False)
    result = await drain_queue(registry, settings)
    return LineStateResponse(
        open=False,
        ended_call_ids=result.ended,
        failed_call_ids=result.failed,
        timed_out=result.timed_out,
    )


def _require(registry: CallRegistry, call_id: str) -> Call:
    call = registry.get(call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown call")
    return call
