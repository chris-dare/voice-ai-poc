from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from voice_ai.agent.api.auth import AuthContext
from voice_ai.agent.api.models import Conversation, RequiredAction, ResponseRecord
from voice_ai.agent.api.schemas import ResponseCreateRequest


class ApiProblem(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        *,
        title: str | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.title = title or _status_title(status_code)
        self.extensions = extensions or {}


def conversation_repr(conversation: Conversation) -> dict[str, Any]:
    return {
        "id": conversation.id,
        "object": "conversation",
        "agent_id": conversation.agent_id,
        "model": conversation.model_id,
        "status": conversation.status,
        "created_at": _timestamp(conversation.created_at),
        "updated_at": _timestamp(conversation.updated_at),
        "metadata": conversation.metadata_json,
    }


def response_repr(response: ResponseRecord) -> dict[str, Any]:
    return {
        "id": response.id,
        "object": "response",
        "agent_id": response.agent_id,
        "model": response.model_id,
        "conversation_id": response.conversation_id,
        "status": response.status,
        "created_at": _timestamp(response.created_at),
        "started_at": _timestamp(response.started_at),
        "completed_at": _timestamp(response.completed_at),
        "cancellation_reason": response.cancellation_reason,
        "input": response.input_json,
        "output": response.output_json,
        "required_action": response.required_action_json,
        "error": response.error_json,
        "usage": response.usage_json,
        "metadata": response.metadata_json,
    }


def action_repr(action: RequiredAction) -> dict[str, Any]:
    return {
        "id": action.id,
        "type": action.type,
        "title": action.title,
        "description": action.description,
        "expires_at": _timestamp(action.expires_at),
    }


def request_fingerprint(value: Any) -> str:
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _conversation_title(value: str) -> str:
    title = " ".join(value.split())
    return title if len(title) <= 72 else f"{title[:69].rstrip()}…"


def _encode_cursor(kind: str, created_at: datetime, resource_id: str) -> str:
    payload = json.dumps(
        {"v": 1, "kind": kind, "at": _as_utc(created_at).isoformat(), "id": resource_id},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str, expected_kind: str) -> tuple[datetime, str]:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if not isinstance(payload, dict):
            raise ValueError
        created_at = _as_utc(datetime.fromisoformat(payload["at"]))
        resource_id = str(payload["id"])
        if payload.get("v") != 1 or payload.get("kind") != expected_kind or not resource_id:
            raise ValueError
        return created_at, resource_id
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiProblem(400, "invalid_cursor", "The pagination cursor is invalid.") from exc


def sse_record(event: dict[str, Any]) -> str:
    sequence = int(event["sequence_number"])
    payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    return f"id: {sequence}\nevent: {event['type']}\ndata: {payload}\n\n"


def _event_data(
    event_type: str,
    response_id: str,
    sequence_number: int,
    created_at: datetime,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "type": event_type,
        "sequence_number": sequence_number,
        "response_id": response_id,
        "created_at": _timestamp(created_at),
        **fields,
    }


def _normalized_input(request: ResponseCreateRequest) -> list[dict[str, Any]]:
    if isinstance(request.input, str):
        return [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": request.input.strip()}],
            }
        ]
    return [item.model_dump(mode="json") for item in request.input]


def _input_text(items: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(content.get("text") or "")
        for item in items
        for content in item.get("content") or []
        if isinstance(content, dict) and content.get("type") == "input_text"
    ).strip()


def _output_items(
    item_id: str,
    text: str,
    tool_activity: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if text:
        items.append(
            {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    items.extend(tool_activity or [])
    return items


def _approval_prompt(pending: dict[str, Any]) -> str:
    plan_name = str(pending.get("plan_name") or "The selected plan")
    currency = str(pending.get("currency") or "").strip()
    price = str(pending.get("quoted_price") or "").strip()
    amount = " ".join(part for part in (currency, price) if part)
    price_text = f" costs {amount} per month" if amount else ""
    return f"{plan_name}{price_text}. Approval is required before I make this change."


def _owns(resource: Any, auth: AuthContext) -> bool:
    return resource.tenant_id == auth.tenant_id and resource.subject_id == auth.subject_id


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _status_title(status_code: int) -> str:
    return {
        400: "Bad request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not found",
        406: "Not acceptable",
        409: "Conflict",
        413: "Content too large",
        415: "Unsupported media type",
        422: "Invalid request",
        429: "Too many requests",
        500: "Internal server error",
        503: "Service unavailable",
    }.get(status_code, "Request failed")
