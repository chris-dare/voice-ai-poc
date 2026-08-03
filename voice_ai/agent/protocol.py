from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from voice_ai.agent.usage import TurnUsage


class AgentTurnRequest(BaseModel):
    session_id: UUID
    text: str = Field(min_length=1, max_length=4_000)
    turn_id: UUID = Field(default_factory=uuid4)


class ResponseStarted(BaseModel):
    type: Literal["response_started"] = "response_started"
    turn_id: UUID


class TextDelta(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class ToolStarted(BaseModel):
    type: Literal["tool_started"] = "tool_started"
    tool: str
    label: str
    source: Literal["root", "subagent"] = "root"
    agent: str | None = None
    tool_call_id: str | None = None


class ToolCompleted(BaseModel):
    type: Literal["tool_completed"] = "tool_completed"
    tool: str
    label: str
    detail: str = "Completed"
    source: Literal["root", "subagent"] = "root"
    agent: str | None = None
    tool_call_id: str | None = None


class ResponseCompleted(BaseModel):
    type: Literal["response_completed"] = "response_completed"
    turn_id: UUID
    latency_ms: float
    usage: TurnUsage | None = None


class AgentError(BaseModel):
    type: Literal["error"] = "error"
    message: str
    retryable: bool = False
    usage: TurnUsage | None = None


AgentEvent = Annotated[
    ResponseStarted
    | TextDelta
    | ToolStarted
    | ToolCompleted
    | ResponseCompleted
    | AgentError,
    Field(discriminator="type"),
]


TOOL_LABELS = {
    "duckduckgo_search": "Searching the web",
    "web_fetch": "Reading a web page",
    "run_code": "Running code",
    "load_capability": "Loading specialist capability",
    "write_plan": "Updating task plan",
    "current_datetime": "Checking the time",
    "delegate_task": "Working with a specialist",
    "web_search": "Searching the web",
}


def tool_label(name: str) -> str:
    return TOOL_LABELS.get(name, name.replace("_", " ").title())
