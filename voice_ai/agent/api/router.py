import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import IntegrityError

from voice_ai.agent.api.auth import (
    AccessTokenVerifier,
    AuthContext,
    AuthFailure,
    require_scopes,
)
from voice_ai.agent.api.schemas import (
    ConversationCreateRequest,
    RequiredActionDecision,
    ResponseCreateRequest,
)
from voice_ai.agent.api.services import (
    ACTIVE_STATUSES,
    AgentApiService,
    ApiProblem,
)


class FixedWindowRateLimiter:
    def __init__(self, requests_per_minute: int) -> None:
        self._limit = requests_per_minute
        self._requests: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, auth: AuthContext) -> dict[str, str]:
        now = datetime.now(UTC).timestamp()
        key = (auth.tenant_id, auth.subject_id)
        async with self._lock:
            requests = self._requests[key]
            while requests and requests[0] <= now - 60:
                requests.popleft()
            if len(requests) >= self._limit:
                retry_after = max(1, int(60 - (now - requests[0])) + 1)
                raise ApiProblem(
                    429,
                    "rate_limit_exceeded",
                    "The request rate limit has been exceeded.",
                    extensions={"retry_after": retry_after},
                )
            requests.append(now)
            remaining = max(0, self._limit - len(requests))
            reset = max(1, int(60 - (now - requests[0])) + 1)
        return {
            "RateLimit-Limit": str(self._limit),
            "RateLimit-Remaining": str(remaining),
            "RateLimit-Reset": str(reset),
        }


def create_api_router(
    service: AgentApiService,
    verifier: AccessTokenVerifier | None,
    *,
    requests_per_minute: int,
) -> APIRouter:
    router = APIRouter(prefix="/v1")
    limiter = FixedWindowRateLimiter(requests_per_minute)
    bearer = HTTPBearer(
        auto_error=False,
        scheme_name="Auth0Bearer",
        description="Auth0 access token issued for this API's audience.",
    )

    async def authorize(
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(bearer)
        ],
    ) -> AuthContext:
        if verifier is None:
            raise ApiProblem(
                503,
                "authentication_not_configured",
                "The public API authentication provider is not configured.",
            )
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise ApiProblem(
                401,
                "invalid_token",
                "A bearer access token is required.",
            )
        token = credentials.credentials.strip()
        if not token:
            raise ApiProblem(401, "invalid_token", "A bearer access token is required.")
        try:
            auth = await verifier.verify(token)
        except AuthFailure as exc:
            raise ApiProblem(exc.status_code, exc.code, exc.detail) from exc
        request.state.rate_limit_headers = await limiter.check(auth)
        return auth

    @router.post("/conversations")
    async def create_conversation(
        payload: ConversationCreateRequest,
        auth: Annotated[AuthContext, Depends(authorize)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        _scopes(auth, "conversations:write")
        try:
            conversation, replayed = await service.create_conversation(
                auth, payload, idempotency_key
            )
        except IntegrityError:
            if not idempotency_key:
                raise
            await asyncio.sleep(0)
            conversation, replayed = await service.create_conversation(
                auth, payload, idempotency_key
            )
        headers = {
            "Location": f"/v1/conversations/{conversation['id']}",
            **({"Idempotent-Replayed": "true"} if replayed else {}),
        }
        return JSONResponse(conversation, status_code=201, headers=headers)

    @router.get("/conversations")
    async def list_conversations(
        auth: Annotated[AuthContext, Depends(authorize)],
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
        after: Annotated[str | None, Query(max_length=1000)] = None,
    ) -> JSONResponse:
        _scopes(auth, "conversations:read")
        return JSONResponse(
            await service.list_conversations(auth, limit=limit, after=after)
        )

    @router.get("/conversations/{conversation_id}")
    async def get_conversation(
        conversation_id: str,
        auth: Annotated[AuthContext, Depends(authorize)],
    ) -> JSONResponse:
        _scopes(auth, "conversations:read")
        return JSONResponse(await service.get_conversation(auth, conversation_id))

    @router.get("/conversations/{conversation_id}/responses")
    async def list_conversation_responses(
        conversation_id: str,
        auth: Annotated[AuthContext, Depends(authorize)],
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        after: Annotated[str | None, Query(max_length=1000)] = None,
    ) -> JSONResponse:
        _scopes(auth, "conversations:read", "responses:read")
        return JSONResponse(
            await service.list_conversation_responses(
                auth,
                conversation_id,
                limit=limit,
                after=after,
            )
        )

    @router.delete("/conversations/{conversation_id}")
    async def delete_conversation(
        conversation_id: str,
        auth: Annotated[AuthContext, Depends(authorize)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> Response:
        _scopes(auth, "conversations:delete")
        replayed = await service.delete_conversation(
            auth, conversation_id, idempotency_key
        )
        headers = {"Idempotent-Replayed": "true"} if replayed else {}
        return Response(status_code=204, headers=headers)

    @router.post("/responses")
    async def create_response(
        payload: ResponseCreateRequest,
        request: Request,
        auth: Annotated[AuthContext, Depends(authorize)],
        accept: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> Response:
        _scopes(auth, "agents:invoke")
        _validate_accept(payload.stream, accept)
        try:
            response, replayed = await service.create_response(
                auth, payload, idempotency_key
            )
        except IntegrityError as exc:
            if idempotency_key:
                await asyncio.sleep(0)
                response, replayed = await service.create_response(
                    auth, payload, idempotency_key
                )
            elif payload.conversation_id:
                raise ApiProblem(
                    409,
                    "conversation_busy",
                    "The conversation already has an active response.",
                ) from exc
            else:
                raise
        response_id = str(response["id"])
        if response["status"] in {"queued", "in_progress"}:
            await service.start_response(response_id)
        common_headers = {
            "X-Response-Id": response_id,
            **({"Idempotent-Replayed": "true"} if replayed else {}),
        }
        if payload.stream:
            stream = _creation_stream(
                service,
                auth,
                response_id,
                background=payload.background,
            )
            return StreamingResponse(
                stream,
                media_type="text/event-stream",
                headers={
                    **common_headers,
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no",
                },
            )
        if payload.background or (
            replayed and response["status"] in ACTIVE_STATUSES
        ):
            return JSONResponse(
                response,
                status_code=202,
                headers={
                    **common_headers,
                    "Location": f"/v1/responses/{response_id}",
                },
            )
        completed = await service.wait_for_terminal(response_id)
        return JSONResponse(completed, status_code=200, headers=common_headers)

    @router.get("/responses/{response_id}")
    async def get_response(
        response_id: str,
        auth: Annotated[AuthContext, Depends(authorize)],
    ) -> JSONResponse:
        _scopes(auth, "responses:read")
        return JSONResponse(await service.get_response(auth, response_id))

    @router.get("/responses/{response_id}/events")
    async def get_response_events(
        response_id: str,
        auth: Annotated[AuthContext, Depends(authorize)],
        accept: Annotated[str | None, Header()] = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        _scopes(auth, "responses:read")
        if accept and not _accepts(accept, "text/event-stream"):
            raise ApiProblem(
                406, "not_acceptable", "This endpoint produces text/event-stream."
            )
        after = _event_cursor(last_event_id)
        # Validate ownership and cursor before sending the 200 response.
        await service.event_page(auth, response_id, after)
        return StreamingResponse(
            service.stream_events(auth, response_id, after),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/responses/{response_id}/cancel")
    async def cancel_response(
        response_id: str,
        auth: Annotated[AuthContext, Depends(authorize)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        _scopes(auth, "responses:cancel")
        response, replayed = await service.cancel_response(
            auth, response_id, idempotency_key
        )
        return JSONResponse(
            response,
            headers={"Idempotent-Replayed": "true"} if replayed else {},
        )

    @router.post("/responses/{response_id}/actions/{action_id}")
    async def submit_action(
        response_id: str,
        action_id: str,
        payload: RequiredActionDecision,
        auth: Annotated[AuthContext, Depends(authorize)],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        _scopes(auth, "actions:approve")
        response, replayed = await service.submit_action(
            auth,
            response_id,
            action_id,
            payload.decision,
            idempotency_key,
        )
        return JSONResponse(
            response,
            headers={"Idempotent-Replayed": "true"} if replayed else {},
        )

    return router


async def _creation_stream(
    service: AgentApiService,
    auth: AuthContext,
    response_id: str,
    *,
    background: bool,
) -> AsyncIterator[str]:
    ended_normally = False
    try:
        async for record in service.stream_events(auth, response_id, 0):
            yield record
        ended_normally = True
    finally:
        if not ended_normally and not background:
            await service.cancel_response(
                auth,
                response_id,
                None,
                reason="connection_lost",
            )


def _scopes(auth: AuthContext, *required: str) -> None:
    try:
        require_scopes(auth, *required)
    except AuthFailure as exc:
        raise ApiProblem(exc.status_code, exc.code, exc.detail) from exc


def _validate_accept(stream: bool, accept: str | None) -> None:
    expected = "text/event-stream" if stream else "application/json"
    if accept and not _accepts(accept, expected):
        raise ApiProblem(
            406,
            "not_acceptable",
            f"The requested operation produces {expected}.",
        )


def _accepts(header: str, expected: str) -> bool:
    exact: float | None = None
    wildcard: float | None = None
    type_wildcard: float | None = None
    expected_type = expected.split("/", 1)[0]
    for part in header.split(","):
        sections = [section.strip() for section in part.split(";")]
        media_type = sections[0].lower()
        quality = 1.0
        for parameter in sections[1:]:
            if parameter.lower().startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        if media_type == expected:
            exact = quality
        elif media_type == f"{expected_type}/*":
            type_wildcard = quality
        elif media_type == "*/*":
            wildcard = quality
    selected = exact if exact is not None else type_wildcard
    selected = selected if selected is not None else wildcard
    return selected is not None and selected > 0


def _event_cursor(value: str | None) -> int:
    if value is None:
        return 0
    try:
        cursor = int(value)
    except ValueError as exc:
        raise ApiProblem(409, "event_cursor_expired", "Last-Event-ID must be a decimal integer.") from exc
    if cursor < 0:
        raise ApiProblem(409, "event_cursor_expired", "Last-Event-ID cannot be negative.")
    return cursor
