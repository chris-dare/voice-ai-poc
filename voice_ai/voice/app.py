from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

from voice_ai.shared.config import VoiceSettings, get_voice_settings
from voice_ai.shared.observability import (
    configure_observability,
    instrument_fastapi,
    record_auth_event,
    record_browser_latency,
    record_startup_latency,
)
from voice_ai.shared.startup import PROCESS_STARTED_AT


class SessionCapacity:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.active = 0
        self._lock = asyncio.Lock()

    async def reserve(self) -> bool:
        async with self._lock:
            if self.active >= self.maximum:
                return False
            self.active += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self.active = max(0, self.active - 1)


class BrowserTelemetry(BaseModel):
    event: str = Field(min_length=1, max_length=64)
    duration_ms: float | None = Field(default=None, ge=0, le=600_000)


BROWSER_DURATION_EVENTS = {
    "navigation_interactive",
    "auth_callback_success",
}
BROWSER_AUTH_EVENTS = {
    "refresh_started",
    "refresh_succeeded",
    "refresh_failed",
    "reauthentication_required",
}


def create_app(settings: VoiceSettings | None = None) -> FastAPI:
    configured = settings or get_voice_settings()
    configure_observability(configured, service_name="voice-ai-gateway")
    capacity = SessionCapacity(configured.max_concurrent_sessions)
    session_tasks: set[asyncio.Task[Any]] = set()
    agent_client: httpx.AsyncClient | None = None
    voice_runtime: Any = None
    voice_initialization: asyncio.Task[None] | None = None
    voice_checks: list[Any] = []
    voice_status = "warming"
    browser_auth_enabled = bool(
        configured.api_enabled
        and configured.auth0_domain
        and configured.auth0_audience
        and configured.auth0_spa_client_id
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal agent_client, voice_initialization
        agent_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5, read=None, write=30, pool=5),
        )
        record_startup_latency(
            duration_seconds=perf_counter() - PROCESS_STARTED_AT,
            component="http",
            status="ready",
        )
        voice_initialization = asyncio.create_task(initialize_voice())
        _app.state.voice_initialization = voice_initialization
        yield
        if voice_initialization is not None:
            voice_initialization.cancel()
            await asyncio.gather(voice_initialization, return_exceptions=True)
        for task in tuple(session_tasks):
            task.cancel()
        if session_tasks:
            await asyncio.gather(*session_tasks, return_exceptions=True)
        if voice_runtime is not None:
            await voice_runtime.close()
        await agent_client.aclose()
        agent_client = None

    async def initialize_voice() -> None:
        nonlocal voice_checks, voice_runtime, voice_status
        started = perf_counter()
        try:
            module = await asyncio.to_thread(
                importlib.import_module,
                "voice_ai.voice.runtime",
            )
            voice_runtime = module.VoiceRuntime(configured)
            app.state.voice_runtime = voice_runtime
            while True:
                voice_checks = await voice_runtime.checks()
                voice_status = voice_runtime.status(voice_checks)
                if voice_status != "not_ready":
                    break
                logger.warning("Voice dependencies are not ready; retrying in one second")
                await asyncio.sleep(1)
            logger.info("Voice runtime startup status: {}", voice_status)
            record_startup_latency(
                duration_seconds=perf_counter() - started,
                component="voice",
                status=voice_status,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            voice_status = "not_ready"
            logger.exception("Voice runtime initialization failed")
            record_startup_latency(
                duration_seconds=perf_counter() - started,
                component="voice",
                status="failed",
            )

    app = FastAPI(title=configured.app_name, lifespan=lifespan)

    @app.get("/livez", include_in_schema=False)
    async def livez() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse(
            {
                "status": voice_status,
                "checks": [check.as_dict() for check in voice_checks],
                "active_sessions": capacity.active,
                "max_concurrent_sessions": capacity.maximum,
                "ice_servers": [
                    {
                        "urls": server.urls,
                        "username": server.username,
                        "credential": server.credential,
                    }
                    for server in configured.ice_servers
                ],
            },
            status_code=503 if voice_status in {"warming", "not_ready"} else 200,
        )

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        ready = voice_status not in {"warming", "not_ready"} and capacity.active < capacity.maximum
        return JSONResponse(
            {"status": "ready" if ready else "not_ready"},
            status_code=200 if ready else 503,
        )

    @app.get("/api/config", include_in_schema=False)
    async def browser_config() -> dict[str, Any]:
        return {
            "agent_id": configured.api_agent_id,
            "agent_api_base": "/agent-api/v1",
            "auth": {
                "enabled": browser_auth_enabled,
                "domain": configured.auth0_domain if browser_auth_enabled else None,
                "client_id": configured.auth0_spa_client_id if browser_auth_enabled else None,
                "audience": configured.auth0_audience if browser_auth_enabled else None,
            },
        }

    @app.post("/api/telemetry", include_in_schema=False, status_code=204)
    async def browser_telemetry(metric: BrowserTelemetry) -> Response:
        if metric.event in BROWSER_DURATION_EVENTS and metric.duration_ms is not None:
            record_browser_latency(duration_ms=metric.duration_ms, event=metric.event)
        elif metric.event in BROWSER_AUTH_EVENTS:
            record_auth_event(outcome=metric.event, stage="browser")
        return Response(status_code=204)

    @app.api_route(
        "/agent-api/v1/{upstream_path:path}",
        methods=["GET", "POST", "DELETE"],
        include_in_schema=False,
    )
    async def proxy_agent_api(upstream_path: str, request: Request) -> Response:
        if not configured.api_enabled:
            return JSONResponse(
                {"detail": "The public agent API is disabled."},
                status_code=503,
            )
        if agent_client is None:
            return JSONResponse(
                {"detail": "The agent gateway is starting."},
                status_code=503,
            )
        public_resource = upstream_path.split("/", 1)[0]
        if public_resource not in {"conversations", "models", "responses"}:
            return JSONResponse({"detail": "Not found."}, status_code=404)
        forwarded_headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower()
            in {
                "accept",
                "authorization",
                "content-type",
                "idempotency-key",
                "last-event-id",
                "x-request-id",
            }
        }
        target = f"{configured.agent_base_url.rstrip('/')}/v1/{upstream_path.lstrip('/')}"
        upstream_request = agent_client.build_request(
            request.method,
            target,
            params=request.query_params,
            headers=forwarded_headers,
            content=await request.body(),
        )
        try:
            upstream = await agent_client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            logger.exception("Agent API proxy request failed")
            return JSONResponse(
                {"detail": "The agent service is unavailable."},
                status_code=502,
            )

        response_headers = {
            name: value
            for name, value in upstream.headers.items()
            if name.lower()
            in {
                "cache-control",
                "content-type",
                "idempotent-replayed",
                "location",
                "ratelimit-limit",
                "ratelimit-remaining",
                "ratelimit-reset",
                "retry-after",
                "www-authenticate",
                "x-accel-buffering",
                "x-request-id",
                "x-response-id",
            }
        }
        content_type = upstream.headers.get("content-type", "")
        if content_type.startswith("text/event-stream"):

            async def body() -> AsyncIterator[bytes]:
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    await upstream.aclose()

            return StreamingResponse(
                body(),
                status_code=upstream.status_code,
                headers=response_headers,
                media_type="text/event-stream",
            )

        payload = await upstream.aread()
        await upstream.aclose()
        return Response(
            content=payload,
            status_code=upstream.status_code,
            headers=response_headers,
        )

    @app.post("/api/offer")
    async def offer(
        request: dict[str, Any],
        authorization: Annotated[str | None, Header()] = None,
        x_conversation_id: Annotated[str | None, Header()] = None,
        x_model_id: Annotated[str | None, Header()] = None,
    ) -> dict[str, str] | None:
        if voice_runtime is None or voice_status in {"warming", "not_ready"}:
            raise HTTPException(503, "Voice services are still warming up; inspect /healthz")
        voice_request = voice_runtime.parse_offer(request)
        public_access_token: str | None = None
        conversation_id: str | None = None
        model_id: str | None = None
        if voice_request.pc_id is None:
            if browser_auth_enabled:
                if not authorization or not authorization.startswith("Bearer "):
                    raise HTTPException(401, "A bearer access token is required")
                conversation_id = (x_conversation_id or "").strip()
                if not conversation_id:
                    raise HTTPException(422, "A conversation_id is required for voice")
                model_id = (x_model_id or "").strip()
                if not model_id:
                    raise HTTPException(422, "A model selection is required for voice")
                public_access_token = authorization.removeprefix("Bearer ").strip()
                await _validate_voice_conversation(
                    client=agent_client,
                    base_url=configured.agent_base_url,
                    access_token=public_access_token,
                    conversation_id=conversation_id,
                )
            if not await capacity.reserve():
                raise HTTPException(429, "The local voice assistant is busy; try again shortly")
        reserved = voice_request.pc_id is None
        task_started = False

        async def on_connection(connection) -> None:
            nonlocal task_started
            task = asyncio.create_task(
                _run_session_lazy(
                    connection=connection,
                    settings=configured,
                    capacity=capacity,
                    public_access_token=public_access_token,
                    conversation_id=conversation_id,
                    model_id=model_id,
                )
            )
            session_tasks.add(task)
            task.add_done_callback(session_tasks.discard)
            task_started = True

        try:
            return await voice_runtime.request_handler.handle_web_request(
                request=voice_request,
                webrtc_connection_callback=on_connection,
            )
        except Exception:
            if reserved and not task_started:
                await capacity.release()
            raise

    @app.patch("/api/offer")
    async def ice_candidate(request: dict[str, Any]) -> dict[str, str]:
        if voice_runtime is None:
            raise HTTPException(503, "Voice services are still warming up")
        await voice_runtime.request_handler.handle_patch_request(voice_runtime.parse_patch(request))
        return {"status": "success"}

    dist = _resolve_frontend(configured.frontend_dist)
    if dist.is_dir():

        @app.get("/conversations/{conversation_id}", include_in_schema=False)
        async def conversation_page(conversation_id: str) -> FileResponse:
            return FileResponse(
                dist / "index.html",
                headers={"Cache-Control": "no-cache"},
            )

        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:
        logger.warning("Frontend bundle missing at {}; run npm --prefix frontend run build", dist)

    instrument_fastapi(app)
    return app


async def _run_session_lazy(
    *,
    connection: Any,
    settings: VoiceSettings,
    capacity: SessionCapacity,
    public_access_token: str | None = None,
    conversation_id: str | None = None,
    model_id: str | None = None,
) -> None:
    module = await asyncio.to_thread(importlib.import_module, "voice_ai.voice.runtime")
    await module.run_session(
        connection=connection,
        settings=settings,
        capacity=capacity,
        public_access_token=public_access_token,
        conversation_id=conversation_id,
        model_id=model_id,
    )


async def _validate_voice_conversation(
    *,
    client: httpx.AsyncClient | None,
    base_url: str,
    access_token: str,
    conversation_id: str,
) -> None:
    if client is None:
        raise HTTPException(503, "The agent gateway is starting")
    try:
        response = await client.get(
            f"{base_url.rstrip('/')}/v1/conversations/{conversation_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except httpx.HTTPError as exc:
        raise HTTPException(503, "The agent service is unavailable") from exc
    if response.status_code == 200:
        return
    if response.status_code in {401, 403, 404}:
        raise HTTPException(response.status_code, "Voice conversation access was denied")
    raise HTTPException(503, "The agent service could not validate the conversation")


def _resolve_frontend(path: Path) -> Path:
    if path.is_absolute():
        return path
    return Path.cwd() / path
