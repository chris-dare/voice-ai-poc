from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Metadata = dict[str, str | int | float | bool | None]


class InputText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["input_text"]
    text: str = Field(min_length=1)


class InputMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["message"]
    role: Literal["user"]
    content: list[InputText] = Field(min_length=1)


InputItem = Annotated[InputMessage, Field(discriminator="type")]


class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=120)
    metadata: Metadata = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: Metadata) -> Metadata:
        return _validated_metadata(value)


class ResponseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = Field(default=None, min_length=1, max_length=120)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=80)
    input: str | list[InputItem]
    stream: bool = False
    background: bool = False
    metadata: Metadata = Field(default_factory=dict)

    @field_validator("input")
    @classmethod
    def validate_input(cls, value: str | list[InputItem]) -> str | list[InputItem]:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("input text must not be empty")
            return value
        if not value:
            raise ValueError("input must contain at least one item")
        return value

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: Metadata) -> Metadata:
        return _validated_metadata(value)

    def input_text(self) -> str:
        if isinstance(self.input, str):
            return self.input.strip()
        return "\n".join(
            content.text
            for item in self.input
            for content in item.content
        ).strip()


class RequiredActionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]


class ProblemDetail(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    title: str
    status: int
    detail: str
    instance: str
    code: str
    request_id: str
    errors: list[dict[str, Any]] | None = None


def _validated_metadata(value: Metadata) -> Metadata:
    if len(value) > 32:
        raise ValueError("metadata may contain at most 32 keys")
    encoded_size = 0
    for key, item in value.items():
        if not key or len(key) > 64:
            raise ValueError("metadata keys must contain 1 to 64 characters")
        rendered = "" if item is None else str(item)
        if len(rendered) > 512:
            raise ValueError("metadata values may contain at most 512 characters")
        encoded_size += len(key) + len(rendered)
    if encoded_size > 4_096:
        raise ValueError("metadata is too large")
    return value
