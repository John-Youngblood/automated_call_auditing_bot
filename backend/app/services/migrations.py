"""The database schema, as an append-only list of migrations.

``PRAGMA user_version`` records how many have run; :func:`migrate` runs the
rest, each in its own transaction, at startup.

The rules, each learned the hard way or cheap to keep:

- Never edit a shipped migration -- add a new one. A database that already
  ran it will never run the edit, so the code and the file drift apart in a
  way only production sees. tests/test_migrations.py freezes each one's hash.
- ``ADD COLUMN`` must be nullable or have a DEFAULT. Existing rows need a
  value, and a NOT NULL column without one fails every insert that omits it.
- Only syntax SQLite 3.46 accepts: that is what the containers ship (the
  Debian base decides it; see backend/Dockerfile). A Mac has something newer
  and accepts more, so a test passing locally proves nothing about this.
- No ``auto_vacuum``: it has to be set before WAL mode is, and that happens
  the moment a database is first opened. Adding it later means a rebuild,
  which is not worth it.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

MIGRATION_1 = """
CREATE TABLE contacts (
  number TEXT PRIMARY KEY CHECK (
    number GLOB '+[1-9]*' AND number NOT GLOB '+*[^0-9]*' AND length(number) BETWEEN 9 AND 16
  ),
  name TEXT CHECK (name IS NULL OR length(name) BETWEEN 1 AND 80),
  note TEXT CHECK (note IS NULL OR length(note) BETWEEN 1 AND 500),
  standing TEXT CHECK (standing IN ('favorite', 'blocked')),
  standing_since INTEGER,
  blocked_attempts INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  CHECK (name IS NOT NULL OR note IS NOT NULL OR standing IS NOT NULL)
) STRICT, WITHOUT ROWID;

CREATE TABLE calls (
  call_sid TEXT PRIMARY KEY,
  number TEXT CHECK (
    number IS NULL
    OR (number GLOB '+[1-9]*' AND number NOT GLOB '+*[^0-9]*' AND length(number) BETWEEN 9 AND 16)
  ),
  location TEXT,
  verified INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
  status TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  transcript TEXT,
  transcript_confidence REAL,
  was_accepted INTEGER NOT NULL DEFAULT 0 CHECK (was_accepted IN (0, 1)),
  on_air_seconds INTEGER,
  recovered INTEGER NOT NULL DEFAULT 0 CHECK (recovered IN (0, 1))
) STRICT;

CREATE INDEX calls_by_number ON calls (number, started_at);
CREATE INDEX calls_by_started ON calls (started_at, call_sid);
"""

#: Append only. Position is the version: MIGRATIONS[0] takes a database to 1.
MIGRATIONS: tuple[str, ...] = (MIGRATION_1,)


class MigrationError(RuntimeError):
    """The database cannot be brought to this code's schema."""


def migrate(conn: sqlite3.Connection) -> None:
    """Run whatever migrations ``conn`` has not had yet.

    Expects an autocommit connection, so each migration's BEGIN/COMMIT is its
    own. A failure rolls that one back and leaves the version where it was.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > len(MIGRATIONS):
        # Run by an older checkout than the one that last opened it. Guessing
        # at a schema we have never seen would corrupt it, so refuse.
        raise MigrationError(
            f"database is newer than this code (version {current}, "
            f"this code knows {len(MIGRATIONS)})"
        )

    for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error as exc:
            # rollback() is a no-op under autocommit=True; the BEGIN is ours.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"migration {version} failed: {exc}") from exc
        logger.info("database migrated to version %s", version)
