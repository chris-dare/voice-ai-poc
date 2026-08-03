from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy import and_, delete, func, or_
from sqlmodel import select

from voice_ai.agent.api.auth import AuthContext
from voice_ai.agent.api.models import (
    Conversation,
    IdempotencyRecord,
    RequiredAction,
    ResponseEvent,
    ResponseRecord,
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
    request_fingerprint,
    response_repr,
    sse_record,
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
from voice_ai.agent.runtime import AgentRuntime
from voice_ai.shared.config import Settings
from voice_ai.shared.observability import record_agent_response

ACTIVE_STATUSES = frozenset({"queued", "in_progress", "requires_action"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class AgentApiService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        runtime: AgentRuntime,
    ) -> None:
        self.settings = settings
        self.database = database
        self.runtime = runtime
        self.events = EventBroker()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._expiry_tasks: dict[str, asyncio.Task[None]] = {}
        self._response_locks: dict[str, asyncio.Lock] = {}
        self._task_guard = asyncio.Lock()
        self._maintenance_task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        await self._reconcile_interrupted_responses()
        async with self.database.session() as session:
            actions = list(
                (
                    await session.exec(
                        select(RequiredAction).where(
                            RequiredAction.decision.is_(None)
                        )
                    )
                ).all()
            )
        for action in actions:
            await self._schedule_expiry(action.id, action.response_id, action.expires_at)
        self._maintenance_task = asyncio.create_task(
            self._maintenance_loop(),
            name="agent-api-maintenance",
        )

    async def shutdown(self) -> None:
        tasks = [*self._tasks.values(), *self._expiry_tasks.values()]
        if self._maintenance_task:
            tasks.append(self._maintenance_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def create_conversation(
        self,
        auth: AuthContext,
        request: ConversationCreateRequest,
        idempotency_key: str | None,
    ) -> tuple[dict[str, Any], bool]:
        self._validate_agent(request.agent_id)
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

    async def get_conversation(
        self, auth: AuthContext, conversation_id: str
    ) -> dict[str, Any]:
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
                delete(ResponseRecord).where(
                    ResponseRecord.conversation_id == conversation_id
                )
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

            now = _now()
            response = ResponseRecord(
                id=_id("resp"),
                conversation_id=conversation.id if conversation else None,
                runtime_session_id=runtime_session_id,
                tenant_id=auth.tenant_id,
                subject_id=auth.subject_id,
                subscriber_id=None,
                agent_id=agent_id,
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
                metadata = dict(conversation.metadata_json)
                if not metadata.get("title"):
                    metadata["title"] = _conversation_title(request.input_text())
                    conversation.metadata_json = metadata
                conversation.updated_at = now
        await self.events.publish(response.id)
        return response_repr(response), False

    async def get_response(self, auth: AuthContext, response_id: str) -> dict[str, Any]:
        await self._expire_if_needed(response_id)
        async with self.database.session() as session:
            response = await session.get(ResponseRecord, response_id)
            if response is None or not _owns(response, auth):
                raise ApiProblem(404, "not_found", "Response not found.")
            return response_repr(response)

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
        async with self._task_guard:
            existing = self._tasks.get(response_id)
            if existing and not existing.done():
                return
            task = asyncio.create_task(
                self._run_response(response_id, decision=decision),
                name=f"public-response-{response_id}",
            )
            self._tasks[response_id] = task
            task.add_done_callback(lambda _: self._tasks.pop(response_id, None))

    async def wait_for_terminal(self, response_id: str) -> dict[str, Any]:
        task = self._tasks.get(response_id)
        if task:
            await task
        async with self.database.session() as session:
            response = await session.get(ResponseRecord, response_id)
            if response is None:
                raise ApiProblem(404, "not_found", "Response not found.")
            return response_repr(response)

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
        await self._restore_session_for_response(response)
        await self.runtime.clear_confirmation(response.runtime_session_id)
        state = await self.runtime.export_session_state(response.runtime_session_id)
        await self._save_session_state_for_response(response_id, state)
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
            await self._restore_session_for_response(response)
            await self.runtime.clear_confirmation(response.runtime_session_id)
            state = await self.runtime.export_session_state(response.runtime_session_id)
            await self._save_session_state_for_response(response_id, state)
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
        while True:
            observed = await self.events.version(response_id)
            events, status = await self.event_page(auth, response_id, cursor)
            for event in events:
                cursor = int(event["sequence_number"])
                yield sse_record(event)
            if status in TERMINAL_STATUSES or status == "requires_action":
                return
            if not await self.events.wait(response_id, observed, heartbeat_seconds):
                yield ": ping\n\n"

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
                delete(ResponseEvent).where(
                    ResponseEvent.response_id.in_(expired_response_ids)
                )
            )
            await session.exec(
                delete(IdempotencyRecord).where(
                    IdempotencyRecord.expires_at < now
                )
            )

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(3_600)
            await self.prune()

    async def _run_response(self, response_id: str, *, decision: str | None) -> None:
        lock = self._response_locks.setdefault(response_id, asyncio.Lock())
        async with lock:
            try:
                response, session_state = await self._mark_in_progress(response_id)
                execution_started = perf_counter()
                await self.runtime.restore_session_state(
                    response.runtime_session_id, session_state
                )
                text = "yes please" if decision == "approve" else "no" if decision == "reject" else _input_text(response.input_json)
                output_text = ""
                request = AgentTurnRequest(
                    session_id=response.runtime_session_id,
                    text=text,
                )
                tool_runs: dict[str, list[str]] = {}
                tool_activity: list[dict[str, Any]] = []
                turn_completed = False
                turn_usage: dict[str, Any] | None = None
                time_to_first_text_ms: float | None = None
                output_item_id = (
                    f"msg_{response_id.removeprefix('resp_')}"
                    f"{'_resume' if decision else ''}"
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
                            activity_item.update(
                                {"source": event.source, "agent": event.agent}
                            )
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
                        tool_run_id = (
                            pending_runs.pop(0) if pending_runs else _id("toolrun")
                        )
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
                                activity.update(
                                    {"source": event.source, "agent": event.agent}
                                )
                            tool_activity.append(activity)
                        activity["status"] = "succeeded"
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
                        output_text += delta
                        await self._append_event(
                            response_id,
                            "response.output_text.delta",
                            item_id=output_item_id,
                            content_index=0,
                            delta=delta,
                        )
                    elif isinstance(event, AgentError):
                        await self._fail_response(
                            response_id,
                            code="agent_execution_failed",
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
                pending = await self.runtime.pending_confirmation(
                    response.runtime_session_id
                )
                state = await self.runtime.export_session_state(
                    response.runtime_session_id
                )
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

    async def _mark_in_progress(
        self, response_id: str
    ) -> tuple[ResponseRecord, dict[str, Any]]:
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
            await self._append_event_in_session(
                session, response, "response.in_progress", now=now
            )
        await self.events.publish(response_id)
        return response, state

    async def _append_event(
        self, response_id: str, event_type: str, **fields: Any
    ) -> None:
        async with self.database.session_factory.begin() as session:
            response = await session.get(ResponseRecord, response_id, with_for_update=True)
            if response is None or response.status in TERMINAL_STATUSES:
                return
            await self._append_event_in_session(
                session, response, event_type, **fields
            )
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
            response.output_json = _output_items(
                output_item_id, output_text, tool_activity
            )
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
            _as_utc(pending_created_at)
            if isinstance(pending_created_at, datetime)
            else now
        ) + timedelta(seconds=self.settings.confirmation_ttl_seconds)
        action = RequiredAction(
            id=_id("act"),
            response_id=response_id,
            tenant_id=response_snapshot.tenant_id,
            subject_id=response_snapshot.subject_id,
            type="confirmation",
            title=f"Change plan to {pending['plan_name']}",
            description=(
                f"The monthly charge will be {pending['currency']} "
                f"{pending['quoted_price']}."
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
            response.output_json = _output_items(
                output_item_id, output_text, tool_activity
            )
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
        await self._append_event_in_session(
            session,
            response,
            "response.cancelled",
            now=now,
            response=response_repr(response),
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
                await self._persist_conversation_state(
                    session, response, state, _now()
                )

    async def _restore_session_for_response(
        self, response: ResponseRecord
    ) -> None:
        async with self.database.session() as session:
            conversation = (
                await session.get(Conversation, response.conversation_id)
                if response.conversation_id
                else None
            )
            state = conversation.session_state if conversation else response.session_state
        await self.runtime.restore_session_state(response.runtime_session_id, state)

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
        await self._restore_session_for_response(response)
        await self.runtime.clear_confirmation(response.runtime_session_id)
        state = await self.runtime.export_session_state(response.runtime_session_id)
        await self._save_session_state_for_response(response_id, state)
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

    async def _reconcile_interrupted_responses(self) -> None:
        interrupted: list[tuple[str, UUID, dict[str, Any]]] = []
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
                conversation = (
                    await session.get(Conversation, response.conversation_id)
                    if response.conversation_id
                    else None
                )
                interrupted.append(
                    (
                        response.id,
                        response.runtime_session_id,
                        conversation.session_state
                        if conversation
                        else response.session_state,
                    )
                )
                now = _now()
                response.status = "failed"
                response.completed_at = now
                response.error_json = {
                    "code": "server_restarted",
                    "message": "Execution was interrupted by an agent service restart.",
                    "retryable": True,
                }
                await self._append_event_in_session(
                    session,
                    response,
                    "response.failed",
                    now=now,
                    response=response_repr(response),
                )
        for response_id, session_id, state in interrupted:
            await self.runtime.restore_session_state(session_id, state)
            await self.runtime.clear_confirmation(session_id)
            cleared = await self.runtime.export_session_state(session_id)
            await self._save_session_state_for_response(response_id, cleared)

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
                expires_at=now
                + timedelta(hours=self.settings.api_idempotency_retention_hours),
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
    settings: Settings,
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
    return result
