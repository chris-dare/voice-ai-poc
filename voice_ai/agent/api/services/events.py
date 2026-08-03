from __future__ import annotations

import asyncio


class EventBroker:
    """Coordinates in-process subscribers waiting for durable response events."""

    def __init__(self) -> None:
        self._conditions: dict[str, asyncio.Condition] = {}
        self._versions: dict[str, int] = {}
        self._guard = asyncio.Lock()

    async def version(self, response_id: str) -> int:
        async with self._guard:
            return self._versions.get(response_id, 0)

    async def publish(self, response_id: str) -> None:
        async with self._guard:
            self._versions[response_id] = self._versions.get(response_id, 0) + 1
            condition = self._conditions.setdefault(response_id, asyncio.Condition())
        async with condition:
            condition.notify_all()

    async def wait(self, response_id: str, observed: int, wait_seconds: float) -> bool:
        async with self._guard:
            if self._versions.get(response_id, 0) != observed:
                return True
            condition = self._conditions.setdefault(response_id, asyncio.Condition())
        try:
            async with condition:
                await asyncio.wait_for(condition.wait(), wait_seconds)
            return True
        except TimeoutError:
            return False
