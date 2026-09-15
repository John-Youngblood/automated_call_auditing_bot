"""Named callers and favourites.

The team's own labels on phone numbers: a display name, a star, or both.

Read from an in-memory dict for the same reason the blocklist is -- every
inbound call consults it while the caller waits on the line, and it is read
again for every row of the history view. Loaded once at startup and updated on
write, so it cannot drift within a process. See docs/architecture.md for the
single-worker constraint that implies.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.models import Contact
from app.db.session import Database
from app.schemas.contacts import ContactOut
from app.services.phone import normalize_number, try_normalize

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


class ContactsService:
    def __init__(self, database: Database) -> None:
        self._db = database
        self._by_number: dict[str, ContactOut] = {}
        self._loaded = False

    async def load(self) -> None:
        """Populate the cache from the database. Called once, at startup."""
        async with self._db.session() as session:
            rows = (await session.execute(select(Contact))).scalars().all()
        self._by_number = {row.number: _to_schema(row) for row in rows}
        self._loaded = True
        favourites = sum(1 for c in self._by_number.values() if c.is_favorite)
        logger.info("contacts loaded: %s (%s favourite)", len(self._by_number), favourites)

    @property
    def size(self) -> int:
        return len(self._by_number)

    def get(self, raw_number: str | None) -> ContactOut | None:
        """Look up a contact. Synchronous and safe on the webhook path.

        Normalises first, so a contact saved as "(555) 019-2834" is found when
        the carrier sends "+15550192834".
        """
        if not self._loaded:  # pragma: no cover - defensive; lifespan loads it
            logger.warning("contacts consulted before load(); treating as empty")
            return None
        normalized = try_normalize(raw_number)
        return self._by_number.get(normalized) if normalized else None

    def resolve_name(self, raw_number: str | None, carrier_name: str | None) -> str | None:
        """The name to show for a caller.

        A saved contact name wins over the carrier's caller-ID name: CNAM data
        is frequently stale, generic ("WIRELESS CALLER"), or simply wrong, and
        a name someone on the team typed is worth more than any of that.
        """
        contact = self.get(raw_number)
        if contact and contact.display_name:
            return contact.display_name
        return carrier_name

    def is_favorite(self, raw_number: str | None) -> bool:
        contact = self.get(raw_number)
        return contact is not None and contact.is_favorite

    def snapshot(self) -> dict[str, ContactOut]:
        """All contacts by number, for bulk use like the history view."""
        return dict(self._by_number)

    async def upsert(
        self,
        raw_number: str,
        *,
        display_name: str | None = None,
        is_favorite: bool | None = None,
    ) -> ContactOut:
        """Create or update a contact.

        ``None`` means "leave this alone" rather than "clear it", so starring a
        caller from the queue cannot wipe a name someone else typed. Pass an
        empty string to actually clear the name.

        Raises :class:`~app.services.phone.InvalidPhoneNumber` for input that
        could never match a real caller.
        """
        number = normalize_number(raw_number)

        async with self._db.session() as session:
            row = (
                await session.execute(select(Contact).where(Contact.number == number))
            ).scalar_one_or_none()

            if row is None:
                row = Contact(number=number)
                session.add(row)

            if display_name is not None:
                cleaned = display_name.strip()
                row.display_name = cleaned or None
            if is_favorite is not None:
                row.is_favorite = is_favorite

            # A contact with no name and no star holds no information. Drop it
            # rather than accumulate empty rows every time someone unstars a
            # caller they never named -- and so "Remove name" is always safe to
            # offer, whether or not a saved name existed.
            emptied = not row.display_name and not row.is_favorite
            if emptied:
                await session.delete(row)
                contact = ContactOut(
                    number=number,
                    display_name=None,
                    is_favorite=False,
                    created_at=row.created_at or _now(),
                    updated_at=_now(),
                )
            else:
                await session.flush()
                contact = _to_schema(row)

        # Only after the commit, so a cache entry cannot outlive a failed write.
        if emptied:
            self._by_number.pop(number, None)
            logger.info("contact dropped (nothing left to remember) number=%s", number)
            return contact

        self._by_number[number] = contact
        logger.info(
            "contact saved number=%s name=%r favourite=%s",
            number,
            contact.display_name,
            contact.is_favorite,
        )
        return contact

    async def delete(self, raw_number: str) -> bool:
        """Forget a contact. Returns whether there was one."""
        number = normalize_number(raw_number)

        async with self._db.session() as session:
            row = (
                await session.execute(select(Contact).where(Contact.number == number))
            ).scalar_one_or_none()
            if row is None:
                return False
            await session.delete(row)

        self._by_number.pop(number, None)
        logger.info("contact removed number=%s", number)
        return True

    async def list_all(self) -> list[ContactOut]:
        """Favourites first, then by name, then by number."""
        async with self._db.session() as session:
            rows = (await session.execute(select(Contact))).scalars().all()
        contacts = [_to_schema(row) for row in rows]
        return sorted(
            contacts,
            key=lambda c: (not c.is_favorite, (c.display_name or "").lower(), c.number),
        )


def _to_schema(row: Contact) -> ContactOut:
    return ContactOut(
        number=row.number,
        display_name=row.display_name,
        is_favorite=row.is_favorite,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
