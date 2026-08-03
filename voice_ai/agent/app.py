from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from voice_ai.agent.api.auth import AccessTokenVerifier, Auth0AccessTokenVerifier
from voice_ai.agent.api.router import create_api_router
from voice_ai.agent.api.services import AgentApiService, ApiProblem
from voice_ai.agent.persistence.database import Database
from voice_ai.agent.protocol import AgentTurnRequest
from voice_ai.agent.runtime import AgentRuntime
from voice_ai.shared.config import Settings, get_settings
from voice_ai.shared.observability import (
    configure_observability,
    instrument_fastapi,
    instrument_sqlalchemy,
    record_startup_latency,
)
from voice_ai.shared.startup import PROCESS_STARTED_AT


def create_agent_app(
    settings: Settings | None = None,
    *,
    token_verifier: AccessTokenVerifier | None = None,
) -> FastAPI:
    configured = settings or get_settings()
    configure_observability(configured, service_name="voice-ai-agent")
    database = Database(configured.database_url)
    instrument_sqlalchemy(database.engine)
    runtime = AgentRuntime(configured)
    api_service = AgentApiService(configured, database, runtime)
    verifier = token_verifier
    if (
        verifier is None
        and configured.api_enabled
        and not configured.api_auth_errors()
    ):
        verifier = Auth0AccessTokenVerifier(configured)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime = runtime
        app.state.agent_api_service = api_service
        await runtime.startup()
        await api_service.startup()
        record_startup_latency(
            duration_seconds=perf_counter() - PROCESS_STARTED_AT,
            component="http",
            status="ready",
        )
        yield
        await api_service.shutdown()
        await runtime.shutdown()
        await database.close()

    app = FastAPI(title=f"{configured.app_name} Agent", lifespan=lifespan)
    app.include_router(
        create_api_router(
            api_service,
            verifier,
            requests_per_minute=configured.api_rate_limit_rpm,
        )
    )

    @app.middleware("http")
    async def protocol_headers(request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or f"req_{uuid4().hex}"
        request.state.request_id = request_id
        requires_json = (
            request.method == "POST"
            and (
                request.url.path in {"/v1/conversations", "/v1/responses"}
                or "/actions/" in request.url.path
            )
        )
        content_type = request.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if requires_json and content_type != "application/json":
            return JSONResponse(
                {
                    "type": "https://api.example.com/problems/unsupported-media-type",
                    "title": "Unsupported media type",
                    "status": 415,
                    "detail": "This endpoint requires application/json.",
                    "instance": request.url.path,
                    "code": "unsupported_media_type",
                    "request_id": request_id,
                },
                status_code=415,
                media_type="application/problem+json",
                headers={"X-Request-Id": request_id},
            )
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        for name, value in getattr(request.state, "rate_limit_headers", {}).items():
            response.headers[name] = value
        return response

    @app.exception_handler(ApiProblem)
    async def api_problem(request: Request, exc: ApiProblem) -> JSONResponse:
        request_id = getattr(request.state, "request_id", f"req_{uuid4().hex}")
        body = {
            "type": f"https://api.example.com/problems/{exc.code.replace('_', '-')}",
            "title": exc.title,
            "status": exc.status_code,
            "detail": exc.detail,
            "instance": request.url.path,
            "code": exc.code,
            "request_id": request_id,
            **exc.extensions,
        }
        headers = {}
        if exc.status_code == 401:
            headers["WWW-Authenticate"] = "Bearer"
        if exc.status_code == 429 and exc.extensions.get("retry_after"):
            headers["Retry-After"] = str(exc.extensions["retry_after"])
        return JSONResponse(
            body,
            status_code=exc.status_code,
            media_type="application/problem+json",
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_problem(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", f"req_{uuid4().hex}")
        errors = [
            {
                "path": "$." + ".".join(str(part) for part in error["loc"] if part != "body"),
                "code": error["type"],
                "message": error["msg"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            {
                "type": "https://api.example.com/problems/invalid-request",
                "title": "Invalid request",
                "status": 422,
                "detail": "The request contains invalid fields.",
                "instance": request.url.path,
                "code": "invalid_input",
                "request_id": request_id,
                "errors": errors,
            },
            status_code=422,
            media_type="application/problem+json",
        )

    @app.exception_handler(HTTPException)
    async def http_problem(request: Request, exc: HTTPException) -> JSONResponse:
        request_id = getattr(request.state, "request_id", f"req_{uuid4().hex}")
        code = {
            401: "invalid_token",
            403: "forbidden",
            404: "not_found",
        }.get(exc.status_code, "http_error")
        return JSONResponse(
            {
                "type": f"https://api.example.com/problems/{code.replace('_', '-')}",
                "title": {
                    401: "Unauthorized",
                    403: "Forbidden",
                    404: "Not found",
                }.get(exc.status_code, "Request failed"),
                "status": exc.status_code,
                "detail": str(exc.detail),
                "instance": request.url.path,
                "code": code,
                "request_id": request_id,
            },
            status_code=exc.status_code,
            media_type="application/problem+json",
            headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else {},
        )

    @app.exception_handler(Exception)
    async def unexpected_problem(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", f"req_{uuid4().hex}")
        logger.opt(exception=exc).error(
            "Unhandled agent API error request_id={}", request_id
        )
        return JSONResponse(
            {
                "type": "https://api.example.com/problems/internal-server-error",
                "title": "Internal server error",
                "status": 500,
                "detail": "The server could not complete the request.",
                "instance": request.url.path,
                "code": "internal_server_error",
                "request_id": request_id,
            },
            status_code=500,
            media_type="application/problem+json",
        )

    async def authorize(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        secret = configured.agent_shared_secret
        if secret and not hmac.compare_digest(
            authorization or "",
            f"Bearer {secret}",
        ):
            raise HTTPException(401, "Invalid agent service credential")

    @app.get("/livez", include_in_schema=False)
    async def livez() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        model_status = await runtime.model_readiness()
        database_ready = False
        try:
            await database.ping_ms()
            database_ready = True
        except Exception:
            logger.debug("Agent database readiness check failed", exc_info=True)
        ready = bool(model_status["ready"]) and database_ready
        auth_ready = not configured.api_enabled or verifier is not None
        ready = ready and auth_ready
        return JSONResponse(
            {
                "status": "ready" if ready else "not_ready",
                "model": model_status["model"],
                "provider": model_status["provider"],
                "model_detail": model_status["detail"],
                "fallbacks": model_status["fallbacks"],
                "tools": True,
                "database": database_ready,
                "api_auth": auth_ready,
            },
            status_code=200 if ready else 503,
        )

    @app.post(
        "/v1/turns/stream",
        dependencies=[Depends(authorize)],
        include_in_schema=False,
    )
    async def stream_turn(
        turn: AgentTurnRequest,
        request: Request,
    ) -> StreamingResponse:
        async def ndjson() -> AsyncIterator[str]:
            async for event in runtime.stream_turn(turn):
                if await request.is_disconnected():
                    break
                yield event.model_dump_json() + "\n"

        return StreamingResponse(ndjson(), media_type="application/x-ndjson")

    @app.delete(
        "/v1/sessions/{session_id}",
        dependencies=[Depends(authorize)],
        include_in_schema=False,
    )
    async def close_session(session_id: UUID) -> dict[str, str]:
        await runtime.close_session(session_id)
        return {"status": "closed"}

    instrument_fastapi(app)
    return app
