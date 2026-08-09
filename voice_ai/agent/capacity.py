from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import uuid4

from loguru import logger
from sqlalchemy import delete, func, text
from sqlmodel import select

from voice_ai.agent.api.models import ExecutionCapacityLease
from voice_ai.agent.persistence.database import Database
from voice_ai.shared.config import AgentSettings
from voice_ai.shared.observability import record_agent_capacity


def _now() -> datetime:
    return datetime.now(UTC)


class ExecutionCapacityError(RuntimeError):
    """Raised when a bounded model/tool route cannot admit work in time."""

    def __init__(self, resource_kind: str, resource_key: str) -> None:
        self.resource_kind = resource_kind
        self.resource_key = resource_key
        super().__init__(f"{resource_kind} capacity is exhausted for {resource_key}")

    @property
    def code(self) -> str:
        return f"{self.resource_kind}_capacity_exhausted"


class DistributedExecutionCapacity:
    """PostgreSQL-coordinated, crash-recoverable model and tool capacity."""

    def __init__(self, database: Database, settings: AgentSettings) -> None:
        self.database = database
        self.settings = settings
        # SQLite is restricted to unit tests. A single lock preserves deterministic
        # semantics there; PostgreSQL advisory locks coordinate production replicas.
        self._local_lock = asyncio.Lock()

    @asynccontextmanager
    async def reserve(
        self,
        *,
        resource_kind: str,
        resource_key: str,
        owner_id: str,
    ) -> AsyncIterator[None]:
        limit = self._limit(resource_kind)
        started = perf_counter()
        lease_id = await self._acquire(
            resource_kind=resource_kind,
            resource_key=resource_key,
            owner_id=owner_id,
            limit=limit,
        )
        wait_ms = round((perf_counter() - started) * 1_000, 1)
        record_agent_capacity(
            event="acquired",
            resource_kind=resource_kind,
            resource_key=resource_key,
            wait_ms=wait_ms,
        )
        operation = asyncio.current_task()
        assert operation is not None
        heartbeat = asyncio.create_task(
            self._heartbeat(lease_id, owner_id, operation),
            name=f"capacity-heartbeat-{lease_id}",
        )
        try:
            yield
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await asyncio.shield(self._release(lease_id, owner_id))
            record_agent_capacity(
                event="released",
                resource_kind=resource_kind,
                resource_key=resource_key,
                wait_ms=0,
            )

    async def prune(self) -> None:
        async with self.database.session_factory.begin() as session:
            await session.exec(
                delete(ExecutionCapacityLease).where(ExecutionCapacityLease.expires_at <= _now())
            )

    def _limit(self, resource_kind: str) -> int:
        if resource_kind == "model":
            return self.settings.agent_model_route_concurrency
        if resource_kind == "tool":
            return self.settings.agent_tool_route_concurrency
        raise ValueError(f"Unsupported capacity resource kind: {resource_kind}")

    async def _acquire(
        self,
        *,
        resource_kind: str,
        resource_key: str,
        owner_id: str,
        limit: int,
    ) -> str:
        deadline = perf_counter() + self.settings.agent_capacity_wait_seconds
        while True:
            lease_id = f"cap_{uuid4().hex}"
            if await self._try_acquire(
                lease_id=lease_id,
                resource_kind=resource_kind,
                resource_key=resource_key,
                owner_id=owner_id,
                limit=limit,
            ):
                return lease_id
            remaining = deadline - perf_counter()
            if remaining <= 0:
                record_agent_capacity(
                    event="rejected",
                    resource_kind=resource_kind,
                    resource_key=resource_key,
                    wait_ms=round(self.settings.agent_capacity_wait_seconds * 1_000, 1),
                )
                raise ExecutionCapacityError(resource_kind, resource_key)
            await asyncio.sleep(min(0.1, remaining))

    async def _try_acquire(
        self,
        *,
        lease_id: str,
        resource_kind: str,
        resource_key: str,
        owner_id: str,
        limit: int,
    ) -> bool:
        async with self._local_lock:
            now = _now()
            async with self.database.session_factory.begin() as session:
                if self.database.engine.dialect.name == "postgresql":
                    await session.exec(
                        text("SELECT pg_advisory_xact_lock(hashtext(:capacity_key))"),
                        params={
                            "capacity_key": f"agent:{resource_kind}:{resource_key}",
                        },
                    )
                await session.exec(
                    delete(ExecutionCapacityLease).where(
                        ExecutionCapacityLease.resource_kind == resource_kind,
                        ExecutionCapacityLease.resource_key == resource_key,
                        ExecutionCapacityLease.expires_at <= now,
                    )
                )
                active = (
                    await session.exec(
                        select(func.count(ExecutionCapacityLease.id)).where(
                            ExecutionCapacityLease.resource_kind == resource_kind,
                            ExecutionCapacityLease.resource_key == resource_key,
                            ExecutionCapacityLease.expires_at > now,
                        )
                    )
                ).one()
                if active >= limit:
                    return False
                session.add(
                    ExecutionCapacityLease(
                        id=lease_id,
                        resource_kind=resource_kind,
                        resource_key=resource_key,
                        owner_id=owner_id,
                        acquired_at=now,
                        heartbeat_at=now,
                        expires_at=now
                        + timedelta(seconds=self.settings.agent_capacity_lease_seconds),
                    )
                )
                return True

    async def _heartbeat(
        self,
        lease_id: str,
        owner_id: str,
        operation: asyncio.Task[object],
    ) -> None:
        interval = min(
            self.settings.agent_capacity_heartbeat_seconds,
            max(1, self.settings.agent_capacity_lease_seconds // 3),
        )
        while True:
            await asyncio.sleep(interval)
            try:
                now = _now()
                async with self.database.session_factory.begin() as session:
                    lease = await session.get(
                        ExecutionCapacityLease,
                        lease_id,
                        with_for_update=True,
                    )
                    if lease is None or lease.owner_id != owner_id:
                        operation.cancel()
                        return
                    lease.heartbeat_at = now
                    lease.expires_at = now + timedelta(
                        seconds=self.settings.agent_capacity_lease_seconds
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Execution-capacity heartbeat failed; cancelling owned work")
                operation.cancel()
                return

    async def _release(self, lease_id: str, owner_id: str) -> None:
        try:
            async with self.database.session_factory.begin() as session:
                lease = await session.get(
                    ExecutionCapacityLease,
                    lease_id,
                    with_for_update=True,
                )
                if lease is not None and lease.owner_id == owner_id:
                    await session.delete(lease)
        except Exception:
            # Lease expiry is the crash-safe release path when the database is
            # temporarily unavailable during cleanup.
            logger.exception("Execution-capacity lease release failed; awaiting expiry")
