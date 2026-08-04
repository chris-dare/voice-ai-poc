from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from voice_ai.voice.agent_client import RemoteAgentLLMService


def _sse(*events: dict) -> bytes:
    records = []
    for sequence, event in enumerate(events, start=1):
        records.append(f"id: {sequence}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n")
    return "".join(records).encode()


@pytest.mark.asyncio
async def test_public_voice_turn_uses_durable_response_stream() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "X-Response-Id": "resp_test",
            },
            content=_sse(
                {
                    "type": "response.tool.started",
                    "name": "get_account_balance",
                    "label": "Checking account balance",
                },
                {"type": "response.output_text.delta", "delta": "GHS 42.50"},
                {"type": "response.completed"},
            ),
        )

    on_event = AsyncMock()
    service = RemoteAgentLLMService(
        base_url="http://agent.test",
        session_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        shared_secret="private-secret",
        public_access_token="user-token",
        conversation_id="conv_test",
        model_id="test:assistant",
        on_event=on_event,
    )
    await service._client.aclose()
    service._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer user-token"},
    )
    service.stop_ttfb_metrics = AsyncMock()
    service._push_llm_text = AsyncMock()
    try:
        first_text = await service._process_public_turn("What's my balance?")
    finally:
        await service._client.aclose()

    assert first_text is False
    assert requests[0].url == httpx.URL("http://agent.test/v1/responses")
    assert requests[0].headers["authorization"] == "Bearer user-token"
    assert json.loads(requests[0].content) == {
        "conversation_id": "conv_test",
        "model": "test:assistant",
        "input": "What's my balance?",
        "stream": True,
        "background": True,
        "metadata": {"channel": "voice"},
    }
    service._push_llm_text.assert_awaited_once_with("GHS 42.50")
    assert on_event.await_args.args[0]["data"]["tool"] == "get_account_balance"
    assert service._active_response_id is None


@pytest.mark.asyncio
async def test_public_voice_confirmation_resumes_the_same_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/responses":
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "text/event-stream",
                    "X-Response-Id": "resp_test",
                },
                content=_sse(
                    {"type": "response.output_text.delta", "delta": "Approve this change?"},
                    {
                        "type": "response.requires_action",
                        "response_id": "resp_test",
                        "required_action": {"id": "act_test"},
                    },
                ),
            )
        if request.url.path.endswith("/actions/act_test"):
            assert json.loads(request.content) == {"decision": "approve"}
            return httpx.Response(200, json={"id": "resp_test", "status": "in_progress"})
        assert request.url.path == "/v1/responses/resp_test/events"
        assert request.headers["last-event-id"] == "2"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse(
                {"type": "response.output_text.delta", "delta": "Plan changed."},
                {"type": "response.completed"},
            ),
        )

    service = RemoteAgentLLMService(
        base_url="http://agent.test",
        session_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        shared_secret=None,
        public_access_token="user-token",
        conversation_id="conv_test",
        model_id="test:assistant",
        on_event=AsyncMock(),
    )
    await service._client.aclose()
    service._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer user-token"},
    )
    service.stop_ttfb_metrics = AsyncMock()
    service._push_llm_text = AsyncMock()
    try:
        await service._process_public_turn("Switch my plan")
        assert service._pending_action is not None
        await service._process_public_turn("Yes, please")
    finally:
        await service._client.aclose()

    assert service._pending_action is None
    assert service._push_llm_text.await_count == 2
    assert [request.method for request in requests] == ["POST", "POST", "GET"]


@pytest.mark.asyncio
async def test_public_voice_confirmation_cancels_incomplete_resumed_stream() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/actions/act_test"):
            return httpx.Response(200, json={"id": "resp_test", "status": "in_progress"})
        if request.url.path == "/v1/responses/resp_test/events":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=_sse({"type": "response.output_text.delta", "delta": "Partial change"}),
            )
        assert request.url.path == "/v1/responses/resp_test/cancel"
        return httpx.Response(200, json={"id": "resp_test", "status": "cancelled"})

    service = RemoteAgentLLMService(
        base_url="http://agent.test",
        session_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        shared_secret=None,
        public_access_token="user-token",
        conversation_id="conv_test",
        model_id="test:assistant",
        on_event=AsyncMock(),
    )
    await service._client.aclose()
    service._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer user-token"},
    )
    service._pending_action = {
        "response_id": "resp_test",
        "action_id": "act_test",
        "last_event_id": "2",
    }
    service.stop_ttfb_metrics = AsyncMock()
    service._push_llm_text = AsyncMock()
    try:
        await service._process_public_turn("Yes, please")
    finally:
        await service._client.aclose()

    assert [request.url.path for request in requests] == [
        "/v1/responses/resp_test/actions/act_test",
        "/v1/responses/resp_test/events",
        "/v1/responses/resp_test/cancel",
    ]
    assert service._active_response_id is None


@pytest.mark.asyncio
async def test_public_voice_turn_cancels_a_lingering_response_before_barge_in() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/responses/resp_old/cancel":
            return httpx.Response(200, json={"id": "resp_old", "status": "cancelled"})
        assert request.url.path == "/v1/responses"
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "X-Response-Id": "resp_new",
            },
            content=_sse(
                {"type": "response.output_text.delta", "delta": "New answer"},
                {"type": "response.completed"},
            ),
        )

    service = RemoteAgentLLMService(
        base_url="http://agent.test",
        session_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        shared_secret=None,
        public_access_token="user-token",
        conversation_id="conv_test",
        model_id="test:assistant",
        on_event=AsyncMock(),
    )
    await service._client.aclose()
    service._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer user-token"},
    )
    service._active_response_id = "resp_old"
    service.stop_ttfb_metrics = AsyncMock()
    service._push_llm_text = AsyncMock()
    try:
        first_text = await service._process_public_turn("Interrupt with this")
    finally:
        await service._client.aclose()

    assert first_text is False
    assert [request.url.path for request in requests] == [
        "/v1/responses/resp_old/cancel",
        "/v1/responses",
    ]
    assert service._active_response_id is None


@pytest.mark.asyncio
async def test_public_voice_turn_recovers_from_conversation_busy_race() -> None:
    requests: list[httpx.Request] = []
    response_attempt = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal response_attempt
        requests.append(request)
        if request.url.path == "/v1/responses/resp_busy/cancel":
            return httpx.Response(200, json={"id": "resp_busy", "status": "cancelled"})
        assert request.url.path == "/v1/responses"
        response_attempt += 1
        if response_attempt == 1:
            return httpx.Response(
                409,
                headers={"Content-Type": "application/problem+json"},
                json={
                    "code": "conversation_busy",
                    "response_id": "resp_busy",
                    "detail": "The conversation already has an active response.",
                },
            )
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "X-Response-Id": "resp_new",
            },
            content=_sse(
                {"type": "response.output_text.delta", "delta": "Complete answer"},
                {"type": "response.completed"},
            ),
        )

    service = RemoteAgentLLMService(
        base_url="http://agent.test",
        session_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        shared_secret=None,
        public_access_token="user-token",
        conversation_id="conv_test",
        model_id="test:assistant",
        on_event=AsyncMock(),
    )
    await service._client.aclose()
    service._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer user-token"},
    )
    service.stop_ttfb_metrics = AsyncMock()
    service._push_llm_text = AsyncMock()
    try:
        first_text = await service._process_public_turn("Use my complete question")
    finally:
        await service._client.aclose()

    assert first_text is False
    assert [request.url.path for request in requests] == [
        "/v1/responses",
        "/v1/responses/resp_busy/cancel",
        "/v1/responses",
    ]
    service._push_llm_text.assert_awaited_once_with("Complete answer")


class _BlockingEventStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.closed.set()
        if False:
            yield b""

    async def aclose(self) -> None:
        self.closed.set()


@pytest.mark.asyncio
async def test_cancelled_voice_stream_still_cancels_background_agent_response() -> None:
    requests: list[httpx.Request] = []
    stream = _BlockingEventStream()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/responses/resp_active/cancel":
            return httpx.Response(200, json={"id": "resp_active", "status": "cancelled"})
        assert request.url.path == "/v1/responses"
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "X-Response-Id": "resp_active",
            },
            stream=stream,
        )

    service = RemoteAgentLLMService(
        base_url="http://agent.test",
        session_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        shared_secret=None,
        public_access_token="user-token",
        conversation_id="conv_test",
        model_id="test:assistant",
        on_event=AsyncMock(),
    )
    await service._client.aclose()
    service._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer user-token"},
    )
    task = asyncio.create_task(service._process_public_turn("First fragment"))
    try:
        await asyncio.wait_for(stream.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await service._client.aclose()

    assert stream.closed.is_set()
    assert [request.url.path for request in requests] == [
        "/v1/responses",
        "/v1/responses/resp_active/cancel",
    ]
    assert service._active_response_id is None


@pytest.mark.asyncio
async def test_public_voice_turn_cancels_stream_that_ends_without_terminal_event() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/responses/resp_incomplete/cancel":
            return httpx.Response(200, json={"id": "resp_incomplete", "status": "cancelled"})
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/event-stream",
                "X-Response-Id": "resp_incomplete",
            },
            content=_sse({"type": "response.output_text.delta", "delta": "Partial answer"}),
        )

    service = RemoteAgentLLMService(
        base_url="http://agent.test",
        session_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        shared_secret=None,
        public_access_token="user-token",
        conversation_id="conv_test",
        model_id="test:assistant",
        on_event=AsyncMock(),
    )
    await service._client.aclose()
    service._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer user-token"},
    )
    service.stop_ttfb_metrics = AsyncMock()
    service._push_llm_text = AsyncMock()
    try:
        await service._process_public_turn("Start a response")
    finally:
        await service._client.aclose()

    assert [request.url.path for request in requests] == [
        "/v1/responses",
        "/v1/responses/resp_incomplete/cancel",
    ]
    assert service._active_response_id is None
