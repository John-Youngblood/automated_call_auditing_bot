"""Engine and session lifecycle.

Wrapped in a small class rather than module-level globals so a test can build
its own isolated database per test, exactly as it builds its own app.

Schema management is ``create_all`` on startup. Fine for a scaffold, with one
sharp edge worth knowing: ``create_all`` creates missing tables but never
*alters* existing ones. Remove a column from a model and the old column stays
in the database -- and if it was ``NOT NULL``, every insert then fails at query
time while the app looks perfectly healthy.

That is not hypothetical; it happened here when ``transcript_line_count`` was
dropped, and it silently lost call history for as long as it went unnoticed.
:meth:`Database.create_schema` now checks for that drift at boot and refuses to
start rather than running degraded. Add Alembic before this holds data you
would miss.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.db.models import Base

logger = logging.getLogger(__name__)


def _is_memory_url(url: str) -> bool:
    return url.endswith(":memory:") or url.endswith("sqlite+aiosqlite://")


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, url: str, echo: bool = False) -> None:
        self.url = url
        kwargs: dict[str, object] = {"echo": echo}

        if _is_memory_url(url):
            # Each new connection to ":memory:" gets its own empty database,
            # so a pool would hand out sessions that cannot see each other's
            # tables. StaticPool keeps one connection for the whole engine.
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}

        self._engine: AsyncEngine = create_async_engine(url, **kwargs)
        self._sessionmaker = async_sessionmaker(
            self._engine,
            expire_on_commit=False,  # objects stay usable after commit
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def create_schema(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            drift = await connection.run_sync(_find_drift)

        for table, missing in drift.warn.items():
            logger.warning(
                "table %r has columns the models no longer use: %s "
                "(harmless, but they will never be written)",
                table,
                ", ".join(missing),
            )

        if drift.blocking:
            details = "; ".join(
                f"{table}: {', '.join(cols)}" for table, cols in drift.blocking.items()
            )
            raise SchemaDriftError(
                f"database schema is out of date and writes will fail -- {details}. "
                "create_all() cannot alter existing tables. In development, delete the "
                "database file (or `docker compose down -v`) and restart. In production, "
                "add a migration."
            )

        logger.info("database ready at %s", self.url)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session scoped to one unit of work, committed on clean exit.

        Rolling back on exception matters here: the blocklist write and the
        hang-up that follows it should not leave a half-applied block behind
        if the write fails.
        """
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self._engine.dispose()


class SchemaDriftError(RuntimeError):
    """The database's tables no longer match the models closely enough to write."""


@dataclass(slots=True)
class _Drift:
    #: Table -> columns that will make every INSERT fail.
    blocking: dict[str, list[str]]
    #: Table -> columns that are merely vestigial.
    warn: dict[str, list[str]]


def _find_drift(connection: Connection) -> _Drift:
    """Compare live tables against the models.

    The failure that matters is a leftover ``NOT NULL`` column with no default:
    the model does not know to supply it, so every insert violates the
    constraint. Everything else is reported but tolerated.
    """
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    blocking: dict[str, list[str]] = {}
    warn: dict[str, list[str]] = {}

    for name, table in Base.metadata.tables.items():
        if name not in existing:
            continue
        expected = {column.name for column in table.columns}
        vestigial, unwritable = [], []

        for column in inspector.get_columns(name):
            if column["name"] in expected:
                continue
            if not column.get("nullable", True) and column.get("default") is None:
                unwritable.append(column["name"])
            else:
                vestigial.append(column["name"])

        if unwritable:
            blocking[name] = sorted(unwritable)
        if vestigial:
            warn[name] = sorted(vestigial)

    return _Drift(blocking=blocking, warn=warn)
