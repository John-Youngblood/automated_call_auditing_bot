"""Durable record of finished calls.

Written once per call, when it reaches a terminal status, keyed by the
provider's call id so repeated terminal transitions (rejected, then the
provider's status callback arriving) upsert instead of duplicating.

"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.models import CallHistory
from app.db.session import Database
from app.schemas.calls import Call
from app.schemas.contacts import ContactOut
from app.schemas.moderation import CallHistoryEntry
from app.services.phone import try_normalize

logger = logging.getLogger(__name__)

#: Characters of transcript shown in the history list.
SUMMARY_LENGTH = 280


class CallHistoryRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def record(self, call: Call) -> None:
        """Upsert one finished call.

        Never raises: history is valuable but it is not worth failing a call
        teardown over. A failed write is logged and the call still ends
        cleanly for the caller and the dashboard.
        """
        try:
            await self._record(call)
        except Exception:
            logger.exception("could not record call history for call_id=%s", call.call_id)

    async def _record(self, call: Call) -> None:
        duration = None
        if call.ended_at is not None:
            duration = max(0, int((call.ended_at - call.started_at).total_seconds()))

        async with self._db.session() as session:
            row = await session.get(CallHistory, call.call_id)
            if row is None:
                row = CallHistory(call_id=call.call_id, started_at=call.started_at)
                session.add(row)

            # Normalised so a history row can be matched against the blocklist
            # and used to populate a Block action directly.
            row.from_number = try_normalize(call.caller.number)
            row.from_name = call.caller.name
            row.from_location = call.caller.location
            row.to_number = call.to_number
            row.status = str(call.status)
            row.started_at = call.started_at
            row.ended_at = call.ended_at
            row.duration_seconds = duration
            row.transcript = call.transcript or None

    async def recent(
        self,
        limit: int = 100,
        blocked: frozenset[str] | None = None,
        contacts: dict[str, ContactOut] | None = None,
    ) -> list[CallHistoryEntry]:
        """Most recent calls first.

        ``blocked`` and ``contacts`` are passed in rather than looked up here,
        so the history view can mark blocked and starred callers without this
        module depending on either service -- one query, no N+1, no coupling.

        Names resolve against the *current* contact rather than the one stored
        on the row, so naming a caller retroactively labels every past call
        from them.
        """
        async with self._db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(CallHistory).order_by(CallHistory.started_at.desc()).limit(limit)
                    )
                )
                .scalars()
                .all()
            )

        blocked = blocked or frozenset()
        contacts = contacts or {}
        return [_to_schema(row, blocked, contacts) for row in rows]

    async def get(self, call_id: str) -> CallHistoryEntry | None:
        async with self._db.session() as session:
            row = await session.get(CallHistory, call_id)
        return _to_schema(row, frozenset(), {}) if row is not None else None


def summarize(transcript: str | None) -> str:
    """First ``SUMMARY_LENGTH`` characters, cut on a word boundary."""
    if not transcript:
        return ""
    flattened = " ".join(transcript.split())
    if len(flattened) <= SUMMARY_LENGTH:
        return flattened
    clipped = flattened[:SUMMARY_LENGTH]
    # Avoid ending mid-word, unless the first word is itself longer than the cap.
    if " " in clipped:
        clipped = clipped[: clipped.rindex(" ")]
    return clipped + "…"


def _to_schema(
    row: CallHistory, blocked: frozenset[str], contacts: dict[str, ContactOut]
) -> CallHistoryEntry:
    contact = contacts.get(row.from_number) if row.from_number else None
    return CallHistoryEntry(
        call_id=row.call_id,
        from_number=row.from_number,
        from_name=(contact.display_name if contact and contact.display_name else row.from_name),
        from_location=row.from_location,
        to_number=row.to_number,
        status=row.status,
        started_at=row.started_at,
        ended_at=row.ended_at,
        duration_seconds=row.duration_seconds,
        transcript_summary=summarize(row.transcript),
        is_blocked=row.from_number is not None and row.from_number in blocked,
        is_favorite=contact is not None and contact.is_favorite,
    )
