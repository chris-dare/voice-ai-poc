import httpx
import pytest

from voice_ai.agent.capabilities import probe_ollama


@pytest.mark.asyncio
async def test_detects_tools_and_thinking() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/show"
        return httpx.Response(
            200,
            json={"capabilities": ["completion", "tools", "thinking"]},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ollama.test",
    ) as client:
        result = await probe_ollama("http://ollama.test", "qwen3:1.7b", client=client)

    assert result.reachable
    assert result.installed
    assert result.supports_tools
    assert result.supports_thinking


@pytest.mark.asyncio
async def test_missing_model_is_distinct_from_unreachable_server() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
    ) as client:
        result = await probe_ollama("http://ollama.test", "missing", client=client)

    assert result.reachable
    assert not result.installed
    assert not result.supports_tools
