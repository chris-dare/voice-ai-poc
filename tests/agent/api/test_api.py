from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from voice_ai.agent.api.auth import AuthContext
from voice_ai.agent.api.models import ResponseJob, WorkerNode
from voice_ai.agent.api.router import SharedFixedWindowRateLimiter, create_api_router
from voice_ai.agent.api.schemas import (
    ConversationCreateRequest,
    ResponseCreateRequest,
)
from voice_ai.agent.api.services import (
    AgentApiService,
    ApiProblem,
    request_fingerprint,
)
from voice_ai.agent.app import capability_manifest, create_agent_app
from voice_ai.agent.models import ModelProbe
from voice_ai.agent.persistence.database import Database
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
        self.requests: list[AgentTurnRequest] = []

    async def stream_turn(self, request: AgentTurnRequest):
        self.requests.append(request)
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


class ConcurrencyRuntime(FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.maximum_active = 0
        self._counter_lock = asyncio.Lock()

    async def stream_turn(self, request: AgentTurnRequest):
        async with self._counter_lock:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            yield ResponseStarted(turn_id=request.turn_id)
            await asyncio.sleep(0.05)
            self.states[request.session_id] = {"turns": [request.text]}
            yield TextDelta(text=f"Answer: {request.text}")
            yield ResponseCompleted(turn_id=request.turn_id, latency_ms=50)
        finally:
            async with self._counter_lock:
                self.active -= 1


class OversizedRuntime(FakeRuntime):
    async def stream_turn(self, request: AgentTurnRequest):
        yield ResponseStarted(turn_id=request.turn_id)
        yield TextDelta(text="x" * 2_048)
        yield ResponseCompleted(turn_id=request.turn_id, latency_ms=1)


@pytest.fixture
async def public_service(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'public-api.db'}")
    # Importing the service above registers all public tables before schema creation.
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
        model="test:assistant",
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
    assert [event["sequence_number"] for event in events] == list(range(1, len(events) + 1))
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
async def test_response_execution_audit_is_immutable_safe_and_owner_scoped(
    public_service,
    auth,
) -> None:
    response, _ = await public_service.create_response(
        auth,
        ResponseCreateRequest(
            agent_id="agent_general_assistant",
            model="test:assistant",
            input="Explain the execution route",
            background=True,
        ),
        None,
    )
    await public_service.start_response(response["id"])
    completed = await public_service.wait_for_terminal(response["id"])
    audit = await public_service.get_response_execution(auth, response["id"])

    assert "execution_snapshot" not in completed
    assert audit["object"] == "response.execution"
    assert audit["status"] == "completed"
    assert audit["schema_version"] == "1"
    assert audit["agent_definition"]["id"] == "agent_general_assistant"
    assert audit["agent_definition"]["version"].startswith("sha256:")
    assert audit["model_route"]["selected_model_id"] == "test:assistant"
    assert audit["model_route"]["settings"]["thinking_effort"] == "low"
    assert "web_research" in audit["capability_set"]
    assert audit["limit_policy"]["limits"]["max_model_requests"] == 16
    assert audit["policy_version"].startswith("sha256:")
    assert audit["model_execution"]["attempt_count"] == 0
    assert "system_prompt" not in str(audit)

    class StaticVerifier:
        async def verify(self, _token: str) -> AuthContext:
            return auth

    app = FastAPI()
    app.include_router(
        create_api_router(public_service, StaticVerifier(), requests_per_minute=1_000)
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        retrieved = await client.get(
            f"/v1/responses/{response['id']}/execution",
            headers={"Authorization": "Bearer test-token"},
        )
    assert retrieved.status_code == 200
    assert retrieved.json()["agent_definition"] == audit["agent_definition"]

    original_definition_version = audit["agent_definition"]["version"]
    public_service.settings.agent_request_limit = 2
    unchanged = await public_service.get_response_execution(auth, response["id"])
    assert unchanged["agent_definition"]["version"] == original_definition_version
    assert unchanged["limit_policy"]["limits"]["max_model_requests"] == 16

    other = AuthContext(
        tenant_id="tenant_2",
        subject_id="auth0|user_2",
        scopes=auth.scopes,
        claims={},
    )
    with pytest.raises(ApiProblem) as hidden:
        await public_service.get_response_execution(other, response["id"])
    assert hidden.value.status_code == 404


@pytest.mark.asyncio
async def test_model_catalog_and_per_response_selection(public_service, auth, monkeypatch) -> None:
    public_service.settings.agent_models = ["test:alternate"]

    async def probe(_settings, model_id: str) -> ModelProbe:
        return ModelProbe("available", None, f"{model_id} ready")

    monkeypatch.setattr(
        "voice_ai.agent.api.services.api.probe_model_availability",
        probe,
    )
    catalog = await public_service.list_models()
    assert [(item["id"], item["status"]) for item in catalog["data"]] == [
        ("test:assistant", "available"),
        ("test:alternate", "available"),
    ]

    conversation, _ = await public_service.create_conversation(
        auth,
        ConversationCreateRequest(
            agent_id="agent_general_assistant",
            model="test:assistant",
        ),
        None,
    )
    response, _ = await public_service.create_response(
        auth,
        ResponseCreateRequest(
            conversation_id=conversation["id"],
            model="test:alternate",
            input="Use the selected route",
            background=True,
        ),
        None,
    )
    completed = await public_service.wait_for_terminal(response["id"])

    refreshed = await public_service.get_conversation(auth, conversation["id"])
    assert conversation["model"] == "test:assistant"
    assert refreshed["model"] == "test:alternate"
    assert completed["model"] == "test:alternate"
    assert public_service.runtime.requests[-1].model_id == "test:alternate"


def test_response_requires_an_explicit_model() -> None:
    with pytest.raises(ValueError):
        ResponseCreateRequest(
            agent_id="agent_general_assistant",
            input="Do not infer a route",
        )


@pytest.mark.asyncio
async def test_recent_unavailable_model_is_rejected_before_queueing(public_service, auth) -> None:
    await public_service._record_model_availability(
        "test:assistant",
        status="unavailable",
        reason_code="provider_account_unavailable",
        detail="Provider access unavailable",
    )

    with pytest.raises(ApiProblem) as unavailable:
        await public_service.create_conversation(
            auth,
            ConversationCreateRequest(agent_id="agent_general_assistant"),
            None,
        )

    assert unavailable.value.code == "model_unavailable"
    assert unavailable.value.extensions["model"] == "test:assistant"


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
            model="test:assistant",
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
            model="test:assistant",
            input="Keep working",
            background=True,
            stream=True,
        ),
        None,
    )
    await public_service.start_response(response["id"])
    await asyncio.wait_for(runtime.started.wait(), timeout=1)

    cancelled, _ = await public_service.cancel_response(auth, response["id"], "cancel-key")

    assert cancelled["status"] == "cancelled"
    assert cancelled["usage"]["latency"]["completion_ms"] >= 0
    assert cancelled["usage"]["slo"]["met"]["completion"] is True
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
            model="test:assistant",
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
    assert history["data"][0]["input"][0]["content"][0]["text"] == ("What is my balance?")
    refreshed = await public_service.get_conversation(auth, first["id"])
    assert refreshed["metadata"]["title"] == "What is my balance?"

    with pytest.raises(ApiProblem) as invalid:
        await public_service.list_conversations(auth, limit=10, after="not-a-cursor")
    assert invalid.value.code == "invalid_cursor"


@pytest.mark.asyncio
async def test_required_action_approves_and_resumes_same_response(public_service, auth) -> None:
    conversation, _ = await public_service.create_conversation(
        auth,
        ConversationCreateRequest(agent_id="agent_general_assistant"),
        None,
    )
    response, _ = await public_service.create_response(
        auth,
        ResponseCreateRequest(
            conversation_id=conversation["id"],
            model="test:assistant",
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
    assert completed["output"][0]["content"][0]["text"] == ("The plan change was submitted.")


@pytest.mark.asyncio
async def test_required_action_survives_service_restart(public_service, auth) -> None:
    response, _ = await public_service.create_response(
        auth,
        ResponseCreateRequest(
            agent_id="agent_general_assistant",
            model="test:assistant",
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
        assert completed["output"][0]["content"][0]["text"] == ("The plan change was submitted.")
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
async def test_conversation_deletion_is_repeatable_without_a_key(public_service, auth) -> None:
    conversation, _ = await public_service.create_conversation(
        auth,
        ConversationCreateRequest(agent_id="agent_general_assistant"),
        None,
    )

    assert not await public_service.delete_conversation(auth, conversation["id"], None)
    assert not await public_service.delete_conversation(auth, conversation["id"], None)
    with pytest.raises(ApiProblem) as missing:
        await public_service.get_conversation(auth, conversation["id"])
    assert missing.value.status_code == 404


def test_fingerprint_canonicalizes_object_keys_but_not_array_order() -> None:
    assert request_fingerprint({"b": 2, "a": 1}) == request_fingerprint({"a": 1, "b": 2})
    assert request_fingerprint({"items": [1, 2]}) != request_fingerprint({"items": [2, 1]})


@pytest.mark.asyncio
async def test_http_boundary_returns_problem_details_and_protocol_headers(tmp_path, auth) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'http-api.db'}"
    database = Database(database_url)
    await database.create_schema()
    await database.close()

    class StaticVerifier:
        async def verify(self, _token: str) -> AuthContext:
            return auth

    settings = Settings(
        database_url=database_url,
        agent_model="ollama:test-model",
        api_enabled=True,
        auth0_domain="tenant.example.auth0.com",
        auth0_audience="https://voice-api.example.com",
        api_max_request_body_bytes=1_024,
    )
    app = create_agent_app(settings, token_verifier=StaticVerifier())
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:

            async def oversized_body():
                yield b"{" + (b'"padding":"' + (b"x" * 1_024))
                yield b'"}'

            oversized = await client.post(
                "/v1/conversations",
                headers={"Content-Type": "application/json"},
                content=oversized_body(),
            )
            assert oversized.status_code == 413
            assert oversized.json()["code"] == "request_body_too_large"
            assert oversized.json()["request_id"] == oversized.headers["x-request-id"]

            unauthorized = await client.post(
                "/v1/conversations",
                json={"agent_id": "agent_general_assistant"},
            )
            assert unauthorized.status_code == 401, unauthorized.json()
            assert unauthorized.headers["content-type"].startswith("application/problem+json")
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


@pytest.mark.asyncio
async def test_accepted_response_survives_api_restart(tmp_path, auth) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'restart.db'}")
    await database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        api_enabled=True,
        auth0_domain="tenant.example.auth0.com",
        auth0_audience="https://voice-api.example.com",
        agent_embedded_worker=False,
        agent_job_poll_seconds=0.05,
    )
    first = AgentApiService(settings, database, FakeRuntime())  # type: ignore[arg-type]
    await first.startup()
    response, _ = await first.create_response(
        auth,
        ResponseCreateRequest(
            agent_id="agent_general_assistant",
            model="test:assistant",
            input="Persist this work",
            background=True,
        ),
        "restart-safe",
    )
    await first.shutdown()

    restarted = AgentApiService(
        settings,
        database,
        FakeRuntime(),  # type: ignore[arg-type]
    )
    await restarted.startup(start_worker=True)
    try:
        completed = await asyncio.wait_for(restarted.wait_for_terminal(response["id"]), timeout=2)
        assert completed["status"] == "completed"
        assert completed["output"][0]["content"][0]["text"] == ("Answer: Persist this work")
    finally:
        await restarted.shutdown()
        await database.close()


@pytest.mark.asyncio
async def test_expired_worker_lease_is_reclaimed(tmp_path, auth) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'lease.db'}")
    await database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        api_enabled=True,
        auth0_domain="tenant.example.auth0.com",
        auth0_audience="https://voice-api.example.com",
        agent_embedded_worker=False,
        agent_job_poll_seconds=0.05,
    )
    abandoned = AgentApiService(
        settings,
        database,
        FakeRuntime(),  # type: ignore[arg-type]
    )
    await abandoned.startup()
    response, _ = await abandoned.create_response(
        auth,
        ResponseCreateRequest(
            agent_id="agent_general_assistant",
            model="test:assistant",
            input="Recover me",
            background=True,
        ),
        None,
    )
    assert await abandoned._claim_response_job() == (response["id"], None, False)
    async with database.session_factory.begin() as session:
        job = await session.get(ResponseJob, response["id"], with_for_update=True)
        assert job is not None
        job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await abandoned.shutdown()

    recovered = AgentApiService(
        settings,
        database,
        FakeRuntime(),  # type: ignore[arg-type]
    )
    await recovered.startup(start_worker=True)
    try:
        completed = await asyncio.wait_for(recovered.wait_for_terminal(response["id"]), timeout=2)
        assert completed["status"] == "completed"
        async with database.session() as session:
            job = await session.get(ResponseJob, response["id"])
            assert job is not None
            assert job.attempt_count == 2
            assert job.status == "completed"
    finally:
        await recovered.shutdown()
        await database.close()


@pytest.mark.asyncio
async def test_tool_and_recovery_metrics_are_emitted_without_tenant_identifiers(
    tmp_path, auth, monkeypatch
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'metrics.db'}")
    await database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        agent_embedded_worker=False,
        agent_job_poll_seconds=0.05,
    )
    tool_metric = Mock()
    recovery_metric = Mock()
    monkeypatch.setattr("voice_ai.agent.api.services.api.record_agent_tool", tool_metric)
    monkeypatch.setattr(
        "voice_ai.agent.api.services.api.record_agent_worker_recovery",
        recovery_metric,
    )
    service = AgentApiService(settings, database, FakeRuntime())  # type: ignore[arg-type]
    await service.startup()
    try:
        response, _ = await service.create_response(
            auth,
            ResponseCreateRequest(
                agent_id="agent_general_assistant",
                model="test:assistant",
                input="Check my balance",
                background=True,
            ),
            None,
        )
        assert await service._claim_response_job() == (response["id"], None, False)
        async with database.session_factory.begin() as session:
            job = await session.get(ResponseJob, response["id"], with_for_update=True)
            assert job is not None
            job.lease_expires_at = datetime.now(UTC) - timedelta(milliseconds=25)

        assert await service._claim_response_job() == (response["id"], None, True)
        await service._execute_claimed_job(response["id"], None)

        recovery_metric.assert_called_once()
        assert recovery_metric.call_args.kwargs["delay_ms"] >= 0
        assert tool_metric.call_args.kwargs == {
            "duration_ms": tool_metric.call_args.kwargs["duration_ms"],
            "tool": "get_account_balance",
            "source": "root",
            "status": "succeeded",
        }
        assert tool_metric.call_args.kwargs["duration_ms"] >= 0
        assert auth.tenant_id not in repr(tool_metric.call_args)
        assert auth.subject_id not in repr(tool_metric.call_args)
    finally:
        await service.shutdown()
        await database.close()


@pytest.mark.asyncio
async def test_rate_limit_is_shared_across_api_replicas(tmp_path, auth) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'rate-limit.db'}")
    await database.create_schema()
    settings = Settings(database_url=str(database.engine.url))
    first_service = AgentApiService(
        settings,
        database,
        FakeRuntime(),  # type: ignore[arg-type]
    )
    second_service = AgentApiService(
        settings,
        database,
        FakeRuntime(),  # type: ignore[arg-type]
    )
    first = SharedFixedWindowRateLimiter(first_service, 2)
    second = SharedFixedWindowRateLimiter(second_service, 2)
    await first.check(auth)
    headers = await second.check(auth)
    assert headers["RateLimit-Remaining"] == "0"
    with pytest.raises(ApiProblem) as limited:
        await first.check(auth)
    assert limited.value.code == "rate_limit_exceeded"
    await database.close()


@pytest.mark.asyncio
async def test_response_queue_applies_bounded_backpressure(tmp_path, auth) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'capacity.db'}")
    await database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        agent_embedded_worker=False,
        agent_queue_capacity=1,
    )
    service = AgentApiService(settings, database, FakeRuntime())  # type: ignore[arg-type]
    await service.startup()
    try:
        await service.create_response(
            auth,
            ResponseCreateRequest(
                agent_id="agent_general_assistant",
                model="test:assistant",
                input="First",
                background=True,
            ),
            None,
        )
        with pytest.raises(ApiProblem) as full:
            await service.create_response(
                auth,
                ResponseCreateRequest(
                    agent_id="agent_general_assistant",
                    model="test:assistant",
                    input="Second",
                    background=True,
                ),
                None,
            )
        assert full.value.code == "capacity_exhausted"
    finally:
        await service.shutdown()
        await database.close()


def test_capability_manifest_is_generated_from_effective_limits() -> None:
    manifest = capability_manifest(
        Settings(
            agent_worker_concurrency=7,
            agent_model_route_concurrency=5,
            agent_tool_route_concurrency=11,
            agent_queue_capacity=42,
            agent_tenant_active_response_limit=3,
            agent_stream_buffer_capacity=17,
            agent_max_output_bytes=65_536,
            agent_deep_agents_enabled=False,
        )
    )

    assert manifest["profiles"] == ["core"]
    assert "specialist_delegation" not in manifest["optional_features"]
    assert manifest["limits"]["max_concurrent_work"] == 7
    assert manifest["limits"]["max_concurrent_model_requests_per_route"] == 5
    assert manifest["limits"]["max_concurrent_tool_calls_per_route"] == 11
    assert manifest["limits"]["max_queued_work"] == 42
    assert manifest["limits"]["max_active_responses_per_tenant"] == 3
    assert manifest["limits"]["max_stream_buffer_events"] == 17
    assert manifest["limits"]["max_output_bytes"] == 65_536


@pytest.mark.asyncio
async def test_response_output_size_is_enforced_by_server_code(tmp_path, auth) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'output-limit.db'}")
    await database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        agent_max_output_bytes=1_024,
        agent_job_poll_seconds=0.05,
    )
    service = AgentApiService(
        settings,
        database,
        OversizedRuntime(),  # type: ignore[arg-type]
    )
    await service.startup()
    try:
        response, _ = await service.create_response(
            auth,
            ResponseCreateRequest(
                agent_id="agent_general_assistant",
                model="test:assistant",
                input="Bound this output",
                background=True,
            ),
            None,
        )
        failed = await asyncio.wait_for(service.wait_for_terminal(response["id"]), timeout=2)

        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "output_limit_exceeded"
        assert not failed["error"]["retryable"]
    finally:
        await service.shutdown()
        await database.close()


@pytest.mark.asyncio
async def test_tenant_admission_is_bounded_without_blocking_other_tenants(tmp_path, auth) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'tenant-capacity.db'}")
    await database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        agent_embedded_worker=False,
        agent_queue_capacity=10,
        agent_tenant_active_response_limit=1,
    )
    service = AgentApiService(settings, database, FakeRuntime())  # type: ignore[arg-type]
    await service.startup()
    other_tenant = AuthContext(
        tenant_id="tenant_2",
        subject_id=auth.subject_id,
        scopes=auth.scopes,
        claims={},
    )
    try:
        await service.create_response(
            auth,
            ResponseCreateRequest(
                agent_id="agent_general_assistant",
                model="test:assistant",
                input="First tenant request",
                background=True,
            ),
            None,
        )
        with pytest.raises(ApiProblem) as exhausted:
            await service.create_response(
                auth,
                ResponseCreateRequest(
                    agent_id="agent_general_assistant",
                    model="test:assistant",
                    input="Second tenant request",
                    background=True,
                ),
                None,
            )
        assert exhausted.value.status_code == 429
        assert exhausted.value.code == "tenant_capacity_exhausted"

        accepted, _ = await service.create_response(
            other_tenant,
            ResponseCreateRequest(
                agent_id="agent_general_assistant",
                model="test:assistant",
                input="Another tenant remains isolated",
                background=True,
            ),
            None,
        )
        assert accepted["status"] == "queued"
    finally:
        await service.shutdown()
        await database.close()


@pytest.mark.asyncio
async def test_completed_jobs_release_process_local_session_and_lock_caches(tmp_path, auth) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cache-cleanup.db'}")
    await database.create_schema()
    settings = Settings(database_url=str(database.engine.url), agent_job_poll_seconds=0.05)
    runtime = FakeRuntime()
    service = AgentApiService(settings, database, runtime)  # type: ignore[arg-type]
    await service.startup()
    try:
        response, _ = await service.create_response(
            auth,
            ResponseCreateRequest(
                agent_id="agent_general_assistant",
                model="test:assistant",
                input="Release local state",
                background=True,
            ),
            None,
        )
        completed = await asyncio.wait_for(service.wait_for_terminal(response["id"]), timeout=2)

        assert completed["status"] == "completed"
        assert runtime.states == {}
        assert service._response_locks == {}
    finally:
        await service.shutdown()
        await database.close()


@pytest.mark.asyncio
async def test_public_api_readiness_requires_a_live_response_worker(
    tmp_path, auth, monkeypatch
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'worker-readiness.db'}"
    database = Database(database_url)
    await database.create_schema()
    await database.close()

    class ReadyRuntime(FakeRuntime):
        def __init__(self, _settings, **_kwargs) -> None:
            super().__init__()

        async def startup(self) -> None:
            pass

        async def shutdown(self) -> None:
            pass

        async def model_readiness(self) -> dict:
            return {
                "ready": True,
                "model": "test:assistant",
                "provider": "test",
                "detail": "ready",
                "fallbacks": [],
            }

    class StaticVerifier:
        async def verify(self, _token: str) -> AuthContext:
            return auth

    monkeypatch.setattr("voice_ai.agent.app.AgentRuntime", ReadyRuntime)
    app = create_agent_app(
        Settings(
            database_url=database_url,
            agent_model="test:assistant",
            api_enabled=True,
            auth0_domain="tenant.example.auth0.com",
            auth0_audience="https://voice-api.example.com",
            agent_embedded_worker=False,
        ),
        token_verifier=StaticVerifier(),
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["response_workers"]["ready"] is False


@pytest.mark.asyncio
async def test_worker_reconciles_stale_presence_and_marks_itself_stopped(tmp_path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'worker-presence.db'}")
    await database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        agent_worker_presence_ttl_seconds=5,
        agent_job_poll_seconds=0.05,
    )
    stale_heartbeat = datetime.now(UTC) - timedelta(minutes=1)
    async with database.session_factory.begin() as session:
        session.add(
            WorkerNode(
                id="stale-worker",
                status="active",
                concurrency=4,
                started_at=stale_heartbeat,
                heartbeat_at=stale_heartbeat,
            )
        )

    service = AgentApiService(settings, database, FakeRuntime())  # type: ignore[arg-type]
    worker = asyncio.create_task(service.run_worker_forever())
    try:
        for _attempt in range(20):
            async with database.session() as session:
                current = await session.get(WorkerNode, service._worker_id)
            if current is not None:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Worker presence was not registered")

        async with database.session() as session:
            stale = await session.get(WorkerNode, "stale-worker")
        assert stale is not None
        assert stale.status == "stopped"
        assert current.status == "active"
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    async with database.session() as session:
        stopped = await session.get(WorkerNode, service._worker_id)
    assert stopped is not None
    assert stopped.status == "stopped"
    await database.close()


@pytest.mark.asyncio
async def test_cancellation_reaches_a_different_worker_replica(tmp_path, auth) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'remote-cancel.db'}")
    await database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        agent_embedded_worker=False,
        agent_job_poll_seconds=0.05,
        agent_cancellation_poll_seconds=0.1,
    )
    api = AgentApiService(settings, database, FakeRuntime())  # type: ignore[arg-type]
    blocking_runtime = BlockingRuntime()
    worker = AgentApiService(
        settings,
        database,
        blocking_runtime,  # type: ignore[arg-type]
    )
    await api.startup(start_worker=False)
    await worker.startup(start_worker=True)
    try:
        response, _ = await api.create_response(
            auth,
            ResponseCreateRequest(
                agent_id="agent_general_assistant",
                model="test:assistant",
                input="Keep working remotely",
                background=True,
            ),
            None,
        )
        await api.start_response(response["id"])
        await asyncio.wait_for(blocking_runtime.started.wait(), timeout=1)
        cancelled, _ = await api.cancel_response(auth, response["id"], None)
        await asyncio.wait_for(blocking_runtime.cancelled.wait(), timeout=1)
        assert cancelled["status"] == "cancelled"
        assert api.runtime.states == {}
    finally:
        await worker.shutdown()
        await api.shutdown()
        await database.close()


@pytest.mark.asyncio
async def test_sse_observes_events_written_by_another_replica(tmp_path, auth) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cross-replica-events.db'}")
    await database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        agent_embedded_worker=False,
        api_event_poll_seconds=0.05,
    )
    reader = AgentApiService(settings, database, FakeRuntime())  # type: ignore[arg-type]
    writer = AgentApiService(settings, database, FakeRuntime())  # type: ignore[arg-type]
    await reader.startup(start_worker=False)
    await writer.startup(start_worker=False)
    try:
        response, _ = await writer.create_response(
            auth,
            ResponseCreateRequest(
                agent_id="agent_general_assistant",
                model="test:assistant",
                input="Stream this",
                background=True,
            ),
            None,
        )
        stream = reader.stream_events(auth, response["id"], 1)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.1)
        await writer._append_event(
            response["id"],
            "response.tool.started",
            tool_run_id="toolrun_cross_replica",
            name="test_tool",
            label="Testing",
        )
        record = await asyncio.wait_for(pending, timeout=0.5)
        assert "event: response.tool.started" in record
        await stream.aclose()
    finally:
        await writer.shutdown()
        await reader.shutdown()
        await database.close()


@pytest.mark.asyncio
async def test_worker_concurrency_is_bounded_under_parallel_load(tmp_path, auth) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'parallel-load.db'}")
    await database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        agent_worker_concurrency=3,
        agent_job_poll_seconds=0.05,
    )
    runtime = ConcurrencyRuntime()
    service = AgentApiService(settings, database, runtime)  # type: ignore[arg-type]
    await service.startup()
    try:
        responses = []
        for index in range(12):
            response, _ = await service.create_response(
                auth,
                ResponseCreateRequest(
                    agent_id="agent_general_assistant",
                    model="test:assistant",
                    input=f"Request {index}",
                    background=True,
                ),
                None,
            )
            responses.append(response)
        completed = await asyncio.gather(
            *(service.wait_for_terminal(response["id"]) for response in responses)
        )
        assert all(response["status"] == "completed" for response in completed)
        assert 1 < runtime.maximum_active <= 3
    finally:
        await service.shutdown()
        await database.close()


@pytest.mark.asyncio
async def test_execution_timeout_produces_durable_terminal_failure(tmp_path, auth) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'execution-timeout.db'}")
    await database.create_schema()
    settings = Settings(
        database_url=str(database.engine.url),
        agent_execution_timeout_seconds=1,
        agent_job_poll_seconds=0.05,
    )
    service = AgentApiService(
        settings,
        database,
        BlockingRuntime(),  # type: ignore[arg-type]
    )
    await service.startup()
    try:
        response, _ = await service.create_response(
            auth,
            ResponseCreateRequest(
                agent_id="agent_general_assistant",
                model="test:assistant",
                input="Never finish",
                background=True,
            ),
            None,
        )
        failed = await asyncio.wait_for(service.wait_for_terminal(response["id"]), timeout=2)
        assert failed["status"] == "failed"
        assert failed["error"] == {
            "code": "execution_timeout",
            "message": "The response exceeded its maximum execution time.",
            "retryable": True,
        }
    finally:
        await service.shutdown()
        await database.close()


@pytest.mark.asyncio
async def test_failed_worker_tasks_are_observed_and_removed(public_service, monkeypatch) -> None:
    metric = Mock()
    monkeypatch.setattr("voice_ai.agent.api.services.api.record_agent_job_event", metric)

    async def fail() -> None:
        raise RuntimeError("simulated worker failure")

    task = asyncio.create_task(fail())
    public_service._tasks["resp_failed_task"] = task
    await asyncio.sleep(0)

    public_service._response_task_finished("resp_failed_task", task)

    assert "resp_failed_task" not in public_service._tasks
    metric.assert_called_once_with(event="task_failed", attempt=0)
