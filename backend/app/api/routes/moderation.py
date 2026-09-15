"""Moderation: the blocklist and the call-history view.

    POST /api/block-number
    GET  /api/blocked-numbers
    GET  /api/call-history

Blocking is the one action here with teeth, and it does two things that must
not be confused with each other:

1. adds the number to ``blocked_numbers``, so *future* calls are refused at
   the webhook before any stream opens; and
2. terminates whatever that caller has in flight *right now*, via the
   provider's REST API.

Step 1 is durable and cheap. Step 2 is network I/O against a third party and
can fail. The response reports them separately so the dashboard can say "the
number is blocked but we could not drop the live call" instead of implying
silence means success.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import (
    BlocklistDep,
    ContactsDep,
    HistoryDep,
    RegistryDep,
    SettingsDep,
    TelephonyDep,
)
from app.schemas.calls import CallStatus
from app.schemas.moderation import (
    BlockedNumberOut,
    BlockNumberRequest,
    BlockNumberResponse,
    CallHistoryEntry,
)
from app.services.phone import InvalidPhoneNumber

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["moderation"])


@router.post(
    "/block-number",
    summary="Block a caller and drop any call they have in flight",
    responses={400: {"description": "Not a usable phone number"}},
)
async def block_number(
    payload: BlockNumberRequest,
    blocklist: BlocklistDep,
    registry: RegistryDep,
    telephony: TelephonyDep,
) -> BlockNumberResponse:
    try:
        record, newly_blocked = await blocklist.block(
            payload.number,
            reason=payload.reason,
            blocked_by=payload.blocked_by,
        )
    except InvalidPhoneNumber as exc:
        # A 400 rather than a silent no-op: a typo that creates an entry which
        # can never match is worse than an error the moderator can see.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # Re-blocking an existing number is still worth doing: the moderator is
    # almost certainly trying to kick a live caller off right now.
    terminated, failed = await _terminate_active_calls(
        record.number, registry=registry, telephony=telephony, reason=payload.reason or "blocked"
    )

    return BlockNumberResponse(
        blocked=record,
        newly_blocked=newly_blocked,
        terminated_call_ids=terminated,
        failed_call_ids=failed,
    )


async def _terminate_active_calls(
    number: str,
    *,
    registry: RegistryDep,
    telephony: TelephonyDep,
    reason: str,
) -> tuple[list[str], list[str]]:
    """Hang up every open call from ``number``.

    Order matters: mark the call BLOCKED locally *after* the provider command,
    so a failure leaves the call visible on the dashboard for a human to deal
    with rather than disappearing it from the queue while the caller is still
    connected.
    """
    terminated: list[str] = []
    failed: list[str] = []

    for call in registry.find_by_number(number):
        ok = await telephony.hangup(call.call_id, reason=reason)
        if ok:
            await registry.set_status(call.call_id, CallStatus.BLOCKED)
            terminated.append(call.call_id)
        else:
            logger.error("blocked %s but could not hang up live call_id=%s", number, call.call_id)
            failed.append(call.call_id)

    return terminated, failed


@router.get("/blocked-numbers", summary="Current blocklist")
async def list_blocked_numbers(blocklist: BlocklistDep) -> list[BlockedNumberOut]:
    return await blocklist.list_blocked()


@router.get("/call-history", summary="Recent finished calls")
async def call_history(
    history: HistoryDep,
    blocklist: BlocklistDep,
    contacts: ContactsDep,
    settings: SettingsDep,
    limit: int | None = Query(default=None, ge=1, le=500),
) -> list[CallHistoryEntry]:
    """Completed, rejected, dropped and blocked calls, newest first.

    The blocklist snapshot is passed down so each row can say whether that
    caller is already blocked -- one in-memory set against the page of rows,
    rather than a query per row.
    """
    return await history.recent(
        limit=limit or settings.call_history_page_size,
        blocked=blocklist.snapshot(),
        contacts=contacts.snapshot(),
    )
