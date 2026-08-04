from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any, cast
from uuid import uuid4

import httpx
from openai.types.chat import ChatCompletionChunk
from pydantic_ai import ModelSettings, RunContext
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.providers.ollama import OllamaProvider


class NativeOllamaModel(OllamaModel):
    """Pydantic AI Ollama model using native streaming so ``think:false`` is honored."""

    def __init__(
        self,
        model_name: str,
        *,
        base_url: str,
        settings: ModelSettings | None = None,
    ) -> None:
        self._native_base_url = base_url.rstrip("/")
        super().__init__(
            model_name,
            provider=OllamaProvider(base_url=f"{self._native_base_url}/v1"),
            settings=settings,
        )

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        model_settings, model_request_parameters = self.prepare_request(
            model_settings,
            model_request_parameters,
        )
        settings = cast(OpenAIChatModelSettings, model_settings or {})
        tools, _tool_choice = self._get_tool_choice(settings, model_request_parameters)
        openai_messages = await self._map_messages(
            messages,
            model_request_parameters,
            model_settings=settings,
        )
        payload: dict[str, Any] = {
            "model": self.model_name,
            "stream": True,
            "think": False,
            "messages": _native_messages(openai_messages),
            "options": _native_options(settings),
        }
        if tools:
            payload["tools"] = tools

        native_stream = _NativeChunkStream(
            base_url=self._native_base_url,
            payload=payload,
        )
        async with native_stream:
            yield await self._process_streamed_response(
                native_stream,  # type: ignore[arg-type]
                model_request_parameters,
                settings,
            )


class _NativeChunkStream:
    def __init__(self, *, base_url: str, payload: dict[str, Any]) -> None:
        self._base_url = base_url
        self._payload = payload
        self._generator = self._stream()

    def __aiter__(self):
        return self._generator

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self._generator.aclose()

    async def _stream(self) -> AsyncGenerator[ChatCompletionChunk, None]:
        request_id = f"chatcmpl-{uuid4().hex}"
        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5, read=30, write=30, pool=5)
            ) as client,
            client.stream(
                "POST",
                f"{self._base_url}/api/chat",
                json=self._payload,
            ) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                data = json.loads(line)
                if error := data.get("error"):
                    raise RuntimeError(f"Ollama inference failed: {error}")

                message = data.get("message") or {}
                if content := message.get("content"):
                    yield _completion_chunk(
                        request_id=request_id,
                        model=str(data.get("model") or self._payload["model"]),
                        delta={"content": content},
                    )
                for index, tool_call in enumerate(message.get("tool_calls") or []):
                    function = tool_call.get("function") or {}
                    yield _completion_chunk(
                        request_id=request_id,
                        model=str(data.get("model") or self._payload["model"]),
                        delta={
                            "tool_calls": [
                                {
                                    "index": int(function.get("index", index)),
                                    "id": tool_call.get("id") or f"call_{uuid4().hex[:12]}",
                                    "type": "function",
                                    "function": {
                                        "name": function.get("name", ""),
                                        "arguments": json.dumps(function.get("arguments") or {}),
                                    },
                                }
                            ]
                        },
                    )
                if data.get("done"):
                    prompt_tokens = int(data.get("prompt_eval_count") or 0)
                    completion_tokens = int(data.get("eval_count") or 0)
                    yield _completion_chunk(
                        request_id=request_id,
                        model=str(data.get("model") or self._payload["model"]),
                        usage={
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "total_tokens": prompt_tokens + completion_tokens,
                        },
                    )
                    # Ollama's semantic stream is complete even if an HTTP connection
                    # remains open. Do not wait indefinitely for transport EOF.
                    return


def _native_messages(messages: list[Any]) -> list[dict[str, Any]]:
    normalized = cast(list[dict[str, Any]], deepcopy(messages))
    for message in normalized:
        content = message.get("content")
        if isinstance(content, list) and all(
            isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
            for part in content
        ):
            message["content"] = "".join(str(part["text"]) for part in content)
        for tool_call in message.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    function["arguments"] = json.loads(arguments)
                except json.JSONDecodeError:
                    function["arguments"] = {}
    return normalized


def _native_options(settings: ModelSettings) -> dict[str, int | float]:
    options: dict[str, int | float] = {}
    mappings = (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("seed", "seed"),
        ("max_tokens", "num_predict"),
    )
    for source, target in mappings:
        value = settings.get(source)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            options[target] = value
    return options


def _completion_chunk(
    *,
    request_id: str,
    model: str,
    delta: dict[str, Any] | None = None,
    usage: dict[str, int] | None = None,
) -> ChatCompletionChunk:
    return ChatCompletionChunk.model_validate(
        {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": (
                [{"index": 0, "delta": delta, "finish_reason": None}] if delta is not None else []
            ),
            "usage": usage,
        }
    )
