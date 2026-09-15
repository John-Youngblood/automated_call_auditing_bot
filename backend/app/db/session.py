"""Engine and session lifecycle.

Wrapped in a small class rather than module-level globals so a test can build
its own isolated database per test, exactly as it builds its own app.

Schema management is ``create_all`` on startup. That is right for a scaffold
with two tables and no data worth preserving; the moment you need to change a
column on a database that holds real call history, add Alembic and migrate
instead -- ``create_all`` will not alter an existing table and the mismatch
fails at query time, not at boot.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
