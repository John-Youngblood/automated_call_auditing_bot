"""Wire contracts for moderation: the blocklist and call history."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas.calls import CamelModel


class BlockNumberRequest(CamelModel):
    """Body of ``POST /api/block-number``.

    ``number`` is whatever the moderator typed or the UI had on hand; the
    server normalises it. Accepting only pre-normalised input would push a
    correctness problem onto every caller of the API.
    """

    number: str = Field(min_length=3, max_length=64)
    reason: str | None = Field(default=None, max_length=256)
    #: Free-text for now. Becomes the authenticated user once the dashboard
    #: grows logins -- do not trust it for audit until then.
    blocked_by: str | None = Field(default=None, max_length=128)


class BlockedNumberOut(CamelModel):
    number: str
    original_input: str | None = None
    reason: str | None = None
    blocked_by: str | None = None
    created_at: datetime


class BlockNumberResponse(CamelModel):
    """What the moderator's click actually accomplished.

    Reports the hang-up outcome separately from the block itself: the number
    is on the list either way, but whether a live call was actually dropped is
    the part the moderator is watching for, and it can fail independently.
    """

    blocked: BlockedNumberOut
    #: False when the number was already on the list. A re-block is still a
    #: useful "kick them off now" action, so it is not an error.
    newly_blocked: bool
    #: Call ids terminated as a result of this block.
    terminated_call_ids: list[str] = Field(default_factory=list)
    #: Calls we tried and failed to hang up, so the UI can say so rather than
    #: letting silence imply success.
    failed_call_ids: list[str] = Field(default_factory=list)


class CallHistoryEntry(CamelModel):
    call_id: str
    from_number: str | None = None
    from_name: str | None = None
    from_location: str | None = None
    to_number: str | None = None
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    #: Truncated transcript for the list view.
    transcript_summary: str = ""
    #: Whether this caller is on the blocklist right now, so the history view
    #: can disable a Block button that would be a no-op.
    is_blocked: bool = False
    #: Starred right now. Resolved at read time rather than stored, so naming
    #: or starring a caller updates every past call from them at once.
    is_favorite: bool = False
