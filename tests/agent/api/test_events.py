import pytest

from voice_ai.agent.api.services.events import EventBroker


@pytest.mark.asyncio
async def test_event_broker_cache_is_bounded() -> None:
    broker = EventBroker(maximum_entries=2)

    await broker.publish("resp_1")
    await broker.publish("resp_2")
    await broker.publish("resp_3")

    assert broker.size == 2
    # Re-observing an evicted response remains safe: PostgreSQL is authoritative,
    # and the broker merely reduces cross-request polling latency.
    assert await broker.version("resp_1") == 0
    assert broker.size == 2
