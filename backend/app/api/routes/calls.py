"""Screening decisions.

    GET  /api/calls
    POST /api/calls/{call_id}/accept
    POST /api/calls/{call_id}/reject

These exist alongside the equivalent websocket commands on purpose. A decision
is a one-shot action that either succeeded or did not, and HTTP gives the
dashboard a status code and a retry story for it; the websocket stays a
streaming channel. The resulting state change still reaches every dashboard
through the broadcaster, so all of them stay in sync either way.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.api.deps import RegistryDep, SettingsDep, TelephonyDep
from app.schemas.calls import Call, CallStatus
from app.services.call_registry import CallRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/calls", tags=["calls"])


@router.get("", summary="Calls currently in the screening queue")
async def list_calls(registry: RegistryDep) -> list[Call]:
    return registry.open_calls()


@router.get("/{call_id}", summary="One call with its transcript")
async def get_call(call_id: str, registry: RegistryDep) -> Call:
    call = registry.get(call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown call")
    return call


@router.post("/{call_id}/accept", summary="Connect the call to a human")
async def accept_call(
    call_id: str,
    registry: RegistryDep,
    telephony: TelephonyDep,
    settings: SettingsDep,
) -> Call:
    """Bridge the call to a human and mark it accepted.

    The provider command is a placeholder (see
    :mod:`app.telephony.provider_client`); the state change and its broadcast
    to every dashboard are real.

    The provider call runs first: if it fails, the call keeps its current
    status and stays on the dashboard rather than being marked accepted while
    the caller is still sitting in the screening stream.
    """
    call = _require(registry, call_id)

    destination = settings.agent_forward_number
    if not await telephony.bridge(call.call_id, destination):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "could not connect the call at the provider"
        )

    logger.info("accepted call_id=%s -> %s", call_id, destination)
    return await registry.set_status(call.call_id, CallStatus.ACCEPTED)


@router.post("/{call_id}/reject", summary="Decline the call")
async def reject_call(call_id: str, registry: RegistryDep, telephony: TelephonyDep) -> Call:
    """Decline the call and mark it rejected.

    Same placeholder/real split as :func:`accept_call`, and the same ordering
    rule: the provider acts first, the dashboard state follows.
    """
    call = _require(registry, call_id)

    if not await telephony.decline(call.call_id):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "could not decline the call at the provider"
        )

    logger.info("rejected call_id=%s", call_id)
    return await registry.set_status(call.call_id, CallStatus.REJECTED)


def _require(registry: CallRegistry, call_id: str) -> Call:
    call = registry.get(call_id)
    if call is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown call")
    return call
