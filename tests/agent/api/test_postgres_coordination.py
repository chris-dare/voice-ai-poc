from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlmodel import select

from voice_ai.agent.api.auth import AuthContext
from voice_ai.agent.api.models import ExecutionCapacityLease, ResponseJob
from voice_ai.agent.api.schemas import ResponseCreateRequest
from voice_ai.agent.api.services import AgentApiService, ApiProblem
from voice_ai.agent.capacity import DistributedExecutionCapacity, ExecutionCapacityError
from voice_ai.agent.persistence.database import Database
from voice_ai.agent.persistence.model import TableModel
from voice_ai.agent.protocol import AgentTurnRequest, ResponseCompleted, ResponseStarted, TextDelta
from voice_ai.shared.config import Settings


class FakeRuntime:
    def __init__(self) -> None:
        self.states: dict[UUID, dict] = {}
        self.requests: list[AgentTurnRequest] = []

    async def stream_turn(self, request: AgentTurnRequest):
        self.requests.append(request)
        yield ResponseStarted(turn_id=request.turn_id)
        self.states[request.session_id] = {"turns": [request.text]}
        yield TextDelta(text=f"Answer: {request.text}")
        yield ResponseCompleted(turn_id=request.turn_id, latency_ms=1)

    async def restore_session_state(self, session_id: UUID, state: dict | None) -> None:
        if state:
            self.states[session_id] = state

    async def export_session_state(self, session_id: UUID) -> dict:
        return self.states.get(session_id, {})

    async def pending_confirmation(self, _session_id: UUID) -> None:
        return None

    async def close_session(self, session_id: UUID) -> None:
        self.states.pop(session_id, None)


class ConcurrencyRuntime(FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.maximum_active = 0
        self._counter_lock = asyncio.Lock()

    async def stream_turn(self, request: AgentTurnRequest):
        self.requests.append(request)
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


class LeaseAwareBlockingRuntime(FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def stream_turn(self, request: AgentTurnRequest):
        self.requests.append(request)
        yield ResponseStarted(turn_id=request.turn_id)
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()


@pytest.fixture
async def coordination_database() -> Database:
    url = os.getenv("TEST_DATABASE_URL")
    if not url or "test" not in url.lower():
        pytest.skip("Set an isolated TEST_DATABASE_URL containing 'test' for coordination tests")
    database = Database(url)
    # Use the same central model-registration path as the application before
    # resetting the isolated schema. Direct metadata access is import-order dependent.
    await database.create_schema()
    async with database.engine.begin() as connection:
        await connection.run_sync(TableModel.metadata.drop_all)
        await connection.run_sync(TableModel.metadata.create_all)
    try:
        yield database
    finally:
        async with database.engine.begin() as connection:
            await connection.run_sync(TableModel.metadata.drop_all)
        await database.close()


@pytest.fixture
def coordination_auth() -> AuthContext:
    return AuthContext(
        tenant_id="tenant_coordination",
        subject_id="auth0|coordination-user",
        scopes=frozenset({"agents:invoke", "responses:read", "responses:cancel"}),
        claims={},
    )


def _payload(index: int) -> ResponseCreateRequest:
    return ResponseCreateRequest(
        agent_id="agent_general_assistant",
        model="test:assistant",
        input=f"Distributed request {index}",
        background=True,
    )


@pytest.mark.asyncio
async def test_postgres_serializes_tenant_admission_across_api_replicas(
    coordination_database: Database,
    coordination_auth: AuthContext,
) -> None:
    settings = Settings(
        database_url=str(coordination_database.engine.url),
        agent_embedded_worker=False,
        agent_queue_capacity=10,
        agent_tenant_active_response_limit=1,
    )
    first = AgentApiService(settings, coordination_database, FakeRuntime())  # type: ignore[arg-type]
    second = AgentApiService(settings, coordination_database, FakeRuntime())  # type: ignore[arg-type]
    await first.startup(start_worker=False)
    await second.startup(start_worker=False)
    try:
        results = await asyncio.gather(
            first.create_response(coordination_auth, _payload(1), None),
            second.create_response(coordination_auth, _payload(2), None),
            return_exceptions=True,
        )

        accepted = [result for result in results if not isinstance(result, BaseException)]
        rejected = [result for result in results if isinstance(result, ApiProblem)]
        assert len(accepted) == 1
        assert len(rejected) == 1
        assert rejected[0].code == "tenant_capacity_exhausted"
    finally:
        await second.shutdown()
        await first.shutdown()


@pytest.mark.asyncio
async def test_postgres_job_claim_has_one_owner_across_workers(
    coordination_database: Database,
    coordination_auth: AuthContext,
) -> None:
    settings = Settings(
        database_url=str(coordination_database.engine.url),
        agent_embedded_worker=False,
    )
    first = AgentApiService(settings, coordination_database, FakeRuntime())  # type: ignore[arg-type]
    second = AgentApiService(settings, coordination_database, FakeRuntime())  # type: ignore[arg-type]
    await first.startup(start_worker=False)
    await second.startup(start_worker=False)
    try:
        response, _ = await first.create_response(coordination_auth, _payload(1), None)
        claims = await asyncio.gather(
            first._claim_response_job(),
            second._claim_response_job(),
        )

        assert claims.count(None) == 1
        assert [claim for claim in claims if claim is not None] == [(response["id"], None, False)]
    finally:
        await second.shutdown()
        await first.shutdown()


@pytest.mark.asyncio
async def test_postgres_expired_lease_moves_to_another_worker(
    coordination_database: Database,
    coordination_auth: AuthContext,
) -> None:
    settings = Settings(
        database_url=str(coordination_database.engine.url),
        agent_embedded_worker=False,
    )
    failed_worker = AgentApiService(
        settings,
        coordination_database,
        FakeRuntime(),  # type: ignore[arg-type]
    )
    replacement_worker = AgentApiService(
        settings,
        coordination_database,
        FakeRuntime(),  # type: ignore[arg-type]
    )
    await failed_worker.startup(start_worker=False)
    await replacement_worker.startup(start_worker=False)
    try:
        response, _ = await failed_worker.create_response(
            coordination_auth,
            _payload(1),
            None,
        )
        assert await failed_worker._claim_response_job() == (response["id"], None, False)
        async with coordination_database.session_factory.begin() as session:
            job = await session.get(ResponseJob, response["id"], with_for_update=True)
            assert job is not None
            original_owner = job.lease_owner
            job.lease_expires_at = datetime.now(UTC) - timedelta(milliseconds=10)

        assert await replacement_worker._claim_response_job() == (response["id"], None, True)
        async with coordination_database.session() as session:
            recovered = await session.get(ResponseJob, response["id"])

        assert recovered is not None
        assert recovered.lease_owner != original_owner
        assert recovered.attempt_count == 2
    finally:
        await replacement_worker.shutdown()
        await failed_worker.shutdown()


@pytest.mark.asyncio
async def test_recovered_core_job_does_not_replay_published_tool_work(
    coordination_database: Database,
    coordination_auth: AuthContext,
) -> None:
    settings = Settings(
        database_url=str(coordination_database.engine.url),
        agent_embedded_worker=False,
    )
    failed_runtime = FakeRuntime()
    replacement_runtime = FakeRuntime()
    failed_worker = AgentApiService(
        settings,
        coordination_database,
        failed_runtime,  # type: ignore[arg-type]
    )
    replacement_worker = AgentApiService(
        settings,
        coordination_database,
        replacement_runtime,  # type: ignore[arg-type]
    )
    await failed_worker.startup(start_worker=False)
    await replacement_worker.startup(start_worker=False)
    try:
        response, _ = await failed_worker.create_response(
            coordination_auth,
            _payload(1),
            None,
        )
        assert await failed_worker._claim_response_job() == (response["id"], None, False)
        await failed_worker._mark_in_progress(response["id"])
        await failed_worker._append_event(
            response["id"],
            "response.tool.started",
            tool_run_id="toolrun_before_crash",
            name="web_fetch",
            label="Opening source",
        )
        async with coordination_database.session_factory.begin() as session:
            job = await session.get(ResponseJob, response["id"], with_for_update=True)
            assert job is not None
            job.lease_expires_at = datetime.now(UTC) - timedelta(milliseconds=10)

        claim = await replacement_worker._claim_response_job()
        assert claim == (response["id"], None, True)
        await replacement_worker._execute_claimed_job(
            response["id"],
            None,
            recovered=True,
        )
        failed = await replacement_worker.get_response(coordination_auth, response["id"])

        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "execution_interrupted"
        assert failed["error"]["retryable"] is True
        assert replacement_runtime.requests == []
    finally:
        await replacement_worker.shutdown()
        await failed_worker.shutdown()


@pytest.mark.asyncio
async def test_postgres_workers_share_parallel_load_with_per_replica_bounds(
    coordination_database: Database,
    coordination_auth: AuthContext,
) -> None:
    settings = Settings(
        database_url=str(coordination_database.engine.url),
        agent_embedded_worker=False,
        agent_worker_concurrency=2,
        agent_job_poll_seconds=0.05,
        agent_tenant_active_response_limit=20,
    )
    api = AgentApiService(settings, coordination_database, FakeRuntime())  # type: ignore[arg-type]
    first_runtime = ConcurrencyRuntime()
    second_runtime = ConcurrencyRuntime()
    first_worker = AgentApiService(
        settings,
        coordination_database,
        first_runtime,  # type: ignore[arg-type]
    )
    second_worker = AgentApiService(
        settings,
        coordination_database,
        second_runtime,  # type: ignore[arg-type]
    )
    await api.startup(start_worker=False)
    await first_worker.startup(start_worker=True)
    await second_worker.startup(start_worker=True)
    try:
        responses = [
            (await api.create_response(coordination_auth, _payload(index), None))[0]
            for index in range(8)
        ]
        completed = await asyncio.gather(
            *(api.wait_for_terminal(response["id"]) for response in responses)
        )

        assert all(response["status"] == "completed" for response in completed)
        assert first_runtime.requests
        assert second_runtime.requests
        assert first_runtime.maximum_active <= 2
        assert second_runtime.maximum_active <= 2
        assert first_runtime.maximum_active + second_runtime.maximum_active <= 4
    finally:
        await second_worker.shutdown()
        await first_worker.shutdown()
        await api.shutdown()


@pytest.mark.asyncio
async def test_postgres_capacity_is_shared_across_worker_replicas(
    coordination_database: Database,
) -> None:
    settings = Settings(
        database_url=str(coordination_database.engine.url),
        agent_model_route_concurrency=1,
        agent_capacity_wait_seconds=0.15,
    )
    first = DistributedExecutionCapacity(coordination_database, settings)
    second = DistributedExecutionCapacity(coordination_database, settings)
    first_acquired = asyncio.Event()
    release_first = asyncio.Event()

    async def hold_first() -> None:
        async with first.reserve(
            resource_kind="model",
            resource_key="openrouter:test/model",
            owner_id="worker-one",
        ):
            first_acquired.set()
            await release_first.wait()

    holder = asyncio.create_task(hold_first())
    await first_acquired.wait()
    try:
        with pytest.raises(ExecutionCapacityError) as error:
            async with second.reserve(
                resource_kind="model",
                resource_key="openrouter:test/model",
                owner_id="worker-two",
            ):
                pytest.fail("A second replica exceeded the shared route limit")
        assert error.value.code == "model_capacity_exhausted"

        async with second.reserve(
            resource_kind="model",
            resource_key="openrouter:another/model",
            owner_id="worker-two",
        ):
            pass
    finally:
        release_first.set()
        await holder

    async with coordination_database.session() as session:
        remaining = (await session.exec(select(ExecutionCapacityLease))).all()
    assert remaining == []


@pytest.mark.asyncio
async def test_postgres_capacity_recovers_an_expired_worker_lease(
    coordination_database: Database,
) -> None:
    settings = Settings(
        database_url=str(coordination_database.engine.url),
        agent_tool_route_concurrency=1,
        agent_capacity_wait_seconds=0.2,
    )
    capacity = DistributedExecutionCapacity(coordination_database, settings)
    now = datetime.now(UTC)
    async with coordination_database.session_factory.begin() as session:
        session.add(
            ExecutionCapacityLease(
                id="cap_abandoned",
                resource_kind="tool",
                resource_key="web_fetch",
                owner_id="dead-worker",
                acquired_at=now - timedelta(minutes=2),
                heartbeat_at=now - timedelta(minutes=2),
                expires_at=now - timedelta(seconds=1),
            )
        )

    async with capacity.reserve(
        resource_kind="tool",
        resource_key="web_fetch",
        owner_id="replacement-worker",
    ):
        async with coordination_database.session() as session:
            leases = (await session.exec(select(ExecutionCapacityLease))).all()
        assert len(leases) == 1
        assert leases[0].owner_id == "replacement-worker"


@pytest.mark.asyncio
async def test_postgres_capacity_loss_cancels_the_owned_operation(
    coordination_database: Database,
) -> None:
    settings = Settings(
        database_url=str(coordination_database.engine.url),
        agent_model_route_concurrency=1,
        agent_capacity_lease_seconds=15,
        agent_capacity_heartbeat_seconds=1,
    )
    capacity = DistributedExecutionCapacity(coordination_database, settings)
    acquired = asyncio.Event()
    cancelled = asyncio.Event()

    async def operation() -> None:
        try:
            async with capacity.reserve(
                resource_kind="model",
                resource_key="openrouter:test/model",
                owner_id="worker-that-lost-its-lease",
            ):
                acquired.set()
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(operation())
    await acquired.wait()
    async with coordination_database.session_factory.begin() as session:
        await session.exec(
            ExecutionCapacityLease.__table__.delete().where(
                ExecutionCapacityLease.owner_id == "worker-that-lost-its-lease"
            )
        )

    await asyncio.wait_for(cancelled.wait(), timeout=3)
    with pytest.raises(asyncio.CancelledError):
        await task

    async with coordination_database.session() as session:
        remaining = (await session.exec(select(ExecutionCapacityLease))).all()
    assert remaining == []


@pytest.mark.asyncio
async def test_postgres_response_lease_loss_cancels_the_old_worker_execution(
    coordination_database: Database,
    coordination_auth: AuthContext,
) -> None:
    runtime = LeaseAwareBlockingRuntime()
    settings = Settings(
        database_url=str(coordination_database.engine.url),
        agent_embedded_worker=False,
        agent_worker_concurrency=1,
        agent_job_lease_seconds=15,
        agent_job_heartbeat_seconds=1,
        agent_cancellation_poll_seconds=0.1,
        agent_job_poll_seconds=0.05,
    )
    service = AgentApiService(settings, coordination_database, runtime)  # type: ignore[arg-type]
    await service.startup(start_worker=False)
    worker = asyncio.create_task(service.run_worker_forever())
    try:
        response, _ = await service.create_response(
            coordination_auth,
            _payload(99),
            None,
        )
        await asyncio.wait_for(runtime.started.wait(), timeout=2)

        async with coordination_database.session_factory.begin() as session:
            job = await session.get(ResponseJob, response["id"], with_for_update=True)
            assert job is not None
            job.lease_owner = "replacement-worker"

        await asyncio.wait_for(runtime.cancelled.wait(), timeout=2)
        async with coordination_database.session() as session:
            job = await session.get(ResponseJob, response["id"])
            assert job is not None
            assert job.status == "running"
            assert job.lease_owner == "replacement-worker"
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await service.shutdown()
