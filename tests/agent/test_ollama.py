from __future__ import annotations

import json

from voice_ai.agent.ollama import _completion_chunk, _native_messages, _native_options


def test_native_messages_convert_tool_arguments_to_objects() -> None:
    original = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "run_code",
                        "arguments": '{"code": "2 + 2"}',
                    },
                }
            ],
        }
    ]

    normalized = _native_messages(original)

    assert normalized[0]["tool_calls"][0]["function"]["arguments"] == {"code": "2 + 2"}
    assert original[0]["tool_calls"][0]["function"]["arguments"] == '{"code": "2 + 2"}'


def test_native_messages_flatten_openai_text_parts_for_ollama() -> None:
    original = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Plan reminder: "},
                {"type": "text", "text": "one step remains."},
            ],
        }
    ]

    normalized = _native_messages(original)

    assert normalized[0]["content"] == "Plan reminder: one step remains."
    assert isinstance(original[0]["content"], list)


def test_native_options_keep_only_supported_numeric_settings() -> None:
    assert _native_options(
        {
            "temperature": 0.15,
            "top_p": None,
            "seed": 20,
            "max_tokens": 80,
            "thinking": False,
        }
    ) == {
        "temperature": 0.15,
        "seed": 20,
        "num_predict": 80,
    }


def test_completion_chunk_preserves_streamed_tool_call() -> None:
    chunk = _completion_chunk(
        request_id="chatcmpl-test",
        model="qwen3:1.7b",
        delta={
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "run_code",
                        "arguments": json.dumps({}),
                    },
                }
            ]
        },
    )

    tool_call = chunk.choices[0].delta.tool_calls[0]
    assert tool_call.function.name == "run_code"
    assert tool_call.function.arguments == "{}"
