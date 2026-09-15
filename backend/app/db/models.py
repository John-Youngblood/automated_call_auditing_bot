"""Tables.

Two, both small and both moderator-facing:

* ``blocked_numbers`` -- the blocklist, keyed by normalised E.164 number.
* ``call_history`` -- one row per finished call, keyed by the provider's call
  id so repeated writes for the same call upsert rather than duplicate.

Deliberately no ORM relationship between them: a blocked number and a call
from it are related only by the number string, and a moderator can block a
number that has never called. Joining them would make both tables harder to
prune independently.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, TypeDecorator
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A datetime column that is always timezone-aware UTC in Python.

    SQLite has no timezone-aware datetime type, so SQLAlchemy's
    ``DateTime(timezone=True)`` is a no-op there: an aware value goes in and a
    *naive* one comes back. Pydantic then serialises it with no offset, and the
    browser reads "2026-09-15T02:03:29" as local time -- so every timestamp in
    the moderation log silently shifts by the UTC offset. A call log that
    misreports when calls happened is worse than no log.

    This normalises both directions: store naive UTC, return aware UTC. Naive
    input is assumed to already be UTC, which holds because every datetime in
    this application comes from ``datetime.now(UTC)``.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class BlockedNumber(Base):
    __tablename__ = "blocked_numbers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    #: Normalised E.164 (see app.services.phone). Unique so a double-click in
    #: the moderation UI cannot create two rows for the same caller.
    number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)

    #: What the moderator actually typed, kept for display and for debugging
    #: normalisation complaints ("I blocked them and they still got through").
    original_input: Mapped[str | None] = mapped_column(String(64), nullable=True)

    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)

    #: Who blocked it. No auth on this service yet, so it is unset in practice
    #: -- wire it up when the dashboard grows logins.
    blocked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<BlockedNumber {self.number}>"


class CallHistory(Base):
    __tablename__ = "call_history"

    #: The provider's call id (Twilio CallSid). Natural primary key, which is
    #: what makes the write on each terminal transition an idempotent upsert.
    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    #: Normalised caller number, or NULL when caller ID was withheld.
    from_number: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    from_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    from_location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    to_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Terminal CallStatus value: accepted / rejected / ended / blocked.
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Full committed transcript, newline-separated. Text rather than a child
    #: table: it is only ever read back whole, and a call produces tens of
    #: lines, not thousands.
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_line_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utcnow, nullable=False)

    __table_args__ = (
        # The history view is always "most recent first", so the index that
        # matters is on the sort column, not the primary key.
        Index("ix_call_history_started_at", "started_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CallHistory {self.call_id} {self.status}>"
