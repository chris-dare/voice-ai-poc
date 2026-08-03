from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from voice_ai.agent.api.auth import AuthContext
from voice_ai.agent.api.schemas import (
    ConversationCreateRequest,
    ResponseCreateRequest,
)
from voice_ai.agent.api.services import (
    AgentApiService,
    ApiProblem,
    request_fingerprint,
)
from voice_ai.agent.app import create_agent_app
from voice_ai.agent.persistence.database import Base, Database
from voice_ai.agent.protocol import (
    AgentTurnRequest,
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    ToolCompleted,
    ToolStarted,
)
from voice_ai.agent.usage import TurnUsage
from voice_ai.shared.config import Settings


class FakeRuntime:
    def __init__(self) -> None:
        self.states: dict[UUID, dict] = {}
        self.pending: set[UUID] = set()

    async def stream_turn(self, request: AgentTurnRequest):
        yield ResponseStarted(turn_id=request.turn_id)
        if request.text == "yes please":
            self.pending.discard(request.session_id)
            text = "The plan change was submitted."
        elif request.text == "no":
            self.pending.discard(request.session_id)
            text = "No plan change was made."
        elif "switch" in request.text.lower():
            self.pending.add(request.session_id)
            text = "Flex 20 costs GHS 20.00 per month."
        else:
            text = f"Answer: {request.text}"
        if "balance" in request.text.lower():
            yield ToolStarted(tool="get_account_balance", label="Checking balance")
            yield ToolCompleted(tool="get_account_balance", label="Checking balance")
        state = self.states.setdefault(request.session_id, {"turns": []})
        state["turns"].append(request.text)
        state["pending"] = request.session_id in self.pending
        yield TextDelta(text=text)
        yield ResponseCompleted(
            turn_id=request.turn_id,
            latency_ms=1,
            usage=TurnUsage(
                input_tokens=12,
                output_tokens=4,
                total_tokens=16,
                model_requests=1,
                tool_calls=1 if "balance" in request.text.lower() else 0,
                wall_clock_ms=1,
                model_duration_ms=0.5,
                route_model="test:assistant",
                route_provider="test",
                gateway=False,
                actual_models=["test-assistant"],
            ),
        )

    async def restore_session_state(self, session_id: UUID, state: dict | None) -> None:
        if state:
            self.states[session_id] = state
            if state.get("pending"):
                self.pending.add(session_id)
            else:
                self.pending.discard(session_id)

    async def export_session_state(self, session_id: UUID) -> dict:
        return self.states.get(session_id, {})

    async def pending_confirmation(self, session_id: UUID) -> dict | None:
        if session_id not in self.pending:
            return None
        return {
            "plan_code": "FLEX_20",
            "plan_name": "Flex 20",
            "quoted_price": "20.00",
            "currency": "GHS",
            "created_at": datetime.now(UTC),
        }

    async def clear_confirmation(self, session_id: UUID) -> None:
        self.pending.discard(session_id)
        if session_id in self.states:
            self.states[session_id]["pending"] = False

    async def close_session(self, session_id: UUID) -> None:
        self.pending.discard(session_id)
        self.states.pop(session_id, None)


class LockingRuntime(FakeRuntime):
    """Match AgentRuntime's non-reentrant per-session turn lock."""

    def __init__(self) -> None:
        super().__init__()
        self.lock = asyncio.Lock()

    async def stream_turn(self, request: AgentTurnRequest):
        async with self.lock:
            yield ResponseStarted(turn_id=request.turn_id)
            self.states[request.session_id] = {"turns": [request.text]}
            yield TextDelta(text="Lock-safe answer")
            yield ResponseCompleted(turn_id=request.turn_id, latency_ms=1)

    async def pending_confirmation(self, session_id: UUID) -> dict | None:
        async with self.lock:
            return None

    async def export_session_state(self, session_id: UUID) -> dict:
        async with self.lock:
            return self.states.get(session_id, {})


class BlockingRuntime(FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def stream_turn(self, request: AgentTurnRequest):
        yield ResponseStarted(turn_id=request.turn_id)
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()


@pytest.fixture
async def public_service(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'public-api.db'}")
    # Importing the service above registers all public tables on Base.metadata.
    await database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        api_enabled=True,
        auth0_domain="tenant.example.auth0.com",
        auth0_audience="https://voice-api.example.com",
        confirmation_ttl_seconds=30,
    )
    runtime = FakeRuntime()
    service = AgentApiService(settings, database, runtime)  # type: ignore[arg-type]
    await service.startup()
    try:
        yield service
    finally:
        await service.shutdown()
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await database.close()


@pytest.fixture
def auth() -> AuthContext:
    return AuthContext(
        tenant_id="tenant_1",
        subject_id="auth0|user_1",
        scopes=frozenset(
            {
                "agents:invoke",
                "conversations:write",
                "conversations:read",
                "conversations:delete",
                "responses:read",
                "responses:cancel",
                "actions:approve",
            }
        ),
        claims={},
    )


@pytest.mark.asyncio
async def test_durable_conversation_response_and_replay(public_service, auth) -> None:
    conversation, replayed = await public_service.create_conversation(
        auth,
        ConversationCreateRequest(agent_id="agent_general_assistant"),
        "conversation-key",
    )
    assert not replayed

    repeated, replayed = await public_service.create_conversation(
        auth,
        ConversationCreateRequest(agent_id="agent_general_assistant"),
        "conversation-key",
    )
    assert replayed
    assert repeated["id"] == conversation["id"]

    payload = ResponseCreateRequest(
        conversation_id=conversation["id"],
        input="What is my balance?",
        background=True,
        stream=True,
    )
    response, replayed = await public_service.create_response(auth, payload, "response-key")
    assert not replayed
    await public_service.start_response(response["id"])
    completed = await public_service.wait_for_terminal(response["id"])

    assert completed["status"] == "completed"
    assert completed["output"][0]["content"][0]["text"] == "Answer: What is my balance?"
    assert completed["output"][1] == {
        "id": completed["output"][1]["id"],
        "type": "tool_activity",
        "name": "get_account_balance",
        "label": "Checking balance",
        "status": "succeeded",
    }
    assert completed["usage"]["input_tokens"] == 12
    assert completed["usage"]["output_tokens"] == 4
    assert completed["usage"]["model_requests"] == 1
    assert completed["usage"]["tool_calls"] == 1
    assert completed["usage"]["route_model"] == "test:assistant"
    assert completed["usage"]["latency"]["time_to_first_text_ms"] is not None
    assert completed["usage"]["slo"]["met"]["completion"] is True

    events, status = await public_service.event_page(auth, response["id"], 0)
    assert status == "completed"
    assert [event["sequence_number"] for event in events] == list(
        range(1, len(events) + 1)
    )
    assert events[-1]["type"] == "response.completed"

    replay = [
        record
        async for record in public_service.stream_events(
            auth, response["id"], events[-2]["sequence_number"]
        )
    ]
    assert len(replay) == 1
    assert "event: response.completed" in replay[0]


@pytest.mark.asyncio
async def test_response_worker_does_not_reenter_runtime_lock(public_service, auth) -> None:
    public_service.runtime = LockingRuntime()
    conversation, _ = await public_service.create_conversation(
        auth,
        ConversationCreateRequest(agent_id="agent_general_assistant"),
        None,
    )
    response, _ = await public_service.create_response(
        auth,
        ResponseCreateRequest(
            conversation_id=conversation["id"],
            input="Hello",
            background=True,
            stream=True,
        ),
        None,
    )

    await public_service.start_response(response["id"])
    completed = await asyncio.wait_for(
        public_service.wait_for_terminal(response["id"]),
        timeout=1,
    )

    assert completed["status"] == "completed"
    assert completed["output"][0]["content"][0]["text"] == "Lock-safe answer"


@pytest.mark.asyncio
async def test_cancelling_response_waits_for_worker_shutdown(public_service, auth) -> None:
    runtime = BlockingRuntime()
    public_service.runtime = runtime
    conversation, _ = await public_service.create_conversation(
        auth,
        ConversationCreateRequest(agent_id="agent_general_assistant"),
        None,
    )
    response, _ = await public_service.create_response(
        auth,
        ResponseCreateRequest(
            conversation_id=conversation["id"],
            input="Keep working",
            background=True,
            stream=True,
        ),
        None,
    )
    await public_service.start_response(response["id"])
    await asyncio.wait_for(runtime.started.wait(), timeout=1)

    cancelled, _ = await public_service.cancel_response(
        auth, response["id"], "cancel-key"
    )

    assert cancelled["status"] == "cancelled"
    assert runtime.cancelled.is_set()
    assert response["id"] not in public_service._tasks


@pytest.mark.asyncio
async def test_conversation_and_response_history_is_paginated(public_service, auth) -> None:
    first, _ = await public_service.create_conversation(
        auth,
        ConversationCreateRequest(agent_id="agent_general_assistant"),
        None,
    )
    second, _ = await public_service.create_conversation(
        auth,
        ConversationCreateRequest(agent_id="agent_general_assistant"),
        None,
    )

    page = await public_service.list_conversations(auth, limit=1, after=None)
    assert page["object"] == "list"
    assert len(page["data"]) == 1
    assert page["has_more"] is True
    assert page["next_cursor"]

    next_page = await public_service.list_conversations(
        auth,
        limit=1,
        after=page["next_cursor"],
    )
    assert len(next_page["data"]) == 1
    assert {page["data"][0]["id"], next_page["data"][0]["id"]} == {
        first["id"],
        second["id"],
    }

    response, _ = await public_service.create_response(
        auth,
        ResponseCreateRequest(
            conversation_id=first["id"],
            input="What is my balance?",
            background=True,
        ),
        None,
    )
    await public_service.start_response(response["id"])
    await public_service.wait_for_terminal(response["id"])

    history = await public_service.list_conversation_responses(
        auth,
        first["id"],
        limit=100,
        after=None,
    )
    assert history["data"][0]["input"][0]["content"][0]["text"] == (
        "What is my balance?"
    )
    refreshed = await public_service.get_conversation(auth, first["id"])
    assert refreshed["metadata"]["title"] == "What is my balance?"

    with pytest.raises(ApiProblem) as invalid:
        await public_service.list_conversations(auth, limit=10, after="not-a-cursor")
    assert invalid.value.code == "invalid_cursor"


@pytest.mark.asyncio
async def test_required_action_approves_and_resumes_same_response(
    public_service, auth
) -> None:
    conversation, _ = await public_service.create_conversation(
        auth,
        ConversationCreateRequest(agent_id="agent_general_assistant"),
        None,
    )
    response, _ = await public_service.create_response(
        auth,
        ResponseCreateRequest(
            conversation_id=conversation["id"],
            input="Switch me to Flex 20",
            background=True,
        ),
        None,
    )
    await public_service.start_response(response["id"])
    waiting = await public_service.wait_for_terminal(response["id"])

    assert waiting["status"] == "requires_action"
    action = waiting["required_action"]
    resumed, replayed = await public_service.submit_action(
        auth,
        response["id"],
        action["id"],
        "approve",
        "approve-key",
    )
    assert not replayed
    assert resumed["id"] == response["id"]

    completed = await public_service.wait_for_terminal(response["id"])
    assert completed["status"] == "completed"
    assert completed["required_action"] is None
    assert completed["output"][0]["content"][0]["text"] == (
        "The plan change was submitted."
    )


@pytest.mark.asyncio
async def test_required_action_survives_service_restart(public_service, auth) -> None:
    response, _ = await public_service.create_response(
        auth,
        ResponseCreateRequest(
            agent_id="agent_general_assistant",
            input="Switch me to Flex 20",
            background=True,
        ),
        None,
    )
    await public_service.start_response(response["id"])
    waiting = await public_service.wait_for_terminal(response["id"])
    action = waiting["required_action"]

    await public_service.shutdown()
    restarted = AgentApiService(
        public_service.settings,
        public_service.database,
        FakeRuntime(),  # type: ignore[arg-type]
    )
    await restarted.startup()
    try:
        await restarted.submit_action(
            auth,
            response["id"],
            action["id"],
            "approve",
            "restart-approval",
        )
        completed = await restarted.wait_for_terminal(response["id"])
        assert completed["status"] == "completed"
        assert completed["output"][0]["content"][0]["text"] == (
            "The plan change was submitted."
        )
    finally:
        await restarted.shutdown()


@pytest.mark.asyncio
async def test_owner_isolation_and_idempotency_conflict(public_service, auth) -> None:
    conversation, _ = await public_service.create_conversation(
        auth,
        ConversationCreateRequest(agent_id="agent_general_assistant"),
        "same-key",
    )
    with pytest.raises(ApiProblem) as conflict:
        await public_service.create_conversation(
            auth,
            ConversationCreateRequest(
                agent_id="agent_general_assistant",
                metadata={"different": True},
            ),
            "same-key",
        )
    assert conflict.value.code == "idempotency_conflict"

    other = AuthContext(
        tenant_id=auth.tenant_id,
        subject_id="auth0|other",
        scopes=auth.scopes,
        claims={},
    )
    with pytest.raises(ApiProblem) as hidden:
        await public_service.get_conversation(other, conversation["id"])
    assert hidden.value.status_code == 404


@pytest.mark.asyncio
async def test_conversation_deletion_is_repeatable_without_a_key(
    public_service, auth
) -> None:
    conversation, _ = await public_service.create_conversation(
        auth,
        ConversationCreateRequest(agent_id="agent_general_assistant"),
        None,
    )

    assert not await public_service.delete_conversation(
        auth, conversation["id"], None
    )
    assert not await public_service.delete_conversation(
        auth, conversation["id"], None
    )
    with pytest.raises(ApiProblem) as missing:
        await public_service.get_conversation(auth, conversation["id"])
    assert missing.value.status_code == 404


def test_fingerprint_canonicalizes_object_keys_but_not_array_order() -> None:
    assert request_fingerprint({"b": 2, "a": 1}) == request_fingerprint(
        {"a": 1, "b": 2}
    )
    assert request_fingerprint({"items": [1, 2]}) != request_fingerprint(
        {"items": [2, 1]}
    )


@pytest.mark.asyncio
async def test_http_boundary_returns_problem_details_and_protocol_headers(
    tmp_path, auth
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'http-api.db'}"
    database = Database(database_url)
    await database.create_schema()
    await database.close()

    class StaticVerifier:
        async def verify(self, _token: str) -> AuthContext:
            return auth

    settings = Settings(
        database_url=database_url,
        api_enabled=True,
        auth0_domain="tenant.example.auth0.com",
        auth0_audience="https://voice-api.example.com",
    )
    app = create_agent_app(settings, token_verifier=StaticVerifier())
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            unauthorized = await client.post(
                "/v1/conversations",
                json={"agent_id": "agent_general_assistant"},
            )
            assert unauthorized.status_code == 401, unauthorized.json()
            assert unauthorized.headers["content-type"].startswith(
                "application/problem+json"
            )
            assert unauthorized.json()["code"] == "invalid_token"
            assert unauthorized.headers["x-request-id"].startswith("req_")

            created = await client.post(
                "/v1/conversations",
                headers={
                    "Authorization": "Bearer test-token",
                    "Idempotency-Key": "http-conversation",
                },
                json={"agent_id": "agent_general_assistant"},
            )
            assert created.status_code == 201
            assert created.headers["location"].startswith("/v1/conversations/conv_")
            assert created.headers["ratelimit-limit"] == "60"
            assert created.headers["x-request-id"].startswith("req_")

            authorized = {"Authorization": "Bearer test-token"}
            conversations = await client.get(
                "/v1/conversations?limit=30",
                headers=authorized,
            )
            assert conversations.status_code == 200
            assert conversations.json()["data"][0]["id"] == created.json()["id"]

            responses = await client.get(
                f"/v1/conversations/{created.json()['id']}/responses?limit=100",
                headers=authorized,
            )
            assert responses.status_code == 200
            assert responses.json() == {
                "object": "list",
                "data": [],
                "has_more": False,
                "next_cursor": None,
            }
