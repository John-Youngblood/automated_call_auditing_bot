"""The schema, as an append-only list of migrations.

These exist for the bug that took the last database out of this project: a
schema change that a fresh test database never saw, failing every insert on
the real one. So the test that matters starts from an *old*, populated
database, not a new one.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.schemas.calls import Call, Caller
from app.services import migrations
from app.services.migrations import MIGRATIONS, MigrationError, migrate
from app.services.store import Store

#: sha256 of each shipped migration, in order. Never edit a shipped migration
#: -- add a new one. A database that already ran it never runs the edit, so
#: production and every fresh test database would quietly disagree.
SHIPPED = [
    "be40c9b88f22bea6c0233e7d636ec7d023f3c5e3910ad709ed7da551348d6b8e",
]


def connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, autocommit=True)


def version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def test_shipped_migrations_are_never_edited() -> None:
    assert [hashlib.sha256(m.encode()).hexdigest() for m in MIGRATIONS[: len(SHIPPED)]] == SHIPPED


def test_a_new_database_reaches_the_latest_version(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "screener.db")) as conn:
        migrate(conn)

        assert version(conn) == len(MIGRATIONS)


def test_a_populated_database_from_the_previous_version_upgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows written by the version before survive, and the upgraded schema
    takes writes -- through Store.open, the way a crew's update runs it."""
    path = tmp_path / "screener.db"
    with closing(connect(path)) as conn:
        monkeypatch.setattr(migrations, "MIGRATIONS", MIGRATIONS[:-1])
        migrate(conn)
        # What the previous version could hold. With one migration that is
        # nothing of ours, so a stranger's table stands in; from migration 2
        # on, insert rows here as version N-1 shaped them.
        conn.execute("CREATE TABLE keepsake (note TEXT)")
        conn.execute("INSERT INTO keepsake VALUES ('still here')")
        monkeypatch.setattr(migrations, "MIGRATIONS", MIGRATIONS)

    store = Store.open(path)
    try:
        assert store.warning is None
        assert store.save_call(Call(call_id="CA-new", caller=Caller(number="+15035551234")))
        assert store.get_call("CA-new") is not None
    finally:
        store.close()
    with closing(connect(path)) as conn:
        assert version(conn) == len(MIGRATIONS)
        assert conn.execute("SELECT note FROM keepsake").fetchall() == [("still here",)]


def test_a_failing_migration_leaves_the_database_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All or nothing: half a migration is a schema no code was written for."""
    with closing(connect(tmp_path / "screener.db")) as conn:
        migrate(conn)
        broken = "CREATE TABLE half_done (x INTEGER);\nCREATE TABLE oops (;"
        monkeypatch.setattr(migrations, "MIGRATIONS", (*MIGRATIONS, broken))

        with pytest.raises(MigrationError):
            migrate(conn)

        assert version(conn) == len(MIGRATIONS)
        assert not conn.in_transaction
        tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_schema")}
        assert "half_done" not in tables


def test_a_database_from_newer_code_is_refused(tmp_path: Path) -> None:
    """Rolled back to an older checkout: guessing at a schema this code has
    never seen could corrupt it. Store.open then falls back to memory."""
    with closing(connect(tmp_path / "screener.db")) as conn:
        conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")

        with pytest.raises(MigrationError, match="newer than this code"):
            migrate(conn)
