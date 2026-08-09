from __future__ import annotations

import json

import pytest

from voice_ai.shared.http import RequestBodyLimitMiddleware


async def _invoke(*, body_chunks: list[bytes], headers: list[tuple[bytes, bytes]]):
    chunk_index = 0
    sent: list[dict] = []

    async def receive():
        nonlocal chunk_index
        if chunk_index >= len(body_chunks):
            return {"type": "http.disconnect"}
        body = body_chunks[chunk_index]
        chunk_index += 1
        return {
            "type": "http.request",
            "body": body,
            "more_body": chunk_index < len(body_chunks),
        }

    async def send(message):
        sent.append(message)

    async def application(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": body})

    middleware = RequestBodyLimitMiddleware(application, max_bytes=8)
    await middleware(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/responses",
            "raw_path": b"/v1/responses",
            "query_string": b"",
            "headers": headers,
            "server": ("test", 443),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
            "state": {"request_id": "req_from_outer_middleware"},
        },
        receive,
        send,
    )
    return sent


@pytest.mark.asyncio
async def test_rejects_declared_oversized_body_before_application() -> None:
    messages = await _invoke(
        body_chunks=[b"not-read"],
        headers=[(b"content-length", b"9"), (b"x-request-id", b"req_test")],
    )

    assert messages[0]["status"] == 413
    assert (b"content-type", b"application/problem+json") in messages[0]["headers"]
    payload = json.loads(messages[1]["body"])
    assert payload["code"] == "request_body_too_large"
    assert payload["request_id"] == "req_from_outer_middleware"


@pytest.mark.asyncio
async def test_rejects_chunked_body_when_cumulative_size_exceeds_limit() -> None:
    messages = await _invoke(body_chunks=[b"12345", b"6789"], headers=[])

    assert messages[0]["status"] == 413
    assert json.loads(messages[1]["body"])["status"] == 413


@pytest.mark.asyncio
async def test_allows_body_at_exact_limit() -> None:
    messages = await _invoke(
        body_chunks=[b"1234", b"5678"],
        headers=[(b"content-length", b"8")],
    )

    assert messages[0]["status"] == 204
    assert messages[1]["body"] == b"12345678"


@pytest.mark.asyncio
async def test_preserves_disconnect_detection_after_replaying_body() -> None:
    received = 0
    observed_disconnect = False

    async def receive():
        nonlocal received
        received += 1
        if received == 1:
            return {"type": "http.request", "body": b"ok", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    async def application(_scope, receive, _send):
        nonlocal observed_disconnect
        assert (await receive())["body"] == b"ok"
        observed_disconnect = (await receive())["type"] == "http.disconnect"

    middleware = RequestBodyLimitMiddleware(application, max_bytes=8)
    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/stream",
            "headers": [],
        },
        receive,
        send,
    )

    assert observed_disconnect
