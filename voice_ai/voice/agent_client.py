from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any
from uuid import UUID, uuid4

import httpx
from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings

from voice_ai.agent.protocol import AgentTurnRequest


class RemoteAgentLLMService(LLMService):
    """Pipecat LLM seam backed by the independently deployed agent service."""

    def __init__(
        self,
        *,
        base_url: str,
        session_id: UUID,
        shared_secret: str | None,
        on_event,
        public_access_token: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        super().__init__(
            settings=LLMSettings(
                model="remote-pydantic-agent",
                system_instruction=None,
                temperature=None,
                max_tokens=None,
                top_p=None,
                top_k=None,
                frequency_penalty=None,
                presence_penalty=None,
                seed=None,
                filter_incomplete_user_turns=False,
                user_turn_completion_config=None,
            )
        )
        self._base_url = base_url.rstrip("/")
        self._session_id = session_id
        self._on_event = on_event
        self._conversation_id = conversation_id
        self._uses_authenticated_api = bool(public_access_token and conversation_id)
        self._pending_action: dict[str, str] | None = None
        self._active_response_id: str | None = None
        self._response_start_lock = asyncio.Lock()
        credential = public_access_token if self._uses_authenticated_api else shared_secret
        headers = {"Authorization": f"Bearer {credential}"} if credential else {}
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(90, connect=5), headers=headers)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        await self.push_frame(LLMFullResponseStartFrame())
        await self.start_processing_metrics()
        await self.start_ttfb_metrics()
        first_text = True
        try:
            text = _last_user_text(frame.context.get_messages())
            if self._uses_authenticated_api:
                first_text = await self._process_public_turn(text)
            else:
                first_text = await self._process_private_turn(text, first_text)
        except httpx.TimeoutException as exc:
            await self._call_event_handler("on_completion_timeout")
            await self.push_error(error_msg="Agent completion timeout", exception=exc)
        except Exception as exc:
            logger.exception("Remote agent request failed")
            await self.push_error(error_msg=f"Agent request failed: {exc}", exception=exc)
        finally:
            if first_text:
                await self.stop_ttfb_metrics()
            await self.stop_processing_metrics()
            await self.push_frame(LLMFullResponseEndFrame())

    async def _process_private_turn(self, text: str, first_text: bool) -> bool:
        payload = AgentTurnRequest(
            session_id=self._session_id,
            text=text,
        )
        async with self._client.stream(
            "POST",
            f"{self._base_url}/v1/turns/stream",
            json=payload.model_dump(mode="json"),
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                event = json.loads(line)
                event_type = event.get("type")
                if event_type == "text_delta" and event.get("text"):
                    if first_text:
                        await self.stop_ttfb_metrics()
                        first_text = False
                    await self._push_llm_text(str(event["text"]))
                elif event_type in {"tool_started", "tool_completed"}:
                    await self._on_event(_tool_message(event))
                elif event_type == "error":
                    raise RuntimeError(str(event.get("message") or "Agent request failed"))
        return first_text

    async def _process_public_turn(self, text: str) -> bool:
        if self._pending_action:
            return await self._resume_public_action(text)

        stream_context = self._client.stream(
            "POST",
            f"{self._base_url}/v1/responses",
            headers={
                "Accept": "text/event-stream",
                "Idempotency-Key": uuid4().hex,
            },
            json={
                "conversation_id": self._conversation_id,
                "input": text,
                "stream": True,
                "background": True,
                "metadata": {"channel": "voice"},
            },
        )
        async with self._response_start_lock:
            await self._cancel_active_response()
            response = await stream_context.__aenter__()
            try:
                response.raise_for_status()
            except BaseException:
                await stream_context.__aexit__(*sys.exc_info())
                raise
            response_id = response.headers.get("X-Response-Id")
            self._active_response_id = response_id
        try:
            return await self._consume_public_events(response, response_id=response_id)
        finally:
            await stream_context.__aexit__(None, None, None)
            if response_id and self._active_response_id == response_id:
                await asyncio.shield(self._cancel_response(response_id))

    async def _resume_public_action(self, text: str) -> bool:
        pending = self._pending_action
        if pending is None:
            return True
        decision = _confirmation_decision(text)
        if decision is None:
            decision = "reject"
        response = await self._client.post(
            (
                f"{self._base_url}/v1/responses/{pending['response_id']}"
                f"/actions/{pending['action_id']}"
            ),
            headers={"Idempotency-Key": uuid4().hex},
            json={"decision": decision},
        )
        response.raise_for_status()
        response_id = pending["response_id"]
        try:
            async with self._client.stream(
                "GET",
                f"{self._base_url}/v1/responses/{response_id}/events",
                headers={
                    "Accept": "text/event-stream",
                    "Last-Event-ID": pending["last_event_id"],
                },
            ) as event_response:
                event_response.raise_for_status()
                self._active_response_id = response_id
                return await self._consume_public_events(
                    event_response,
                    response_id=response_id,
                )
        finally:
            if self._active_response_id == response_id:
                await asyncio.shield(self._cancel_response(response_id))

    async def _consume_public_events(
        self,
        response: httpx.Response,
        *,
        response_id: str | None,
    ) -> bool:
        last_event_id = "0"
        first_text = True
        async for line in response.aiter_lines():
            if line.startswith("id:"):
                last_event_id = line[3:].strip()
                continue
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip())
            event_type = event.get("type")
            if event_type == "response.created":
                created = event.get("response") or {}
                response_id = str(event.get("response_id") or created.get("id") or "") or None
                self._active_response_id = response_id
            elif event_type == "response.output_text.delta" and event.get("delta"):
                if first_text:
                    await self.stop_ttfb_metrics()
                    first_text = False
                await self._push_llm_text(str(event["delta"]))
            elif event_type in {"response.tool.started", "response.tool.completed"}:
                await self._on_event(_public_tool_message(event))
            elif event_type == "response.requires_action":
                action = event.get("required_action") or {}
                self._pending_action = {
                    "response_id": str(event["response_id"]),
                    "action_id": str(action["id"]),
                    "last_event_id": last_event_id,
                }
                self._clear_active_response(response_id)
            elif event_type == "response.completed":
                self._pending_action = None
                self._clear_active_response(response_id)
            elif event_type == "response.failed":
                self._clear_active_response(response_id)
                error = (event.get("response") or {}).get("error") or {}
                raise RuntimeError(str(error.get("message") or "Agent request failed"))
            elif event_type == "response.cancelled":
                self._clear_active_response(response_id)
                return first_text
        return first_text

    async def _cancel_active_response(self) -> None:
        response_id = self._active_response_id
        if response_id:
            await self._cancel_response(response_id)

    async def _cancel_response(self, response_id: str) -> None:
        try:
            response = await self._client.post(
                f"{self._base_url}/v1/responses/{response_id}/cancel",
                headers={"Idempotency-Key": uuid4().hex},
            )
            if response.status_code != 404:
                response.raise_for_status()
        finally:
            self._clear_active_response(response_id)

    def _clear_active_response(self, response_id: str | None) -> None:
        if response_id and self._active_response_id == response_id:
            self._active_response_id = None

    async def cleanup(self) -> None:
        if self._uses_authenticated_api:
            try:
                await self._cancel_active_response()
            except Exception:
                logger.debug("Active public response cleanup failed", exc_info=True)
        if not self._uses_authenticated_api:
            try:
                await self._client.delete(f"{self._base_url}/v1/sessions/{self._session_id}")
            except Exception:
                logger.debug("Remote agent session cleanup failed", exc_info=True)
        await self._client.aclose()
        await super().cleanup()


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text = " ".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ).strip()
            if text:
                return text
    raise ValueError("No user transcript was available for the agent")


def _tool_message(event: dict[str, Any]) -> dict[str, Any]:
    completed = event["type"] == "tool_completed"
    return {
        "type": "tool-activity",
        "data": {
            "tool": event.get("tool"),
            "label": event.get("label"),
            "status": "completed" if completed else "running",
            "detail": event.get("detail") or ("Completed" if completed else "Working securely…"),
            "source": event.get("source", "root"),
            "agent": event.get("agent"),
        },
    }


def _public_tool_message(event: dict[str, Any]) -> dict[str, Any]:
    completed = event["type"] == "response.tool.completed"
    return {
        "type": "tool-activity",
        "data": {
            "tool": event.get("name"),
            "label": event.get("label"),
            "status": "completed" if completed else "running",
            "detail": event.get("detail") or ("Completed" if completed else "Working securely…"),
            "source": event.get("source", "root"),
            "agent": event.get("agent"),
        },
    }


def _confirmation_decision(text: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9' ]+", "", " ".join(text.lower().split())).strip()
    if normalized in {
        "yes",
        "yes please",
        "i confirm",
        "confirm",
        "go ahead",
        "please do",
        "do it",
        "that is correct",
        "that's correct",
    }:
        return "approve"
    if normalized in {
        "no",
        "no thanks",
        "cancel",
        "stop",
        "never mind",
        "nevermind",
        "do not",
        "don't",
    }:
        return "reject"
    return None
