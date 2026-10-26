"""Screening decisions, call history, and closing the line.

    GET  /api/calls
    GET  /api/calls/{call_id}
    POST /api/calls/{call_id}/accept
    POST /api/calls/{call_id}/reject
    GET  /api/call-history
    POST /api/line/open
    POST /api/line/close

The decision endpoints duplicate the websocket commands on purpose: a decision
either succeeded or did not, and HTTP gives the dashboard a status code for it.
The resulting state change still reaches every dashboard over the socket.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import Field

from app.api.auth import RequireSession
from app.api.deps import LineDep, RegistryDep, SettingsDep, StoreDep
from app.schemas.calls import Call, CallStatus, CamelModel
from app.services.call_registry import CallRegistry
from app.services.decisions import TelephonyUnavailable, put_on_air, turn_away
from app.services.drain import hang_up_holders as drain_queue
from app.services.phone import InvalidPhoneNumber, parse_typed_number

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["calls"], dependencies=[RequireSession])


@router.get("/calls", summary="Calls currently in the screening queue")
async def list_calls(registry: RegistryDep) -> list[Call]:
    return registry.open_calls()


class CallHistoryPage(CamelModel):
    calls: list[Call]
    #: Pass back as ``before`` for the page after this one. None on the last.
    next: str | None = None


@router.get("/call-history", summary="Finished calls, newest first, a page at a time")
async def call_history(
    store: StoreDep,
    limit: int = Query(default=50, ge=1, le=200),
    # At most 15 digits: more would overflow SQLite's integer -- a 500, not
    # a 422 -- and the server never hands out a cursor like that.
    before: str | None = Query(default=None, pattern=r"^[0-9]{1,15}:.+$"),
    number: str | None = None,
    q: str | None = None,
) -> CallHistoryPage:
    """Saved calls, kept across restarts.

    Same ``Call`` shape the queue serves -- one wire type wherever a call
    appears. ``number`` takes what an operator would type; ``q`` searches
    what callers said and contacts' names and notes.
    """
    if number is not None:
        try:
            number = parse_typed_number(number)
        except InvalidPhoneNumber as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    calls, cursor = store.history(limit=limit, before=before, number=number, q=q)
    return CallHistoryPage(calls=calls, next=cursor)


@router.get("/calls/{call_id}", summary="One call with its transcript")
async def get_call(call_id: str, registry: RegistryDep, store: StoreDep) -> Call:
    # Memory first: a call in play is newer there than its saved row.
    call = registry.get(call_id) or store.get_call(call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown call")
    return call


@router.post("/calls/{call_id}/accept", summary="Connect the call to a human")
async def accept_call(call_id: str, registry: RegistryDep, settings: SettingsDep) -> Call:
    """Bridge the call to the host and mark it accepted.

    Twilio acts first: a failed bridge leaves the call on the dashboard rather
    than marked accepted while the caller is still on hold.
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

    Same ordering as :func:`accept_call`, and it matters more here: a rejected
    call leaves the queue, so a silent failure would strand them invisibly.
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

    ``failed`` matters: those callers are still connected, so silence must not
    let an operator walk away believing the line was clear.
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
    line: LineDep, registry: RegistryDep, settings: SettingsDep, store: StoreDep
) -> LineStateResponse:
    """End the show, in one action.

    Closes to new callers *and* hangs up on anyone still holding. Close first,
    so a caller dialling mid-drain is turned away rather than joining a queue
    being emptied. Not a shutdown hook -- see app/services/drain.py.

    Also when old transcripts are cleared: once a show, with nobody waiting.
    """
    line.set_open(False)
    result = await drain_queue(registry, settings)
    store.purge_transcripts(settings.transcript_retention_days)
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
