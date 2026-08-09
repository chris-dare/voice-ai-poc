from __future__ import annotations

from typing import Any
from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP request bodies before application parsing.

    ``Content-Length`` provides an early rejection path, while wrapping ``receive``
    also covers chunked requests and clients that declare an incorrect length.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = _content_length(scope)
        if content_length is not None and content_length > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        body = bytearray()
        disconnected = False
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        replayed = False

        async def bounded_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": disconnected,
                }
            if disconnected:
                return {"type": "http.disconnect"}
            return await receive()

        await self.app(scope, bounded_receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        state = scope.get("state")
        state_request_id = state.get("request_id") if isinstance(state, dict) else None
        request_id = (
            str(state_request_id)
            if state_request_id
            else _header(scope, b"x-request-id") or f"req_{uuid4().hex}"
        )
        path = str(scope.get("path") or "/")
        response = JSONResponse(
            {
                "type": "https://api.example.com/problems/request-body-too-large",
                "title": "Request body too large",
                "status": 413,
                "detail": f"The request body exceeds the {self.max_bytes}-byte limit.",
                "instance": path,
                "code": "request_body_too_large",
                "request_id": request_id,
            },
            status_code=413,
            media_type="application/problem+json",
            headers={"X-Request-Id": request_id},
        )
        await response(scope, receive, send)


def _content_length(scope: Scope) -> int | None:
    value = _header(scope, b"content-length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return max(0, parsed)


def _header(scope: Scope, name: bytes) -> str | None:
    for candidate, value in scope.get("headers", []):
        if candidate.lower() == name:
            return value.decode("latin-1")
    return None


def request_body_limit(app: Any, *, max_bytes: int) -> None:
    """Install the shared ASGI middleware on a FastAPI-compatible app."""
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=max_bytes)
