"""The blocklist: who is not allowed through.

Backed by ``blocked_numbers`` but read from an in-memory set. The inbound-call
webhook consults this on every ring while the provider holds the caller
waiting, so the read path does no I/O at all -- a lock-step database round trip
there would put a disk on the critical path of answering a phone.

The cache is loaded once at startup and updated on every write, so it cannot
drift within a process. It *can* drift between processes, which is the same
single-worker constraint the broadcaster and call registry already impose; see
docs/architecture.md. If you move to multiple workers, either re-read on a
short TTL or publish block events over the same Redis channel as everything
else.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.models import BlockedNumber
from app.db.session import Database
from app.schemas.moderation import BlockedNumberOut
from app.services.phone import normalize_number, try_normalize

logger = logging.getLogger(__name__)


class BlocklistService:
    def __init__(self, database: Database) -> None:
        self._db = database
        self._blocked: set[str] = set()
        self._loaded = False

    async def load(self) -> None:
        """Populate the cache from the database. Called once, at startup."""
        async with self._db.session() as session:
            rows = (await session.execute(select(BlockedNumber.number))).scalars().all()
        self._blocked = set(rows)
        self._loaded = True
        logger.info("blocklist loaded: %s number(s)", len(self._blocked))

    @property
    def size(self) -> int:
        return len(self._blocked)

    def is_blocked(self, raw_number: str | None) -> bool:
        """Synchronous, allocation-light, safe on the webhook hot path.

        Normalises before comparing, so a number blocked as "(555) 019-2834"
        still matches a provider sending "+15550192834". An unparseable or
        withheld caller ID is never blocked -- blocking "unknown" would block
        every anonymous caller at once.
        """
        if not self._loaded:  # pragma: no cover - defensive; lifespan loads it
            logger.warning("blocklist consulted before load(); treating as empty")
            return False
        normalized = try_normalize(raw_number)
        return normalized is not None and normalized in self._blocked

    def snapshot(self) -> frozenset[str]:
        """Current blocked numbers, for bulk checks like the history view."""
        return frozenset(self._blocked)

    async def block(
        self,
        raw_number: str,
        *,
        reason: str | None = None,
        blocked_by: str | None = None,
    ) -> tuple[BlockedNumberOut, bool]:
        """Add a number to the blocklist.

        Returns the stored record and whether it was newly added. Idempotent:
        re-blocking an existing number is a no-op on the table rather than an
        error, because the moderator's intent ("this caller must not get
        through") is already satisfied, and the caller of this method still
        wants to go on and terminate any live call.

        Raises :class:`~app.services.phone.InvalidPhoneNumber` for input that
        could never match a real caller.
        """
        normalized = normalize_number(raw_number)

        async with self._db.session() as session:
            existing = (
                await session.execute(
                    select(BlockedNumber).where(BlockedNumber.number == normalized)
                )
            ).scalar_one_or_none()

            if existing is not None:
                record = _to_schema(existing)
                # Keep the cache honest even if it somehow missed this row.
                self._blocked.add(normalized)
                return record, False

            row = BlockedNumber(
                number=normalized,
                original_input=raw_number.strip()[:64],
                reason=reason,
                blocked_by=blocked_by,
            )
            session.add(row)
            await session.flush()  # populate defaults before the session closes
            record = _to_schema(row)

        # Only after the commit succeeds -- a cache entry for a row that failed
        # to persist would vanish on the next restart and silently unblock.
        self._blocked.add(normalized)
        logger.info("blocked number=%s by=%s reason=%s", normalized, blocked_by, reason)
        return record, True

    async def list_blocked(self) -> list[BlockedNumberOut]:
        async with self._db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(BlockedNumber).order_by(BlockedNumber.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
        return [_to_schema(row) for row in rows]


def _to_schema(row: BlockedNumber) -> BlockedNumberOut:
    return BlockedNumberOut(
        number=row.number,
        original_input=row.original_input,
        reason=row.reason,
        blocked_by=row.blocked_by,
        created_at=row.created_at,
    )
