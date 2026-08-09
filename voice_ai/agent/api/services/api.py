from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from time import perf_counter
from typing import Any
from uuid import uuid4

from loguru import logger
from pydantic_ai.models import parse_model_id
from sqlalchemy import and_, delete, func, or_, text, update
from sqlmodel import select

from voice_ai.agent.api.auth import AuthContext
from voice_ai.agent.api.models import (
    Conversation,
    ExecutionCapacityLease,
    IdempotencyRecord,
    ModelAvailability,
    RateLimitBucket,
    RequiredAction,
    ResponseEvent,
    ResponseJob,
    ResponseRecord,
    WorkerNode,
)
from voice_ai.agent.api.schemas import ConversationCreateRequest, ResponseCreateRequest
from voice_ai.agent.api.services.events import EventBroker
from voice_ai.agent.api.services.representations import (
    ApiProblem,
    _as_utc,
    _conversation_title,
    _decode_cursor,
    _encode_cursor,
    _event_data,
    _id,
    _input_text,
    _normalized_input,
    _now,
    _output_items,
    _owns,
    action_repr,
    conversation_repr,
    execution_audit_repr,
    request_fingerprint,
    response_repr,
    sse_record,
)
from voice_ai.agent.execution_audit import build_execution_snapshot
from voice_ai.agent.models import (
    ModelConfigurationError,
    model_display_name,
    probe_model_availability,
)
from voice_ai.agent.persistence.database import Database
from voice_ai.agent.protocol import (
    AgentError,
    AgentTurnRequest,
    ResponseCompleted,
    ResponseStarted,
    TextDelta,
    ToolCompleted,
    ToolStarted,
)
from voice_ai.agent.runtime import SYSTEM_PROMPT, AgentRuntime
from voice_ai.shared.config import AgentSettings
from voice_ai.shared.observability import (
    record_agent_active_responses,
    record_agent_job_event,
    record_agent_queue_depth,
    record_agent_response,
    record_agent_response_event,
    record_agent_tool,
    record_agent_worker_recovery,
)

ACTIVE_STATUSES = frozenset({"queued", "in_progress", "requires_action"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class AgentApiService:
    def __init__(
        self,
        settings: AgentSettings,
        database: Database,
        runtime: AgentRuntime,
    ) -> None:
        self.settings = settings
        self.database = database
        self.runtime = runtime
        self.events = EventBroker(settings.agent_event_broker_capacity)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._expiry_tasks: dict[str, asyncio.Task[None]] = {}
        self._response_locks: dict[str, asyncio.Lock] = {}
        self._maintenance_task: asyncio.Task[None] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._work_available = asyncio.Event()
        self._worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"

    async def startup(self, *, start_worker: bool | None = None) -> None:
        await self._reconcile_response_jobs()
        async with self.database.session() as session:
            actions = list(
                (
                    await session.exec(
                        select(RequiredAction).where(RequiredAction.decision.is_(None))
                    )
                ).all()
            )
        for action in actions:
            await self._schedule_expiry(action.id, action.response_id, action.expires_at)
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(),
            name="agent-api-maintenance",
        )
        if start_worker if start_worker is not None else self.settings.agent_embedded_worker:
            self._worker_task = asyncio.create_task(
                self.run_worker_forever(),
                name="agent-response-worker",
            )

    async def shutdown(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            await asyncio.gather(self._worker_task, return_exceptions=True)
            self._worker_task = None
        tasks = [*self._tasks.values(), *self._expiry_tasks.values()]
        if self._maintenance_task:
            tasks.append(self._maintenance_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._expiry_tasks.clear()
        self._maintenance_task = None

    async def list_models(self) -> dict[str, Any]:
        data = []
        for model_id in self.settings.selectable_model_ids:
            data.append(await self._model_entry(model_id, refresh=True))
        return {
            "object": "list",
            "data": data,
            "default": self.settings.agent_model or None,
        }

    async def _model_entry(
        self,
        model_id: str,
        *,
        refresh: bool,
    ) -> dict[str, Any]:
        now = _now()
        async with self.database.session() as session:
            health = await session.get(ModelAvailability, model_id)
        fresh = bool(
            health
            and now - _as_utc(health.checked_at)
            < timedelta(seconds=self.settings.agent_model_probe_ttl_seconds)
        )
        if refresh and not fresh:
            probe = await probe_model_availability(self.settings, model_id)
            await self._record_model_availability(
                model_id,
                status=probe.status,
                reason_code=probe.reason_code,
                detail=probe.detail,
            )
            async with self.database.session() as session:
                health = await session.get(ModelAvailability, model_id)

        provider, _name = parse_model_id(model_id)
        status = health.status if health is not None else "unknown"
        return {
            "id": model_id,
            "object": "model",
            "display_name": model_display_name(model_id),
            "provider": provider or "unknown",
            "status": status,
            "available": status == "available",
            "selectable": status != "unavailable",
            "default": model_id == self.settings.agent_model,
            "reason_code": health.reason_code if health is not None else None,
            "detail": (
                health.detail
                if health is not None
                else "Configured; availability has not been checked"
            ),
            "checked_at": (health.checked_at.isoformat() if health is not None else None),
        }

    async def _ensure_model_selectable(self, model_id: str) -> None:
        if not model_id or model_id not in self.settings.selectable_model_ids:
            raise ApiProblem(
                422,
                "model_not_allowed",
                "The requested model is not in the configured model catalog.",
                extensions={"model": model_id},
            )
        # Catalog reads perform active probes. Submission uses the shared cached
        # result so provider I/O never holds a response-creation transaction open.
        entry = await self._model_entry(model_id, refresh=False)
        if not entry["selectable"]:
            raise ApiProblem(
                503,
                "model_unavailable",
                f"{entry['display_name']} is currently unavailable. Choose another model.",
                extensions={
                    "model": model_id,
                    "model_status": entry["status"],
                    "reason_code": entry["reason_code"],
                    "retry_after": self.settings.agent_model_probe_ttl_seconds,
                },
            )

    async def _record_model_availability(
        self,
        model_id: str,
        *,
        status: str,
        reason_code: str | None,
        detail: str | None,
    ) -> None:
        now = _now()
        values = {
            "model_id": model_id,
            "status": status,
            "reason_code": reason_code,
            "detail": detail,
            "checked_at": now,
            "last_success_at": now if status == "available" else None,
            "last_failure_at": now if status in {"degraded", "unavailable"} else None,
        }
        dialect = self.database.engine.dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:  # pragma: no cover - supported deployments use PostgreSQL/SQLite
            raise RuntimeError(f"Unsupported availability database dialect: {dialect}")
        statement = insert(ModelAvailability).values(**values)
        excluded = statement.excluded
        statement = statement.on_conflict_do_update(
            index_elements=[ModelAvailability.model_id],
            set_={
                "status": excluded.status,
                "reason_code": excluded.reason_code,
                "detail": excluded.detail,
                "checked_at": excluded.checked_at,
                "last_success_at": (
                    excluded.last_success_at
                    if status == "available"
                    else ModelAvailability.last_success_at
                ),
                "last_failure_at": (
                    excluded.last_failure_at
                    if status in {"degraded", "unavailable"}
                    else ModelAvailability.last_failure_at
                ),
            },
        )
        async with self.database.session_factory.begin() as session:
            await session.exec(statement)

    async def create_conversation(
        self,
        auth: AuthContext,
        request: ConversationCreateRequest,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        self._validate_agent(request.agent_id)
        model_id = request.model or self.settings.agent_model
        await self._ensure_model_selectable(model_id)
        fingerprint = request_fingerprint(request.model_dump(mode="json"))
        route = "/v1/conversations"
        async with self.database.session_factory.begin() as session:
            replay = await self._idempotency_lookup(
                session, auth, "POST", route, idempotency_key, fingerprint
            )
            if replay:
                conversation = await session.get(Conversation, replay.resource_id)
                if conversation is None or not _owns(conversation, auth):
                    raise ApiProblem(404, "not_found", "Conversation not found.")
                return conversation_repr(conversation), True

            now = _now()
            conversation = Conversation(
                id=_id("conv"),
                runtime_session_id=uuid4(),
                tenant_id=auth.tenant_id,
                subject_id=auth.subject_id,
                subscriber_id=None,
                agent_id=request.agent_id,
                model_id=model_id,
                status="active",
                metadata_json=request.metadata,
                session_state={},
                created_at=now,
                updated_at=now,
            )
            session.add(conversation)
            await self._idempotency_store(
                session,
                auth,
                "POST",
                route,
                idempotency_key,
                fingerprint,
                "conversation",
                conversation.id,
            )
        return conversation_repr(conversation), False

    async def list_conversations(
        self,
        auth: AuthContext,
        *,
        limit: int,
        after: str | None,
    ) -> dict[str, Any]:
        cursor = _decode_cursor(after, "conversation") if after else None
        conditions = [
            Conversation.tenant_id == auth.tenant_id,
            Conversation.subject_id == auth.subject_id,
            Conversation.status == "active",
        ]
        if cursor:
            created_at, resource_id = cursor
            conditions.append(
                or_(
                    Conversation.updated_at < created_at,
                    and_(
                        Conversation.updated_at == created_at,
                        Conversation.id < resource_id,
                    ),
                )
            )
        async with self.database.session() as session:
            conversations = list(
                (
                    await session.exec(
                        select(Conversation)
                        .where(*conditions)
                        .order_by(
                            Conversation.updated_at.desc(),
                            Conversation.id.desc(),
                        )
                        .limit(limit + 1)
                    )
                ).all()
            )
        has_more = len(conversations) > limit
        page = conversations[:limit]
        return {
            "object": "list",
            "data": [conversation_repr(item) for item in page],
            "has_more": has_more,
            "next_cursor": (
                _encode_cursor("conversation", page[-1].updated_at, page[-1].id)
                if has_more and page
                else None
            ),
        }

    async def get_conversation(self, auth: AuthContext, conversation_id: str) -> dict[str, Any]:
        async with self.database.session() as session:
            conversation = await session.get(Conversation, conversation_id)
            if (
                conversation is None
                or conversation.status != "active"
                or not _owns(conversation, auth)
            ):
                raise ApiProblem(404, "not_found", "Conversation not found.")
            return conversation_repr(conversation)

    async def delete_conversation(
        self,
        auth: AuthContext,
        conversation_id: str,
        idempotency_key: str | None,
    ) -> bool:
        route = f"/v1/conversations/{conversation_id}"
        fingerprint = request_fingerprint({})
        async with self.database.session_factory.begin() as session:
            replay = await self._idempotency_lookup(
                session, auth, "DELETE", route, idempotency_key, fingerprint
            )
            if replay:
                return True
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None or not _owns(conversation, auth):
                raise ApiProblem(404, "not_found", "Conversation not found.")
            if conversation.status == "deleted":
                return False
            active = (
                await session.exec(
                    select(ResponseRecord.id).where(
                        ResponseRecord.conversation_id == conversation_id,
                        ResponseRecord.status.in_(ACTIVE_STATUSES),
                    )
                )
            ).first()
            if active:
                raise ApiProblem(
                    409,
                    "conversation_busy",
                    "The conversation has an active response.",
                    extensions={"response_id": active},
                )
            await self._idempotency_store(
                session,
                auth,
                "DELETE",
                route,
                idempotency_key,
                fingerprint,
                "conversation_deletion",
                conversation_id,
            )
            await session.exec(
                delete(ResponseRecord).where(ResponseRecord.conversation_id == conversation_id)
            )
            conversation.status = "deleted"
            conversation.metadata_json = {}
            conversation.session_state = {}
            conversation.updated_at = _now()
        await self.runtime.close_session(conversation.runtime_session_id)
        return False

    async def create_response(
        self,
        auth: AuthContext,
        request: ResponseCreateRequest,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        if len(request.input_text()) > self.settings.api_max_input_chars:
            raise ApiProblem(413, "input_too_large", "The input exceeds the configured limit.")
        fingerprint = request_fingerprint(request.model_dump(mode="json"))
        route = "/v1/responses"
        async with self.database.session_factory.begin() as session:
            replay = await self._idempotency_lookup(
                session, auth, "POST", route, idempotency_key, fingerprint
            )
            if replay:
                response = await session.get(ResponseRecord, replay.resource_id)
                if response is None or not _owns(response, auth):
                    raise ApiProblem(404, "not_found", "Response not found.")
                return response_repr(response), True

            conversation: Conversation | None = None
            if request.conversation_id:
                conversation = await session.get(Conversation, request.conversation_id)
                if (
                    conversation is None
                    or conversation.status != "active"
                    or not _owns(conversation, auth)
                ):
                    raise ApiProblem(404, "not_found", "Conversation not found.")
                if request.agent_id and request.agent_id != conversation.agent_id:
                    raise ApiProblem(
                        409,
                        "agent_mismatch",
                        "The requested agent does not match the conversation agent.",
                    )
                agent_id = conversation.agent_id
                runtime_session_id = conversation.runtime_session_id
                existing = (
                    await session.exec(
                        select(ResponseRecord.id).where(
                            ResponseRecord.conversation_id == conversation.id,
                            ResponseRecord.status.in_(ACTIVE_STATUSES),
                        )
                    )
                ).first()
                if existing:
                    raise ApiProblem(
                        409,
                        "conversation_busy",
                        "The conversation already has an active response.",
                        extensions={"response_id": existing},
                    )
            else:
                if not request.agent_id:
                    raise ApiProblem(
                        422,
                        "agent_required",
                        "agent_id is required when conversation_id is omitted.",
                    )
                agent_id = request.agent_id
                runtime_session_id = uuid4()
            self._validate_agent(agent_id)

            # The response request is authoritative. Conversation.model_id is a
            # mutable UI preference, while ResponseRecord.model_id is the immutable
            # execution snapshot used by workers and retries.
            model_id = request.model
            await self._ensure_model_selectable(model_id)

            if self.database.engine.dialect.name == "postgresql":
                await session.exec(
                    text("SELECT pg_advisory_xact_lock(hashtext('agent_queue_capacity'))")
                )
            queued_jobs = (
                await session.exec(
                    select(func.count(ResponseJob.response_id)).where(
                        ResponseJob.status.in_({"pending", "running"})
                    )
                )
            ).one()
            if queued_jobs >= self.settings.agent_queue_capacity:
                raise ApiProblem(
                    503,
                    "capacity_exhausted",
                    "The agent is at capacity; retry shortly.",
                    extensions={"retry_after": 2},
                )

            tenant_active = (
                await session.exec(
                    select(func.count(ResponseRecord.id)).where(
                        ResponseRecord.tenant_id == auth.tenant_id,
                        ResponseRecord.status.in_(ACTIVE_STATUSES),
                    )
                )
            ).one()
            if tenant_active >= self.settings.agent_tenant_active_response_limit:
                raise ApiProblem(
                    429,
                    "tenant_capacity_exhausted",
                    "This tenant has too many active responses; retry shortly.",
                    extensions={"retry_after": 2},
                )

            now = _now()
            selection_getter = getattr(self.runtime, "model_selection_for", None)
            try:
                selection = selection_getter(model_id) if callable(selection_getter) else None
            except ModelConfigurationError as exc:
                raise ApiProblem(
                    503,
                    "model_unavailable",
                    "The requested model route could not be configured.",
                    extensions={"model": model_id},
                ) from exc
            response = ResponseRecord(
                id=_id("resp"),
                conversation_id=conversation.id if conversation else None,
                runtime_session_id=runtime_session_id,
                tenant_id=auth.tenant_id,
                subject_id=auth.subject_id,
                subscriber_id=None,
                agent_id=agent_id,
                model_id=model_id,
                status="queued",
                created_at=now,
                started_at=None,
                completed_at=None,
                cancellation_reason=None,
                input_json=_normalized_input(request),
                session_state={},
                output_json=[],
                required_action_json=None,
                error_json=None,
                usage_json=None,
                metadata_json=request.metadata,
                execution_snapshot_json=build_execution_snapshot(
                    self.settings,
                    agent_id=agent_id,
                    model_id=model_id,
                    definition_source=SYSTEM_PROMPT,
                    selection=selection,
                ),
                background=request.background,
                stream=request.stream,
            )
            session.add(response)
            await session.flush()
            current = response_repr(response)
            session.add(
                ResponseEvent(
                    response_id=response.id,
                    sequence_number=1,
                    type="response.created",
                    created_at=now,
                    data_json=_event_data(
                        "response.created", response.id, 1, now, response=current
                    ),
                )
            )
            session.add(
                ResponseJob(
                    response_id=response.id,
                    status="pending",
                    decision=None,
                    attempt_count=0,
                    max_attempts=self.settings.agent_job_max_attempts,
                    available_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            await self._idempotency_store(
                session,
                auth,
                "POST",
                route,
                idempotency_key,
                fingerprint,
                "response",
                response.id,
            )
            if conversation:
                conversation.model_id = model_id
                metadata = dict(conversation.metadata_json)
                if not metadata.get("title"):
                    metadata["title"] = _conversation_title(request.input_text())
                    conversation.metadata_json = metadata
                conversation.updated_at = now
        await self.events.publish(response.id)
        record_agent_response_event(event="accepted", status="queued")
        self._work_available.set()
        return response_repr(response), False

    async def get_response(self, auth: AuthContext, response_id: str) -> dict[str, Any]:
        await self._expire_if_needed(response_id)
        async with self.database.session() as session:
            response = await session.get(ResponseRecord, response_id)
            if response is None or not _owns(response, auth):
                raise ApiProblem(404, "not_found", "Response not found.")
            return response_repr(response)

    async def get_response_execution(
        self,
        auth: AuthContext,
        response_id: str,
    ) -> dict[str, Any]:
        async with self.database.session() as session:
            response = await session.get(ResponseRecord, response_id)
            if response is None or not _owns(response, auth):
                raise ApiProblem(404, "not_found", "Response not found.")
            return execution_audit_repr(response)

    async def list_conversation_responses(
        self,
        auth: AuthContext,
        conversation_id: str,
        *,
        limit: int,
        after: str | None,
    ) -> dict[str, Any]:
        cursor = _decode_cursor(after, "response") if after else None
        async with self.database.session() as session:
            conversation = await session.get(Conversation, conversation_id)
            if (
                conversation is None
                or conversation.status != "active"
                or not _owns(conversation, auth)
            ):
                raise ApiProblem(404, "not_found", "Conversation not found.")
            conditions = [
                ResponseRecord.conversation_id == conversation_id,
                ResponseRecord.tenant_id == auth.tenant_id,
                ResponseRecord.subject_id == auth.subject_id,
            ]
            if cursor:
                created_at, resource_id = cursor
                conditions.append(
                    or_(
                        ResponseRecord.created_at > created_at,
                        and_(
                            ResponseRecord.created_at == created_at,
                            ResponseRecord.id > resource_id,
                        ),
                    )
                )
            responses = list(
                (
                    await session.exec(
                        select(ResponseRecord)
                        .where(*conditions)
                        .order_by(
                            ResponseRecord.created_at.asc(),
                            ResponseRecord.id.asc(),
                        )
                        .limit(limit + 1)
                    )
                ).all()
            )
        has_more = len(responses) > limit
        page = responses[:limit]
        return {
            "object": "list",
            "data": [response_repr(item) for item in page],
            "has_more": has_more,
            "next_cursor": (
                _encode_cursor("response", page[-1].created_at, page[-1].id)
                if has_more and page
                else None
            ),
        }

    async def start_response(self, response_id: str, *, decision: str | None = None) -> None:
        async with self.database.session_factory.begin() as session:
            response = await session.get(ResponseRecord, response_id, with_for_update=True)
            if response is None:
                raise ApiProblem(404, "not_found", "Response not found.")
            if response.status not in {"queued", "in_progress"}:
                return
            job = await session.get(ResponseJob, response_id, with_for_update=True)
            now = _now()
            if job is None:
                job = ResponseJob(
                    response_id=response_id,
                    status="pending",
                    decision=decision,
                    attempt_count=0,
                    max_attempts=self.settings.agent_job_max_attempts,
                    available_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(job)
            elif job.status not in {"running", "pending"}:
                job.status = "pending"
                job.available_at = now
                job.lease_owner = None
                job.lease_expires_at = None
            if decision is not None:
                job.decision = decision
            job.updated_at = now
        self._work_available.set()

    async def wait_for_terminal(self, response_id: str) -> dict[str, Any]:
        deadline = perf_counter() + self.settings.api_sync_wait_timeout_seconds
        while True:
            async with self.database.session() as session:
                response = await session.get(ResponseRecord, response_id)
                if response is None:
                    raise ApiProblem(404, "not_found", "Response not found.")
                if response.status in TERMINAL_STATUSES or response.status == "requires_action":
                    return response_repr(response)
            if perf_counter() >= deadline:
                raise ApiProblem(
                    504,
                    "response_timeout",
                    "The response is still running; retrieve it using its response ID.",
                    extensions={"response_id": response_id, "retry_after": 1},
                )
            observed = await self.events.version(response_id)
            await self.events.wait(
                response_id,
                observed,
                self.settings.api_event_poll_seconds,
            )

    async def cancel_response(
        self,
        auth: AuthContext,
        response_id: str,
        idempotency_key: str | None,
        *,
        reason: str = "client_request",
    ) -> tuple[dict[str, Any], bool]:
        route = f"/v1/responses/{response_id}/cancel"
        fingerprint = request_fingerprint({})
        async with self.database.session_factory.begin() as session:
            replay = await self._idempotency_lookup(
                session, auth, "POST", route, idempotency_key, fingerprint
            )
            response = await session.get(ResponseRecord, response_id, with_for_update=True)
            if response is None or not _owns(response, auth):
                raise ApiProblem(404, "not_found", "Response not found.")
            if replay:
                return response_repr(response), True
            if response.status in ACTIVE_STATUSES:
                await self._set_cancelled(session, response, reason)
            job = await session.get(ResponseJob, response_id, with_for_update=True)
            if job is not None:
                job.status = "cancelled"
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = _now()
            await self._idempotency_store(
                session,
                auth,
                "POST",
                route,
                idempotency_key,
                fingerprint,
                "response",
                response.id,
            )
        task = self._tasks.get(response_id)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._tasks.pop(response_id, None)
        await self._clear_confirmation_for_response(response)
        await self.events.publish(response_id)
        return response_repr(response), False

    async def submit_action(
        self,
        auth: AuthContext,
        response_id: str,
        action_id: str,
        decision: str,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        route = f"/v1/responses/{response_id}/actions/{action_id}"
        fingerprint = request_fingerprint({"decision": decision})
        expired = False
        async with self.database.session_factory.begin() as session:
            replay = await self._idempotency_lookup(
                session, auth, "POST", route, idempotency_key, fingerprint
            )
            response = await session.get(ResponseRecord, response_id, with_for_update=True)
            action = await session.get(RequiredAction, action_id, with_for_update=True)
            if (
                response is None
                or action is None
                or action.response_id != response_id
                or not _owns(response, auth)
                or not _owns(action, auth)
            ):
                raise ApiProblem(404, "not_found", "Required action not found.")
            if replay:
                return response_repr(response), True
            now = _now()
            if action.decision == decision:
                return response_repr(response), True
            if action.decision:
                raise ApiProblem(
                    409,
                    "action_already_resolved",
                    "The required action has already been resolved.",
                )
            if _as_utc(action.expires_at) <= now:
                action.decision = "expired"
                action.decided_at = now
                await self._set_cancelled(session, response, "action_expired")
                expired = True
            elif response.status != "requires_action":
                raise ApiProblem(
                    409,
                    "action_already_resolved",
                    "The response is no longer awaiting this action.",
                )
            else:
                action.decision = decision
                action.decided_at = now
                response.status = "queued"
                response.required_action_json = None
                job = await session.get(ResponseJob, response_id, with_for_update=True)
                if job is None:
                    job = ResponseJob(
                        response_id=response_id,
                        status="pending",
                        decision=decision,
                        attempt_count=0,
                        max_attempts=self.settings.agent_job_max_attempts,
                        available_at=now,
                        lease_owner=None,
                        lease_expires_at=None,
                        heartbeat_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(job)
                else:
                    job.status = "pending"
                    job.decision = decision
                    job.attempt_count = 0
                    job.available_at = now
                    job.lease_owner = None
                    job.lease_expires_at = None
                    job.updated_at = now
                await self._idempotency_store(
                    session,
                    auth,
                    "POST",
                    route,
                    idempotency_key,
                    fingerprint,
                    "response",
                    response.id,
                )
        if expired:
            await self._clear_confirmation_for_response(response)
            await self.events.publish(response_id)
            raise ApiProblem(409, "action_expired", "The required action has expired.")
        expiry = self._expiry_tasks.pop(action_id, None)
        if expiry:
            expiry.cancel()
        await self.start_response(response_id, decision=decision)
        return response_repr(response), False

    async def event_page(
        self,
        auth: AuthContext,
        response_id: str,
        after: int,
    ) -> tuple[list[dict[str, Any]], str]:
        await self._expire_if_needed(response_id)
        async with self.database.session() as session:
            response = await session.get(ResponseRecord, response_id)
            if response is None or not _owns(response, auth):
                raise ApiProblem(404, "not_found", "Response not found.")
            bounds = (
                await session.exec(
                    select(
                        func.min(ResponseEvent.sequence_number),
                        func.max(ResponseEvent.sequence_number),
                    ).where(ResponseEvent.response_id == response_id)
                )
            ).one()
            minimum, maximum = bounds
            if (
                after < 0
                or (after > 0 and maximum is None)
                or (maximum is not None and after > maximum)
            ):
                raise ApiProblem(409, "event_cursor_expired", "The event cursor is invalid.")
            if after and minimum is not None and after < minimum - 1:
                raise ApiProblem(
                    409, "event_cursor_expired", "The requested events are no longer retained."
                )
            events = list(
                (
                    await session.exec(
                        select(ResponseEvent)
                        .where(
                            ResponseEvent.response_id == response_id,
                            ResponseEvent.sequence_number > after,
                        )
                        .order_by(ResponseEvent.sequence_number)
                    )
                ).all()
            )
            return [event.data_json for event in events], response.status

    async def stream_events(
        self,
        auth: AuthContext,
        response_id: str,
        after: int,
        *,
        heartbeat_seconds: float = 15,
    ) -> AsyncIterator[str]:
        cursor = after
        last_heartbeat = perf_counter()
        while True:
            observed = await self.events.version(response_id)
            events, status = await self.event_page(auth, response_id, cursor)
            for event in events:
                cursor = int(event["sequence_number"])
                yield sse_record(event)
            if status in TERMINAL_STATUSES or status == "requires_action":
                return
            until_heartbeat = max(
                0.05,
                heartbeat_seconds - (perf_counter() - last_heartbeat),
            )
            notified = await self.events.wait(
                response_id,
                observed,
                min(self.settings.api_event_poll_seconds, until_heartbeat),
            )
            if not notified and perf_counter() - last_heartbeat >= heartbeat_seconds:
                yield ": ping\n\n"
                last_heartbeat = perf_counter()

    async def prune(self) -> None:
        now = _now()
        event_cutoff = now - timedelta(hours=self.settings.api_event_retention_hours)
        async with self.database.session_factory.begin() as session:
            expired_response_ids = select(ResponseRecord.id).where(
                ResponseRecord.status.in_(TERMINAL_STATUSES),
                ResponseRecord.completed_at.is_not(None),
                ResponseRecord.completed_at < event_cutoff,
            )
            await session.exec(
                delete(ResponseEvent).where(ResponseEvent.response_id.in_(expired_response_ids))
            )
            await session.exec(delete(IdempotencyRecord).where(IdempotencyRecord.expires_at < now))
            await session.exec(delete(RateLimitBucket).where(RateLimitBucket.expires_at < now))
            await session.exec(
                delete(ExecutionCapacityLease).where(ExecutionCapacityLease.expires_at < now)
            )
            await session.exec(
                delete(WorkerNode).where(
                    WorkerNode.heartbeat_at
                    < now - timedelta(seconds=self.settings.agent_worker_presence_ttl_seconds * 10)
                )
            )

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(3_600)
            try:
                await self.prune()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Agent maintenance failed; retrying on the next interval")

    async def run_worker_forever(self) -> None:
        """Claim durable jobs up to the configured per-process concurrency."""
        logger.info(
            "Agent response worker started worker_id={} concurrency={}",
            self._worker_id,
            self.settings.agent_worker_concurrency,
        )
        presence: asyncio.Task[None] | None = None
        try:
            await self._expire_stale_workers()
            await self._record_worker_presence("active")
            presence = asyncio.create_task(
                self._worker_presence_loop(),
                name="agent-worker-presence",
            )
            while True:
                self._tasks = {
                    response_id: task
                    for response_id, task in self._tasks.items()
                    if not task.done()
                }
                claimed_any = False
                while len(self._tasks) < self.settings.agent_worker_concurrency:
                    claimed = await self._claim_response_job()
                    if claimed is None:
                        break
                    response_id, decision, recovered = claimed
                    claimed_any = True
                    task = asyncio.create_task(
                        self._execute_claimed_job(response_id, decision, recovered=recovered),
                        name=f"agent-response-{response_id}",
                    )
                    self._tasks[response_id] = task
                    task.add_done_callback(
                        lambda finished, claimed_id=response_id: self._response_task_finished(
                            claimed_id,
                            finished,
                        )
                    )
                if claimed_any:
                    await asyncio.sleep(0)
                    continue
                self._work_available.clear()
                try:
                    await asyncio.wait_for(
                        self._work_available.wait(),
                        timeout=self.settings.agent_job_poll_seconds,
                    )
                except TimeoutError:
                    await self._fail_exhausted_jobs()
        finally:
            if presence is not None:
                presence.cancel()
                await asyncio.gather(presence, return_exceptions=True)
            await self._record_worker_presence("stopped")

    def _response_task_finished(
        self,
        response_id: str,
        task: asyncio.Task[None],
    ) -> None:
        if self._tasks.get(response_id) is task:
            self._tasks.pop(response_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            record_agent_job_event(event="task_failed", attempt=0)
            logger.opt(exception=error).error(
                "Agent response task failed; durable lease recovery remains authoritative "
                "response_id={}",
                response_id,
            )

    async def _worker_presence_loop(self) -> None:
        while True:
            await self._record_worker_presence("active")
            await asyncio.sleep(max(2, self.settings.agent_worker_presence_ttl_seconds // 3))

    async def _expire_stale_workers(self) -> None:
        cutoff = _now() - timedelta(seconds=self.settings.agent_worker_presence_ttl_seconds)
        async with self.database.session_factory.begin() as session:
            await session.exec(
                update(WorkerNode)
                .where(
                    WorkerNode.status == "active",
                    WorkerNode.heartbeat_at < cutoff,
                )
                .values(status="stopped")
            )

    async def _record_worker_presence(self, status: str) -> None:
        now = _now()
        async with self.database.session_factory.begin() as session:
            node = await session.get(WorkerNode, self._worker_id, with_for_update=True)
            if node is None:
                session.add(
                    WorkerNode(
                        id=self._worker_id,
                        status=status,
                        concurrency=self.settings.agent_worker_concurrency,
                        started_at=now,
                        heartbeat_at=now,
                    )
                )
            else:
                node.status = status
                node.concurrency = self.settings.agent_worker_concurrency
                node.heartbeat_at = now

    async def worker_readiness(self) -> dict[str, Any]:
        cutoff = _now() - timedelta(seconds=self.settings.agent_worker_presence_ttl_seconds)
        async with self.database.session() as session:
            active = (
                await session.exec(
                    select(func.count(WorkerNode.id), func.sum(WorkerNode.concurrency)).where(
                        WorkerNode.status == "active",
                        WorkerNode.heartbeat_at >= cutoff,
                    )
                )
            ).one()
            queued = (
                await session.exec(
                    select(func.count(ResponseJob.response_id)).where(
                        ResponseJob.status == "pending"
                    )
                )
            ).one()
            active_responses = (
                await session.exec(
                    select(func.count(ResponseRecord.id)).where(
                        ResponseRecord.status.in_(ACTIVE_STATUSES)
                    )
                )
            ).one()
        workers, concurrency = active
        record_agent_queue_depth(int(queued or 0))
        record_agent_active_responses(int(active_responses or 0))
        return {
            "ready": bool(workers),
            "workers": int(workers or 0),
            "concurrency": int(concurrency or 0),
            "queued": int(queued or 0),
        }

    async def _claim_response_job(self) -> tuple[str, str | None, bool] | None:
        now = _now()
        async with self.database.session_factory.begin() as session:
            statement = (
                select(ResponseJob)
                .where(
                    ResponseJob.available_at <= now,
                    ResponseJob.attempt_count < ResponseJob.max_attempts,
                    or_(
                        ResponseJob.status == "pending",
                        and_(
                            ResponseJob.status == "running",
                            ResponseJob.lease_expires_at.is_not(None),
                            ResponseJob.lease_expires_at <= now,
                        ),
                    ),
                )
                .order_by(ResponseJob.available_at, ResponseJob.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            job = (await session.exec(statement)).first()
            if job is None:
                return None
            response = await session.get(ResponseRecord, job.response_id, with_for_update=True)
            if response is None or response.status not in {"queued", "in_progress"}:
                job.status = "completed"
                job.updated_at = now
                return None
            recovered_lease = job.lease_expires_at if job.status == "running" else None
            job.status = "running"
            job.attempt_count += 1
            job.lease_owner = self._worker_id
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(seconds=self.settings.agent_job_lease_seconds)
            job.updated_at = now
            record_agent_job_event(
                event="claimed",
                attempt=job.attempt_count,
            )
            if recovered_lease is not None:
                record_agent_job_event(event="recovered", attempt=job.attempt_count)
                record_agent_worker_recovery(
                    delay_ms=max(
                        0.0,
                        (now - _as_utc(recovered_lease)).total_seconds() * 1_000,
                    ),
                    attempt=job.attempt_count,
                )
            return job.response_id, job.decision, recovered_lease is not None

    async def _execute_claimed_job(
        self,
        response_id: str,
        decision: str | None,
        *,
        recovered: bool = False,
    ) -> None:
        if recovered and await self._has_non_replayable_progress(response_id):
            await self._fail_response(
                response_id,
                code="execution_interrupted",
                message=(
                    "The worker stopped after publishing model or tool progress. "
                    "Retry the request to avoid replaying completed work automatically."
                ),
                retryable=True,
            )
            return
        execution_task = asyncio.current_task()
        assert execution_task is not None
        heartbeat = asyncio.create_task(
            self._heartbeat_job(response_id, execution_task),
            name=f"agent-heartbeat-{response_id}",
        )
        try:
            async with asyncio.timeout(self.settings.agent_execution_timeout_seconds):
                await self._run_response(response_id, decision=decision)
        except TimeoutError:
            logger.warning(
                "Agent response exceeded execution timeout response_id={}",
                response_id,
            )
            await self._fail_response(
                response_id,
                code="execution_timeout",
                message="The response exceeded its maximum execution time.",
                retryable=True,
            )
        except asyncio.CancelledError:
            raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        await self._finalize_response_job(response_id)

    async def _has_non_replayable_progress(self, response_id: str) -> bool:
        async with self.database.session() as session:
            event = (
                await session.exec(
                    select(ResponseEvent.sequence_number).where(
                        ResponseEvent.response_id == response_id,
                        ResponseEvent.type.in_(
                            {
                                "response.output_text.delta",
                                "response.tool.started",
                                "response.tool.completed",
                            }
                        ),
                    )
                )
            ).first()
        return event is not None

    async def _heartbeat_job(
        self,
        response_id: str,
        execution_task: asyncio.Task[None],
    ) -> None:
        heartbeat_interval = min(
            self.settings.agent_job_heartbeat_seconds,
            max(1, self.settings.agent_job_lease_seconds // 3),
        )
        interval = min(
            heartbeat_interval,
            self.settings.agent_cancellation_poll_seconds,
        )
        last_heartbeat = perf_counter()
        while True:
            await asyncio.sleep(interval)
            try:
                now = _now()
                async with self.database.session_factory.begin() as session:
                    job = await session.get(ResponseJob, response_id, with_for_update=True)
                    if job is not None and job.status == "cancelled":
                        execution_task.cancel()
                        return
                    if job is None or job.status != "running" or job.lease_owner != self._worker_id:
                        logger.warning(
                            "Agent response lease was lost; cancelling local execution "
                            "response_id={}",
                            response_id,
                        )
                        record_agent_job_event(
                            event="lease_lost",
                            attempt=job.attempt_count if job else 0,
                        )
                        execution_task.cancel()
                        return
                    if perf_counter() - last_heartbeat >= heartbeat_interval:
                        job.heartbeat_at = now
                        job.lease_expires_at = now + timedelta(
                            seconds=self.settings.agent_job_lease_seconds
                        )
                        job.updated_at = now
                        last_heartbeat = perf_counter()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Agent response heartbeat failed; cancelling local execution response_id={}",
                    response_id,
                )
                record_agent_job_event(event="heartbeat_failed", attempt=0)
                if not execution_task.done():
                    execution_task.cancel()
                return

    async def _finalize_response_job(self, response_id: str) -> None:
        async with self.database.session_factory.begin() as session:
            job = await session.get(ResponseJob, response_id, with_for_update=True)
            response = await session.get(ResponseRecord, response_id)
            if job is None or job.lease_owner != self._worker_id:
                return
            status = response.status if response is not None else "failed"
            job.status = (
                "cancelled"
                if status == "cancelled"
                else "dead"
                if status == "failed"
                else "completed"
            )
            job.lease_owner = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.updated_at = _now()
            record_agent_job_event(
                event=job.status,
                attempt=job.attempt_count,
            )

    async def _fail_exhausted_jobs(self) -> None:
        now = _now()
        exhausted: list[str] = []
        async with self.database.session_factory.begin() as session:
            jobs = list(
                (
                    await session.exec(
                        select(ResponseJob)
                        .where(
                            ResponseJob.status == "running",
                            ResponseJob.lease_expires_at <= now,
                            ResponseJob.attempt_count >= ResponseJob.max_attempts,
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for job in jobs:
                job.status = "dead"
                job.lease_owner = None
                job.lease_expires_at = None
                job.updated_at = now
                exhausted.append(job.response_id)
        for response_id in exhausted:
            await self._fail_response(
                response_id,
                code="execution_attempts_exhausted",
                message="The response could not be completed after repeated worker failures.",
                retryable=False,
            )

    async def _run_response(self, response_id: str, *, decision: str | None) -> None:
        lock = self._response_locks.setdefault(response_id, asyncio.Lock())
        runtime_session_id = None
        tool_timings: dict[str, tuple[float, str, str]] = {}
        try:
            async with lock:
                try:
                    response, session_state = await self._mark_in_progress(response_id)
                    runtime_session_id = response.runtime_session_id
                    execution_started = perf_counter()
                    await self.runtime.restore_session_state(
                        response.runtime_session_id, session_state
                    )
                    text = (
                        "yes please"
                        if decision == "approve"
                        else "no"
                        if decision == "reject"
                        else _input_text(response.input_json)
                    )
                    output_text = ""
                    request = AgentTurnRequest(
                        session_id=response.runtime_session_id,
                        text=text,
                        model_id=response.model_id,
                    )
                    tool_runs: dict[str, list[str]] = {}
                    tool_activity: list[dict[str, Any]] = []
                    turn_completed = False
                    turn_usage: dict[str, Any] | None = None
                    time_to_first_text_ms: float | None = None
                    output_size_bytes = 0
                    output_item_id = (
                        f"msg_{response_id.removeprefix('resp_')}{'_resume' if decision else ''}"
                    )
                    async for event in self.runtime.stream_turn(request):
                        if isinstance(event, ResponseStarted):
                            continue
                        if isinstance(event, ToolStarted):
                            tool_run_id = _id("toolrun")
                            correlation_key = event.tool_call_id or (
                                f"{event.source}:{event.agent or 'root'}:{event.tool}"
                            )
                            tool_runs.setdefault(correlation_key, []).append(tool_run_id)
                            tool_timings[tool_run_id] = (
                                perf_counter(),
                                event.tool,
                                event.source,
                            )
                            display_label = (
                                f"{event.label} · {event.agent}"
                                if event.source == "subagent" and event.agent
                                else event.label
                            )
                            activity_item = {
                                "id": tool_run_id,
                                "type": "tool_activity",
                                "name": event.tool,
                                "label": display_label,
                                "status": "running",
                            }
                            if event.source == "subagent":
                                activity_item.update({"source": event.source, "agent": event.agent})
                            tool_activity.append(activity_item)
                            await self._append_event(
                                response_id,
                                "response.tool.started",
                                tool_run_id=tool_run_id,
                                name=event.tool,
                                label=display_label,
                                source=event.source,
                                agent=event.agent,
                            )
                        elif isinstance(event, ToolCompleted):
                            correlation_key = event.tool_call_id or (
                                f"{event.source}:{event.agent or 'root'}:{event.tool}"
                            )
                            pending_runs = tool_runs.get(correlation_key) or []
                            tool_run_id = pending_runs.pop(0) if pending_runs else _id("toolrun")
                            display_label = (
                                f"{event.label} · {event.agent}"
                                if event.source == "subagent" and event.agent
                                else event.label
                            )
                            activity = next(
                                (
                                    item
                                    for item in reversed(tool_activity)
                                    if item["id"] == tool_run_id
                                ),
                                None,
                            )
                            if activity is None:
                                activity = {
                                    "id": tool_run_id,
                                    "type": "tool_activity",
                                    "name": event.tool,
                                    "label": display_label,
                                }
                                if event.source == "subagent":
                                    activity.update({"source": event.source, "agent": event.agent})
                                tool_activity.append(activity)
                            activity["status"] = "succeeded"
                            timing = tool_timings.pop(tool_run_id, None)
                            if timing is not None:
                                tool_started, tool_name, tool_source = timing
                                record_agent_tool(
                                    duration_ms=round(
                                        (perf_counter() - tool_started) * 1_000,
                                        1,
                                    ),
                                    tool=tool_name,
                                    source=tool_source,
                                    status="succeeded",
                                )
                            if event.detail not in {"Completed", "Tool completed"}:
                                activity["detail"] = event.detail
                            await self._append_event(
                                response_id,
                                "response.tool.completed",
                                tool_run_id=tool_run_id,
                                name=event.tool,
                                status="succeeded",
                                label=display_label,
                                detail=event.detail,
                                source=event.source,
                                agent=event.agent,
                            )
                        elif isinstance(event, TextDelta):
                            if time_to_first_text_ms is None:
                                time_to_first_text_ms = round(
                                    (perf_counter() - execution_started) * 1_000, 1
                                )
                            delta = event.text
                            output_size_bytes += len(delta.encode("utf-8"))
                            if output_size_bytes > self.settings.agent_max_output_bytes:
                                await self._fail_response(
                                    response_id,
                                    code="output_limit_exceeded",
                                    message="The response exceeded its maximum output size.",
                                    retryable=False,
                                    time_to_first_text_ms=time_to_first_text_ms,
                                )
                                return
                            output_text += delta
                            await self._append_event(
                                response_id,
                                "response.output_text.delta",
                                item_id=output_item_id,
                                content_index=0,
                                delta=delta,
                            )
                        elif isinstance(event, AgentError):
                            if event.model_id and event.model_status in {
                                "degraded",
                                "unavailable",
                            }:
                                await self._record_model_availability(
                                    event.model_id,
                                    status=event.model_status,
                                    reason_code=event.code,
                                    detail=event.message,
                                )
                            await self._fail_response(
                                response_id,
                                code=event.code,
                                message=event.message,
                                retryable=event.retryable,
                                usage=(
                                    event.usage.model_dump(mode="json")
                                    if event.usage is not None
                                    else None
                                ),
                                time_to_first_text_ms=time_to_first_text_ms,
                            )
                            return
                        elif isinstance(event, ResponseCompleted):
                            await self._record_model_availability(
                                response.model_id,
                                status="available",
                                reason_code=None,
                                detail="A recent request completed successfully",
                            )
                            turn_completed = True
                            turn_usage = (
                                event.usage.model_dump(mode="json")
                                if event.usage is not None
                                else None
                            )
                    # AgentRuntime serializes each turn with its session lock. Read
                    # confirmation/history only after the generator has exited and
                    # released that lock; re-entering from inside TextDelta handling
                    # deadlocks a real runtime even though simple test doubles allow it.
                    if not turn_completed:
                        await self._fail_response(
                            response_id,
                            code="agent_execution_failed",
                            message="The agent stream ended without completing the response.",
                            retryable=True,
                        )
                        return
                    pending = await self.runtime.pending_confirmation(response.runtime_session_id)
                    state = await self.runtime.export_session_state(response.runtime_session_id)
                    if pending and decision is None:
                        await self._require_action(
                            response_id,
                            response,
                            output_text,
                            output_item_id,
                            pending,
                            state,
                            tool_activity,
                            turn_usage,
                            time_to_first_text_ms,
                        )
                    else:
                        await self._complete_response(
                            response_id,
                            output_text,
                            output_item_id,
                            state,
                            tool_activity,
                            turn_usage,
                            time_to_first_text_ms,
                        )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("API response {} execution failed", response_id)
                    await self._fail_response(
                        response_id,
                        code="agent_execution_failed",
                        message="The agent could not complete that request.",
                        retryable=True,
                    )
        finally:
            for tool_started, tool_name, tool_source in tool_timings.values():
                record_agent_tool(
                    duration_ms=round((perf_counter() - tool_started) * 1_000, 1),
                    tool=tool_name,
                    source=tool_source,
                    status="interrupted",
                )
            if runtime_session_id is not None:
                await self.runtime.close_session(runtime_session_id)
            if self._response_locks.get(response_id) is lock:
                self._response_locks.pop(response_id, None)

    async def _mark_in_progress(self, response_id: str) -> tuple[ResponseRecord, dict[str, Any]]:
        async with self.database.session_factory.begin() as session:
            response = await session.get(ResponseRecord, response_id, with_for_update=True)
            if response is None:
                raise ApiProblem(404, "not_found", "Response not found.")
            if response.status not in {"queued", "in_progress"}:
                raise ApiProblem(409, "response_not_active", "Response is not executable.")
            now = _now()
            response.status = "in_progress"
            response.started_at = response.started_at or now
            conversation = (
                await session.get(Conversation, response.conversation_id)
                if response.conversation_id
                else None
            )
            state = conversation.session_state if conversation else response.session_state
            await self._append_event_in_session(session, response, "response.in_progress", now=now)
        await self.events.publish(response_id)
        return response, state

    async def _append_event(self, response_id: str, event_type: str, **fields: Any) -> None:
        async with self.database.session_factory.begin() as session:
            response = await session.get(ResponseRecord, response_id, with_for_update=True)
            if response is None or response.status in TERMINAL_STATUSES:
                return
            await self._append_event_in_session(session, response, event_type, **fields)
        await self.events.publish(response_id)

    async def _append_event_in_session(
        self,
        session,
        response_row: ResponseRecord,
        event_type: str,
        *,
        now: datetime | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        sequence = (
            (
                await session.exec(
                    select(func.max(ResponseEvent.sequence_number)).where(
                        ResponseEvent.response_id == response_row.id
                    )
                )
            ).one()
            or 0
        ) + 1
        created_at = now or _now()
        data = _event_data(event_type, response_row.id, sequence, created_at, **fields)
        session.add(
            ResponseEvent(
                response_id=response_row.id,
                sequence_number=sequence,
                type=event_type,
                created_at=created_at,
                data_json=data,
            )
        )
        return data

    async def _complete_response(
        self,
        response_id: str,
        output_text: str,
        output_item_id: str,
        state: dict[str, Any],
        tool_activity: list[dict[str, Any]],
        usage: dict[str, Any] | None,
        time_to_first_text_ms: float | None,
    ) -> None:
        async with self.database.session_factory.begin() as session:
            response = await session.get(ResponseRecord, response_id, with_for_update=True)
            if response is None or response.status in TERMINAL_STATUSES:
                return
            now = _now()
            response.status = "completed"
            response.completed_at = now
            response.output_json = _output_items(output_item_id, output_text, tool_activity)
            response.usage_json = _finalize_usage(
                response,
                usage,
                now=now,
                time_to_first_text_ms=time_to_first_text_ms,
                settings=self.settings,
                status="completed",
            )
            await self._persist_conversation_state(session, response, state, now)
            await self._append_event_in_session(
                session,
                response,
                "response.completed",
                now=now,
                response=response_repr(response),
            )
            await self._finish_job_in_session(session, response_id, "completed", now)
        await self.events.publish(response_id)

    async def _require_action(
        self,
        response_id: str,
        response_snapshot: ResponseRecord,
        output_text: str,
        output_item_id: str,
        pending: dict[str, Any],
        state: dict[str, Any],
        tool_activity: list[dict[str, Any]],
        usage: dict[str, Any] | None,
        time_to_first_text_ms: float | None,
    ) -> None:
        now = _now()
        pending_created_at = pending.get("created_at")
        expires_at = (
            _as_utc(pending_created_at) if isinstance(pending_created_at, datetime) else now
        ) + timedelta(seconds=self.settings.confirmation_ttl_seconds)
        action = RequiredAction(
            id=_id("act"),
            response_id=response_id,
            tenant_id=response_snapshot.tenant_id,
            subject_id=response_snapshot.subject_id,
            type="confirmation",
            title=f"Change plan to {pending['plan_name']}",
            description=(
                f"The monthly charge will be {pending['currency']} {pending['quoted_price']}."
            ),
            expires_at=expires_at,
            decision=None,
            decided_at=None,
            created_at=now,
        )
        required_action = action_repr(action)
        async with self.database.session_factory.begin() as session:
            response = await session.get(ResponseRecord, response_id, with_for_update=True)
            if response is None or response.status in TERMINAL_STATUSES:
                return
            session.add(action)
            response.status = "requires_action"
            response.output_json = _output_items(output_item_id, output_text, tool_activity)
            response.required_action_json = required_action
            response.usage_json = _finalize_usage(
                response,
                usage,
                now=now,
                time_to_first_text_ms=time_to_first_text_ms,
                settings=self.settings,
                status="requires_action",
            )
            await self._persist_conversation_state(session, response, state, now)
            await self._append_event_in_session(
                session,
                response,
                "response.requires_action",
                now=now,
                required_action=required_action,
                response=response_repr(response),
            )
            await self._finish_job_in_session(session, response_id, "completed", now)
        await self.events.publish(response_id)
        await self._schedule_expiry(action.id, response_id, expires_at)

    async def _fail_response(
        self,
        response_id: str,
        *,
        code: str,
        message: str,
        retryable: bool,
        usage: dict[str, Any] | None = None,
        time_to_first_text_ms: float | None = None,
    ) -> None:
        async with self.database.session_factory.begin() as session:
            response = await session.get(ResponseRecord, response_id, with_for_update=True)
            if response is None or response.status in TERMINAL_STATUSES:
                return
            now = _now()
            response.status = "failed"
            response.completed_at = now
            response.error_json = {
                "code": code,
                "message": message,
                "retryable": retryable,
            }
            response.usage_json = _finalize_usage(
                response,
                usage,
                now=now,
                time_to_first_text_ms=time_to_first_text_ms,
                settings=self.settings,
                status="failed",
            )
            await self._append_event_in_session(
                session,
                response,
                "response.failed",
                now=now,
                response=response_repr(response),
            )
            await self._finish_job_in_session(session, response_id, "dead", now)
        await self.events.publish(response_id)

    async def _set_cancelled(
        self,
        session,
        response: ResponseRecord,
        reason: str,
    ) -> None:
        if response.status in TERMINAL_STATUSES:
            return
        now = _now()
        response.status = "cancelled"
        response.completed_at = now
        response.cancellation_reason = reason
        response.required_action_json = None
        previous_usage = response.usage_json or {}
        previous_latency = previous_usage.get("latency", {})
        response.usage_json = _finalize_usage(
            response,
            previous_usage,
            now=now,
            time_to_first_text_ms=previous_latency.get("time_to_first_text_ms"),
            settings=self.settings,
            status="cancelled",
        )
        await self._append_event_in_session(
            session,
            response,
            "response.cancelled",
            now=now,
            response=response_repr(response),
        )
        await self._finish_job_in_session(session, response.id, "cancelled", now)

    async def _finish_job_in_session(
        self,
        session,
        response_id: str,
        status: str,
        now: datetime,
    ) -> None:
        job = await session.get(ResponseJob, response_id, with_for_update=True)
        if job is None:
            return
        job.status = status
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        job.updated_at = now
        record_agent_job_event(
            event=status,
            attempt=job.attempt_count,
        )

    async def _persist_conversation_state(
        self,
        session,
        response: ResponseRecord,
        state: dict[str, Any],
        now: datetime,
    ) -> None:
        if response.conversation_id:
            conversation = await session.get(
                Conversation, response.conversation_id, with_for_update=True
            )
            if conversation:
                conversation.session_state = state
                conversation.updated_at = now
        else:
            response.session_state = state

    async def _save_session_state_for_response(
        self, response_id: str, state: dict[str, Any]
    ) -> None:
        async with self.database.session_factory.begin() as session:
            response = await session.get(ResponseRecord, response_id, with_for_update=True)
            if response is not None:
                await self._persist_conversation_state(session, response, state, _now())

    async def _restore_session_for_response(self, response: ResponseRecord) -> None:
        async with self.database.session() as session:
            conversation = (
                await session.get(Conversation, response.conversation_id)
                if response.conversation_id
                else None
            )
            state = conversation.session_state if conversation else response.session_state
        await self.runtime.restore_session_state(response.runtime_session_id, state)

    async def _clear_confirmation_for_response(self, response: ResponseRecord) -> None:
        try:
            await self._restore_session_for_response(response)
            await self.runtime.clear_confirmation(response.runtime_session_id)
            state = await self.runtime.export_session_state(response.runtime_session_id)
            await self._save_session_state_for_response(response.id, state)
        finally:
            await self.runtime.close_session(response.runtime_session_id)

    async def _schedule_expiry(
        self, action_id: str, response_id: str, expires_at: datetime
    ) -> None:
        existing = self._expiry_tasks.get(action_id)
        if existing and not existing.done():
            return

        async def expire() -> None:
            delay = max(0.0, (_as_utc(expires_at) - _now()).total_seconds())
            await asyncio.sleep(delay)
            await self._expire_action(action_id, response_id)

        task = asyncio.create_task(expire(), name=f"required-action-{action_id}")
        self._expiry_tasks[action_id] = task
        task.add_done_callback(lambda _: self._expiry_tasks.pop(action_id, None))

    async def _expire_action(self, action_id: str, response_id: str) -> None:
        async with self.database.session_factory.begin() as session:
            action = await session.get(RequiredAction, action_id, with_for_update=True)
            response = await session.get(ResponseRecord, response_id, with_for_update=True)
            if (
                action is None
                or response is None
                or action.decision is not None
                or _as_utc(action.expires_at) > _now()
                or response.status != "requires_action"
            ):
                return
            action.decision = "expired"
            action.decided_at = _now()
            await self._set_cancelled(session, response, "action_expired")
        await self._clear_confirmation_for_response(response)
        await self.events.publish(response_id)

    async def _expire_if_needed(self, response_id: str) -> None:
        async with self.database.session() as session:
            action = (
                await session.exec(
                    select(RequiredAction).where(
                        RequiredAction.response_id == response_id,
                        RequiredAction.decision.is_(None),
                        RequiredAction.expires_at <= _now(),
                    )
                )
            ).first()
        if action:
            await self._expire_action(action.id, response_id)

    async def _reconcile_response_jobs(self) -> None:
        """Backfill jobs without taking ownership from a healthy worker replica."""
        async with self.database.session_factory.begin() as session:
            responses = list(
                (
                    await session.exec(
                        select(ResponseRecord)
                        .where(ResponseRecord.status.in_({"queued", "in_progress"}))
                        .with_for_update()
                    )
                ).all()
            )
            for response in responses:
                now = _now()
                job = await session.get(ResponseJob, response.id, with_for_update=True)
                if job is None:
                    session.add(
                        ResponseJob(
                            response_id=response.id,
                            status="pending",
                            decision=None,
                            attempt_count=0,
                            max_attempts=self.settings.agent_job_max_attempts,
                            available_at=now,
                            lease_owner=None,
                            lease_expires_at=None,
                            heartbeat_at=None,
                            created_at=now,
                            updated_at=now,
                        )
                    )
        self._work_available.set()

    async def _idempotency_lookup(
        self,
        session,
        auth: AuthContext,
        method: str,
        route: str,
        key: str | None,
        fingerprint: str,
    ) -> IdempotencyRecord | None:
        if not key:
            return None
        if len(key) > 255:
            raise ApiProblem(400, "invalid_idempotency_key", "Idempotency-Key is too long.")
        record = (
            await session.exec(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.tenant_id == auth.tenant_id,
                    IdempotencyRecord.subject_id == auth.subject_id,
                    IdempotencyRecord.method == method,
                    IdempotencyRecord.route == route,
                    IdempotencyRecord.key == key,
                )
            )
        ).first()
        if record and record.fingerprint != fingerprint:
            raise ApiProblem(
                409,
                "idempotency_conflict",
                "The idempotency key was already used with a different request.",
            )
        if record and _as_utc(record.expires_at) <= _now():
            await session.delete(record)
            return None
        return record

    async def _idempotency_store(
        self,
        session,
        auth: AuthContext,
        method: str,
        route: str,
        key: str | None,
        fingerprint: str,
        resource_type: str,
        resource_id: str,
    ) -> None:
        if not key:
            return
        now = _now()
        session.add(
            IdempotencyRecord(
                tenant_id=auth.tenant_id,
                subject_id=auth.subject_id,
                method=method,
                route=route,
                key=key,
                fingerprint=fingerprint,
                resource_type=resource_type,
                resource_id=resource_id,
                created_at=now,
                expires_at=now + timedelta(hours=self.settings.api_idempotency_retention_hours),
            )
        )

    def _validate_agent(self, agent_id: str) -> None:
        if agent_id != self.settings.api_agent_id:
            raise ApiProblem(404, "agent_not_found", "Agent not found.")


def _finalize_usage(
    response: ResponseRecord,
    usage: dict[str, Any] | None,
    *,
    now: datetime,
    time_to_first_text_ms: float | None,
    settings: AgentSettings,
    status: str,
) -> dict[str, Any]:
    started_at = _as_utc(response.started_at or now)
    queue_delay_ms = round(
        max(0.0, (started_at - _as_utc(response.created_at)).total_seconds() * 1_000),
        1,
    )
    completion_ms = round(max(0.0, (_as_utc(now) - started_at).total_seconds() * 1_000), 1)
    result = dict(usage or {})
    result["latency"] = {
        "queue_delay_ms": queue_delay_ms,
        "time_to_first_text_ms": time_to_first_text_ms,
        "completion_ms": completion_ms,
    }
    result["slo"] = {
        "objectives_ms": {
            "queue_delay": settings.agent_slo_queue_delay_ms,
            "time_to_first_text": settings.agent_slo_time_to_first_text_ms,
            "completion": settings.agent_slo_completion_ms,
        },
        "met": {
            "queue_delay": queue_delay_ms <= settings.agent_slo_queue_delay_ms,
            "time_to_first_text": (
                time_to_first_text_ms <= settings.agent_slo_time_to_first_text_ms
                if time_to_first_text_ms is not None
                else None
            ),
            "completion": completion_ms <= settings.agent_slo_completion_ms,
        },
    }
    record_agent_response(
        queue_delay_ms=queue_delay_ms,
        time_to_first_text_ms=time_to_first_text_ms,
        completion_ms=completion_ms,
        status=status,
    )
    record_agent_response_event(event="terminal", status=status)
    return result
