"""Call history and contacts, kept in SQLite.

One connection, used only from the event-loop thread, and every method is
synchronous. Measured on this schema, a primary-key read takes about 4µs and
a caller's history about 20µs, so a cache, a thread hop or a lock would buy
nothing. ``check_same_thread`` stays on to catch anyone who adds a thread.

Never the reason a call is dropped. A database that cannot be opened is
swapped for one in memory, and a write that fails is logged and reported
through :attr:`Store.warning` -- the show goes on either way. See
docs/architecture.md.

Timestamps are INTEGER milliseconds since the epoch, UTC, converted only by
:func:`_to_ms` and :func:`_from_ms`. Never sqlite3's own datetime adapters:
they are deprecated, and naive datetimes from a database once shifted every
time in the dashboard by the UTC offset.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.schemas.calls import (
    TERMINAL_STATUSES,
    Call,
    Caller,
    CallerHistory,
    CallStatus,
    Contact,
    ContactListItem,
    utc_now,
)
from app.services.migrations import MigrationError, migrate

logger = logging.getLogger(__name__)

#: What every dashboard shows while the store runs in memory. Says what an
#: operator would notice -- a blocked number ringing through -- not what broke.
DEGRADED_WARNING = (
    "Call history and contacts aren't being saved, and blocked numbers aren't being "
    "turned away. Restart the backend; if this stays, see docs/office.md."
)

_WRITE_FAILED_WARNING = (
    "Call history and contacts stopped saving ({reason}). Restart the backend; "
    "if this stays, see docs/office.md."
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

#: Generated from the enum, never written out, so history cannot drift from
#: the queue's idea of finished. Sorted only to keep the SQL stable.
_TERMINAL = tuple(sorted(status.value for status in TERMINAL_STATUSES))

#: Statuses of a call that is over. Not ACCEPTED: on air is still in play.
_CLOSED = (CallStatus.REJECTED.value, CallStatus.ENDED.value)

_CALL_COLUMNS = """
    calls.call_sid, calls.number, calls.location, calls.verified, calls.status,
    calls.started_at, calls.ended_at, calls.transcript, calls.transcript_confidence,
    calls.was_accepted, calls.on_air_seconds, calls.recovered"""

#: Prefixed, so they can sit beside a call's own columns in a join.
_CONTACT_COLUMNS = """
    contacts.number AS contact_number, contacts.name AS contact_name,
    contacts.note AS contact_note, contacts.standing AS contact_standing,
    contacts.standing_since AS contact_standing_since,
    contacts.blocked_attempts AS contact_blocked_attempts,
    contacts.updated_at AS contact_updated_at"""

_CALLS_WITH_CONTACTS = f"""
    SELECT {_CALL_COLUMNS}, {_CONTACT_COLUMNS}
    FROM calls LEFT JOIN contacts ON contacts.number = calls.number"""

_SAVE_CALL = """
    INSERT INTO calls (
        call_sid, number, location, verified, status, started_at, ended_at,
        transcript, transcript_confidence, was_accepted, on_air_seconds, recovered
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (call_sid) DO UPDATE SET
        number = excluded.number,
        location = excluded.location,
        verified = excluded.verified,
        status = excluded.status,
        started_at = excluded.started_at,
        ended_at = excluded.ended_at,
        transcript = excluded.transcript,
        transcript_confidence = excluded.transcript_confidence,
        was_accepted = excluded.was_accepted,
        on_air_seconds = excluded.on_air_seconds,
        recovered = excluded.recovered"""

#: Excludes one call: the ring asking, whose own retried webhook must not
#: count as a previous call.
_CALLER_HISTORY = """
    SELECT
        count(*) AS previous_calls,
        count(*) FILTER (WHERE was_accepted AND on_air_seconds IS NOT NULL) AS times_on_air,
        max(started_at) FILTER (WHERE was_accepted AND on_air_seconds IS NOT NULL)
            AS last_on_air_at,
        count(*) FILTER (WHERE status = 'rejected') AS times_rejected,
        max(started_at) AS last_called_at
    FROM calls
    WHERE number = ? AND call_sid != ?"""

_PUT_CONTACT = """
    INSERT INTO contacts (
        number, name, note, standing, standing_since, blocked_attempts, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (number) DO UPDATE SET
        name = excluded.name,
        note = excluded.note,
        standing = excluded.standing,
        standing_since = excluded.standing_since,
        blocked_attempts = excluded.blocked_attempts,
        updated_at = excluded.updated_at"""

#: Favorites first, then blocked, then everyone else; by name within each.
_LIST_CONTACTS = f"""
    SELECT {_CONTACT_COLUMNS},
        count(calls.call_sid) AS calls, max(calls.started_at) AS last_called_at
    FROM contacts LEFT JOIN calls ON calls.number = contacts.number
    GROUP BY contacts.number
    ORDER BY
        CASE contacts.standing WHEN 'favorite' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END,
        lower(contacts.name) NULLS LAST,
        contacts.number"""


class Store:
    def __init__(self, conn: sqlite3.Connection, path: str, warning: str | None = None) -> None:
        self._conn = conn
        #: ":memory:" when the file could not be used.
        self.path = path
        #: Why calls are not being saved, for every dashboard to show. Kept
        #: until restart: a store that failed one write may fail the next,
        #: and a warning that came and went would be easy to miss.
        self.warning = warning

    @classmethod
    def open(cls, path: Path) -> Store:
        """Open the database at ``path``, creating and migrating it as needed.

        Never raises. Anything that stops the file being used -- a volume the
        backend cannot write, a database left by newer code -- falls back to
        one in memory with :attr:`warning` set. The show crew updates
        unattended, so a backend that refused to start would take the phone
        line down with it; one that forgets is still screening calls.
        """
        conn: sqlite3.Connection | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path, autocommit=True)
            _prepare(conn, wal=True)
            return cls(conn, str(path))
        except (OSError, sqlite3.Error, MigrationError) as exc:
            logger.error(
                "cannot use the database at %s, keeping calls in memory until restart: %s",
                path,
                exc,
            )
            if conn is not None:
                conn.close()

        conn = sqlite3.connect(":memory:", autocommit=True)
        _prepare(conn, wal=False)
        return cls(conn, ":memory:", warning=DEGRADED_WARNING)

    def close(self) -> None:
        self._conn.close()

    # -- callers ------------------------------------------------------------
    def caller_profile(
        self, number: str, *, exclude_sid: str
    ) -> tuple[Contact | None, CallerHistory]:
        """``number``'s contact, and what their other calls came to."""
        rows = self._query(
            "look up a contact",
            f"SELECT {_CONTACT_COLUMNS} FROM contacts WHERE number = ?",
            (number,),
        )
        contact = Contact(**_contact_fields(rows[0])) if rows else None

        rows = self._query("look up a caller's history", _CALLER_HISTORY, (number, exclude_sid))
        if not rows:
            return contact, CallerHistory()
        row = rows[0]
        return contact, CallerHistory(
            previous_calls=row["previous_calls"],
            times_on_air=row["times_on_air"],
            last_on_air_at=_from_ms(row["last_on_air_at"]),
            times_rejected=row["times_rejected"],
            last_called_at=_from_ms(row["last_called_at"]),
        )

    def note_blocked_attempt(self, number: str) -> bool:
        cursor = self._write(
            "count a blocked attempt",
            "UPDATE contacts SET blocked_attempts = blocked_attempts + 1 "
            "WHERE number = ? AND standing = 'blocked'",
            (number,),
        )
        return cursor is not None

    # -- calls --------------------------------------------------------------
    def save_call(self, call: Call) -> bool:
        """Write ``call``'s row in full. Safe to repeat."""
        cursor = self._write(
            f"save call {call.call_id}",
            _SAVE_CALL,
            (
                call.call_id,
                call.caller.number,
                call.caller.location,
                call.caller.verified,
                call.status.value,
                _to_ms(call.started_at),
                _to_ms(call.ended_at) if call.ended_at else None,
                call.transcript,
                call.transcript_confidence,
                call.was_accepted,
                call.on_air_seconds,
                call.recovered,
            ),
        )
        return cursor is not None

    def get_call(self, call_sid: str) -> Call | None:
        rows = self._query(
            f"read call {call_sid}", f"{_CALLS_WITH_CONTACTS} WHERE calls.call_sid = ?", (call_sid,)
        )
        return _call(rows[0]) if rows else None

    def open_call_sids(self) -> list[str]:
        """Calls saved as still in play, on air included."""
        rows = self._query(
            "list open calls",
            f"SELECT call_sid FROM calls WHERE status NOT IN ({_marks(_CLOSED)})",
            _CLOSED,
        )
        return [row["call_sid"] for row in rows]

    def mark_ended_during_restart(self, call_sids: Sequence[str]) -> int:
        """End calls nobody saw end. ``ended_at`` is left alone, so it stays
        NULL on any that were never decided -- which is how history tells
        these apart: we know they are over, not when."""
        if not call_sids:
            return 0
        cursor = self._write(
            "close calls left open by a restart",
            f"UPDATE calls SET status = ? WHERE status NOT IN ({_marks(_CLOSED)}) "
            f"AND call_sid IN ({_marks(call_sids)})",
            (CallStatus.ENDED.value, *_CLOSED, *call_sids),
        )
        return cursor.rowcount if cursor else 0

    def close_call(self, call_sid: str, *, on_air_seconds: int | None) -> bool:
        """End a call that is not in memory -- one begun before a restart.

        The same rules as the registry's: REJECTED stays REJECTED, and an
        ``ended_at`` already set (an accepted call's is its decision time)
        stands. A call the sweep already closed is closed again, because now
        we know more: when it ended, and -- for one on air through the
        restart -- for how long, which only the dial result ever says.
        """
        cursor = self._write(
            f"close call {call_sid}",
            "UPDATE calls SET status = ?, ended_at = coalesce(ended_at, ?), "
            "on_air_seconds = coalesce(?, on_air_seconds) "
            "WHERE call_sid = ? AND status != ?",
            (
                CallStatus.ENDED.value,
                _to_ms(utc_now()),
                on_air_seconds,
                call_sid,
                CallStatus.REJECTED.value,
            ),
        )
        return cursor is not None

    def history(
        self,
        *,
        limit: int,
        before: str | None = None,
        number: str | None = None,
        q: str | None = None,
    ) -> tuple[list[Call], str | None]:
        """Finished calls, newest first, a page at a time.

        ``before`` is the ``next`` of the previous page: the last row's start
        time and SID. Keyset rather than offset, so a call finishing while an
        operator pages back neither repeats a row nor skips one.

        ``q`` matches what callers said and contacts' names and notes, and a
        number when it has four or more digits.
        """
        where = [f"calls.status IN ({_marks(_TERMINAL)})"]
        params: list[object] = [*_TERMINAL]
        if before is not None:
            started_at, _, call_sid = before.partition(":")
            where.append("(calls.started_at, calls.call_sid) < (?, ?)")
            params += [int(started_at), call_sid]
        if number is not None:
            where.append("calls.number = ?")
            params.append(number)
        if q := (q or "").strip():
            pattern = "%" + re.sub(r"([%_\\])", r"\\\1", q) + "%"
            matches = [
                "calls.transcript LIKE ? ESCAPE '\\'",
                "contacts.name LIKE ? ESCAPE '\\'",
                "contacts.note LIKE ? ESCAPE '\\'",
            ]
            params += [pattern] * len(matches)
            # Digits alone, so "(503) 555-01" finds +150355501.. however it
            # was punctuated.
            digits = re.sub(r"[^0-9]", "", q)
            if len(digits) >= 4:
                matches.append("calls.number LIKE ?")
                params.append(f"%{digits}%")
            where.append(f"({' OR '.join(matches)})")

        rows = self._query(
            "read call history",
            f"{_CALLS_WITH_CONTACTS} WHERE {' AND '.join(where)} "
            "ORDER BY calls.started_at DESC, calls.call_sid DESC LIMIT ?",
            (*params, limit),
        )
        # A short page is the last one. A full page may be too; the next
        # request finds out by coming back empty.
        cursor = f"{rows[-1]['started_at']}:{rows[-1]['call_sid']}" if len(rows) == limit else None
        return [_call(row) for row in rows], cursor

    def purge_transcripts(self, retention_days: int) -> int:
        """Forget what callers said ``retention_days`` ago; keep the calls.

        No call is open that long -- Twilio ends any call at four hours -- so
        the live queue is never touched. 0 keeps everything.
        """
        if retention_days == 0:
            return 0
        cursor = self._write(
            "clear old transcripts",
            "UPDATE calls SET transcript = NULL, transcript_confidence = NULL "
            "WHERE started_at < ? AND transcript IS NOT NULL",
            (_to_ms(utc_now() - timedelta(days=retention_days)),),
        )
        cleared = cursor.rowcount if cursor else 0
        if cleared:
            logger.info("cleared %s transcript(s) older than %s days", cleared, retention_days)
        return cleared

    # -- contacts -----------------------------------------------------------
    def contacts(self) -> list[ContactListItem]:
        return [
            ContactListItem(
                **_contact_fields(row),
                calls=row["calls"],
                last_called_at=_from_ms(row["last_called_at"]),
            )
            for row in self._query("list contacts", _LIST_CONTACTS)
        ]

    def get_contact(self, number: str) -> Contact | None:
        rows = self._query(
            "read a contact", f"SELECT {_CONTACT_COLUMNS} FROM contacts WHERE number = ?", (number,)
        )
        return Contact(**_contact_fields(rows[0])) if rows else None

    def put_contact(self, contact: Contact) -> bool:
        cursor = self._write(
            "save a contact",
            _PUT_CONTACT,
            (
                contact.number,
                contact.name,
                contact.note,
                contact.standing,
                _to_ms(contact.standing_since) if contact.standing_since else None,
                contact.blocked_attempts,
                _to_ms(contact.updated_at),
            ),
        )
        return cursor is not None

    def delete_contact(self, number: str) -> bool:
        """Remove a contact. Their calls stay: history is not the contact's."""
        cursor = self._write("remove a contact", "DELETE FROM contacts WHERE number = ?", (number,))
        return cursor is not None

    def counts(self) -> tuple[int, int]:
        """(contacts, calls), for the startup log line."""
        rows = self._query(
            "count rows", "SELECT (SELECT count(*) FROM contacts), (SELECT count(*) FROM calls)"
        )
        return (rows[0][0], rows[0][1]) if rows else (0, 0)

    # -- internals ----------------------------------------------------------
    def _query(self, action: str, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
        """Rows, or none if the read failed -- logged, never raised."""
        try:
            return self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            logger.error("could not %s: %s", action, exc)
            return []

    def _write(self, action: str, sql: str, params: Sequence[object]) -> sqlite3.Cursor | None:
        """The cursor, or None if the write failed.

        A failure is logged, sets :attr:`warning` if it is the first, and is
        never raised: whoever is writing is in the middle of a phone call.
        """
        try:
            return self._conn.execute(sql, params)
        except sqlite3.Error as exc:
            logger.error("could not %s: %s", action, exc)
            if self.warning is None:
                # First line only: a failed CHECK goes on to quote the SQL.
                reason = str(exc).partition("\n")[0]
                self.warning = _WRITE_FAILED_WARNING.format(reason=reason)
            return None


def _prepare(conn: sqlite3.Connection, *, wal: bool) -> None:
    conn.row_factory = sqlite3.Row
    if wal:
        # A backup can read while calls are being saved.
        conn.execute("PRAGMA journal_mode = WAL")
    # A call that was saved survives a power cut. One fsync per write is
    # nothing at the pace calls arrive.
    conn.execute("PRAGMA synchronous = FULL")
    # A backup holding the lock makes a write wait a moment, not fail.
    conn.execute("PRAGMA busy_timeout = 250")
    # Purged transcripts and removed contacts are overwritten, not just
    # unlinked from a page that still holds them.
    conn.execute("PRAGMA secure_delete = ON")
    migrate(conn)


def _marks(values: Sequence[object]) -> str:
    """One ``?`` per value, for an IN list."""
    return ", ".join("?" * len(values))


def _to_ms(dt: datetime) -> int:
    """Epoch milliseconds. A naive datetime is refused, not guessed at."""
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime {dt!r}: only aware datetimes are stored")
    return (dt - _EPOCH) // timedelta(milliseconds=1)


def _from_ms(ms: int | None) -> datetime | None:
    """Aware UTC. None stays None: most of these columns are nullable."""
    return None if ms is None else datetime.fromtimestamp(ms / 1000, UTC)


def _contact_fields(row: sqlite3.Row) -> dict[str, object]:
    return {
        "number": row["contact_number"],
        "name": row["contact_name"],
        "note": row["contact_note"],
        "standing": row["contact_standing"],
        "standing_since": _from_ms(row["contact_standing_since"]),
        "blocked_attempts": row["contact_blocked_attempts"],
        "updated_at": _from_ms(row["contact_updated_at"]),
    }


def _call(row: sqlite3.Row) -> Call:
    return Call(
        call_id=row["call_sid"],
        status=CallStatus(row["status"]),
        caller=Caller(
            number=row["number"],
            location=row["location"],
            verified=bool(row["verified"]),
            # Joined, so a call shows the contact as it is now.
            contact=Contact(**_contact_fields(row)) if row["contact_number"] else None,
        ),
        started_at=_from_ms(row["started_at"]),
        ended_at=_from_ms(row["ended_at"]),
        transcript=row["transcript"],
        transcript_confidence=row["transcript_confidence"],
        was_accepted=bool(row["was_accepted"]),
        on_air_seconds=row["on_air_seconds"],
        recovered=bool(row["recovered"]),
    )
