from __future__ import annotations

import asyncio
from collections import OrderedDict


class EventBroker:
    """Coordinates in-process listeners waiting for durable response events."""

    def __init__(self, maximum_entries: int = 10_000) -> None:
        if maximum_entries < 1:
            raise ValueError("maximum_entries must be positive")
        self._maximum_entries = maximum_entries
        self._conditions: OrderedDict[str, asyncio.Condition] = OrderedDict()
        self._versions: dict[str, int] = {}
        self._guard = asyncio.Lock()

    @property
    def size(self) -> int:
        return len(self._conditions)

    def _condition(self, response_id: str) -> asyncio.Condition:
        condition = self._conditions.get(response_id)
        if condition is None:
            condition = asyncio.Condition()
            self._conditions[response_id] = condition
        else:
            self._conditions.move_to_end(response_id)
        while len(self._conditions) > self._maximum_entries:
            evicted_id, _condition = self._conditions.popitem(last=False)
            self._versions.pop(evicted_id, None)
        return condition

    async def version(self, response_id: str) -> int:
        async with self._guard:
            self._condition(response_id)
            return self._versions.get(response_id, 0)

    async def publish(self, response_id: str) -> None:
        async with self._guard:
            self._versions[response_id] = self._versions.get(response_id, 0) + 1
            condition = self._condition(response_id)
        async with condition:
            condition.notify_all()

    async def wait(self, response_id: str, observed: int, wait_seconds: float) -> bool:
        async with self._guard:
            if self._versions.get(response_id, 0) != observed:
                return True
            condition = self._condition(response_id)
        try:
            async with condition:
                await asyncio.wait_for(condition.wait(), wait_seconds)
            return True
        except TimeoutError:
            return False
