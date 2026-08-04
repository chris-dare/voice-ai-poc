from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from statistics import median
from time import perf_counter

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel.ext.asyncio.session import AsyncSession

from voice_ai.agent.persistence.model import TableModel


class Database:
    """Owns the agent service's async database engine and sessions."""

    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        pool_size: int = 10,
        max_overflow: int = 20,
        pool_timeout: int = 30,
    ) -> None:
        pool_options = (
            {}
            if url.startswith("sqlite")
            else {
                "pool_size": pool_size,
                "max_overflow": max_overflow,
                "pool_timeout": pool_timeout,
            }
        )
        self.engine: AsyncEngine = create_async_engine(
            url,
            echo=echo,
            pool_pre_ping=True,
            **pool_options,
        )
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        session = self.session_factory()
        try:
            yield session
        finally:
            # Streaming clients can disconnect while a query is in flight. Shield
            # session cleanup so task cancellation cannot leak a checked-out
            # asyncpg connection from the pool.
            await asyncio.shield(session.close())

    async def close(self) -> None:
        await self.engine.dispose()

    async def ping_ms(self) -> float:
        async with self.session() as session:
            await session.exec(text("SELECT 1"))
            samples: list[float] = []
            for _ in range(3):
                started = perf_counter()
                await session.exec(text("SELECT 1"))
                samples.append((perf_counter() - started) * 1000)
        return median(samples)

    async def create_schema(self) -> None:
        # Import model modules before create_all so every table is registered.
        from voice_ai.agent import eval_models as _eval_models  # noqa: F401
        from voice_ai.agent.api import models as _api_models  # noqa: F401
        from voice_ai.agent.telco import models as _telco_models  # noqa: F401

        async with self.engine.begin() as connection:
            await connection.run_sync(TableModel.metadata.create_all)
