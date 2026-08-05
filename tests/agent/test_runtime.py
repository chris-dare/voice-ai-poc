from uuid import uuid4

import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from voice_ai.agent.protocol import AgentTurnRequest
from voice_ai.agent.runtime import AgentRuntime, current_datetime
from voice_ai.shared.config import Settings


def test_current_datetime_supports_iana_timezones() -> None:
    value = current_datetime("Africa/Accra")

    assert value.endswith("+00:00")


def test_current_datetime_rejects_unknown_timezone() -> None:
    with pytest.raises(ValueError, match="Unknown timezone"):
        current_datetime("Mars/Olympus_Mons")


@pytest.mark.asyncio
async def test_general_question_is_answered_by_the_model() -> None:
    calls = 0

    async def model_stream(_messages, _info: AgentInfo):
        nonlocal calls
        calls += 1
        yield "A concise, useful answer."

    runtime = AgentRuntime(
        Settings(),
        model=FunctionModel(stream_function=model_stream),
    )
    request = AgentTurnRequest(session_id=uuid4(), text="Help me think this through")

    events = [event async for event in runtime.stream_turn(request)]
    await runtime.shutdown()

    assert calls == 1
    assert [event.type for event in events] == [
        "response_started",
        "text_delta",
        "response_completed",
    ]
    assert events[1].text == "A concise, useful answer."
    assert events[-1].usage is not None
    assert events[-1].usage.model_requests == 1
    assert len(events[-1].usage.attempts) == 1
    assert events[-1].usage.attempts[0].agent == "general_assistant"


@pytest.mark.asyncio
async def test_missing_model_credentials_fail_before_a_provider_request() -> None:
    runtime = AgentRuntime(
        Settings(
            _env_file=None,
            agent_model="openrouter:anthropic/claude-sonnet-4.6",
            openrouter_api_key=None,
        )
    )

    events = [
        event
        async for event in runtime.stream_turn(AgentTurnRequest(session_id=uuid4(), text="Hello"))
    ]

    assert [event.type for event in events] == ["response_started", "error"]
    assert "OPENROUTER_API_KEY is required" in events[-1].message
    assert not events[-1].retryable


@pytest.mark.asyncio
async def test_specialist_capabilities_are_progressively_disclosed() -> None:
    visible_tools: dict[str, bool] = {}

    async def model_stream(_messages, info: AgentInfo):
        visible_tools.update({tool.name: tool.defer_loading for tool in info.function_tools})
        yield "A direct answer needs no specialist."

    runtime = AgentRuntime(
        Settings(),
        model=FunctionModel(stream_function=model_stream),
    )
    request = AgentTurnRequest(session_id=uuid4(), text="Say hello")

    _ = [event async for event in runtime.stream_turn(request)]
    await runtime.shutdown()

    assert not visible_tools["load_capability"]
    assert not visible_tools["current_datetime"]
    assert not visible_tools["run_code"]
    assert visible_tools["delegate_task"]
    assert "write_plan" not in visible_tools


@pytest.mark.asyncio
async def test_code_mode_executes_in_monty_and_emits_tool_activity() -> None:
    async def model_stream(messages, _info: AgentInfo):
        if any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield "The answer is 4."
        else:
            yield {
                0: DeltaToolCall(
                    name="run_code",
                    json_args='{"code":"2 + 2"}',
                    tool_call_id="code-1",
                )
            }

    runtime = AgentRuntime(
        Settings(),
        model=FunctionModel(stream_function=model_stream),
    )
    request = AgentTurnRequest(session_id=uuid4(), text="Calculate 2 + 2")

    events = [event async for event in runtime.stream_turn(request)]
    await runtime.shutdown()

    assert [event.type for event in events] == [
        "response_started",
        "tool_started",
        "tool_completed",
        "text_delta",
        "response_completed",
    ]
    assert events[1].tool == "run_code"
    assert events[1].label == "Running code"
    assert events[3].text == "The answer is 4."


@pytest.mark.asyncio
async def test_code_mode_cannot_read_provider_credentials(monkeypatch) -> None:
    secret = "must-not-enter-the-monty-sandbox"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    sandbox_results: list[str] = []

    async def model_stream(messages, _info: AgentInfo):
        results = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, (ToolReturnPart, RetryPromptPart)) and part.tool_name == "run_code"
        ]
        if results:
            sandbox_results.extend(repr(part) for part in results)
            yield "The sandbox could not access the credential."
        else:
            yield {
                0: DeltaToolCall(
                    name="run_code",
                    json_args=('{"code":"import os\\nos.getenv(\\"OPENROUTER_API_KEY\\")"}'),
                    tool_call_id="secret-probe",
                )
            }

    runtime = AgentRuntime(
        Settings(),
        model=FunctionModel(stream_function=model_stream),
    )
    events = [
        event
        async for event in runtime.stream_turn(
            AgentTurnRequest(session_id=uuid4(), text="Read the provider key")
        )
    ]
    await runtime.shutdown()

    assert sandbox_results
    assert secret not in "".join(sandbox_results)
    assert events[-2].text == "The sandbox could not access the credential."


@pytest.mark.asyncio
async def test_deep_agent_streams_nested_specialist_tool_activity() -> None:
    async def model_stream(messages, info: AgentInfo):
        tool_returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if info.instructions and "Analyze the self-contained task" in info.instructions:
            if any(part.tool_name == "run_code" for part in tool_returns):
                yield "The verified result is 4."
            else:
                yield {
                    0: DeltaToolCall(
                        name="run_code",
                        json_args='{"code":"2 + 2"}',
                        tool_call_id="child-code-1",
                    )
                }
            return

        if any(part.tool_name == "delegate_task" for part in tool_returns):
            yield "The specialist verified that the answer is 4."
            return
        delegate = next(tool for tool in info.function_tools if tool.name == "delegate_task")
        if not delegate.defer_loading:
            yield {
                0: DeltaToolCall(
                    name="delegate_task",
                    json_args=('{"agent_name":"analyst","task":"Use code to calculate 2 + 2."}'),
                    tool_call_id="delegate-1",
                )
            }
        else:
            yield {
                0: DeltaToolCall(
                    name="load_capability",
                    json_args='{"id":"deep_work"}',
                    tool_call_id="load-1",
                )
            }

    runtime = AgentRuntime(
        Settings(),
        model=FunctionModel(stream_function=model_stream),
    )
    events = [
        event
        async for event in runtime.stream_turn(
            AgentTurnRequest(session_id=uuid4(), text="Calculate 2 + 2 carefully")
        )
    ]
    await runtime.shutdown()

    nested_code = [
        event
        for event in events
        if event.type in {"tool_started", "tool_completed"} and event.tool == "run_code"
    ]
    assert [event.type for event in nested_code] == ["tool_started", "tool_completed"]
    assert all(event.source == "subagent" for event in nested_code)
    assert all(event.agent == "analyst" for event in nested_code)
    assert any(event.type == "tool_started" and event.tool == "delegate_task" for event in events)
    assert events[-2].text == "The specialist verified that the answer is 4."
    assert events[-1].usage is not None
    assert {attempt.agent for attempt in events[-1].usage.attempts} == {
        "analyst",
        "general_assistant",
    }
    assert events[-1].usage.model_requests == len(events[-1].usage.attempts)


@pytest.mark.asyncio
async def test_unfinished_tool_promise_fails_closed() -> None:
    async def model_stream(_messages, _info: AgentInfo):
        yield "I will search for that information."

    runtime = AgentRuntime(
        Settings(),
        model=FunctionModel(stream_function=model_stream),
    )
    request = AgentTurnRequest(session_id=uuid4(), text="Find the latest release")

    events = [event async for event in runtime.stream_turn(request)]
    await runtime.shutdown()

    assert not any(event.type == "text_delta" for event in events)
    assert events[-1].type == "error"


@pytest.mark.asyncio
async def test_unevaluated_tool_code_fails_closed() -> None:
    async def model_stream(_messages, _info: AgentInfo):
        yield 'await current_datetime(timezone="Africa/Accra")'

    runtime = AgentRuntime(
        Settings(),
        model=FunctionModel(stream_function=model_stream),
    )
    request = AgentTurnRequest(session_id=uuid4(), text="What time is it in Accra?")

    events = [event async for event in runtime.stream_turn(request)]
    await runtime.shutdown()

    assert not any(event.type == "text_delta" for event in events)
    assert events[-1].type == "error"


@pytest.mark.asyncio
async def test_agent_session_state_round_trips_for_durable_conversations() -> None:
    async def model_stream(_messages, _info: AgentInfo):
        yield "Hello from the agent."

    runtime = AgentRuntime(
        Settings(),
        model=FunctionModel(stream_function=model_stream),
    )
    session_id = uuid4()
    request = AgentTurnRequest(session_id=session_id, text="Hello")

    _ = [event async for event in runtime.stream_turn(request)]
    state = await runtime.export_session_state(session_id)

    assert len(state["messages"]) >= 2
    await runtime.close_session(session_id)
    await runtime.restore_session_state(session_id, state)
    restored = await runtime.export_session_state(session_id)
    await runtime.shutdown()
    assert restored == state


@pytest.mark.asyncio
async def test_anthropic_thinking_and_tool_parts_survive_history_round_trip() -> None:
    async def model_stream(_messages, _info: AgentInfo):
        yield "unused"

    runtime = AgentRuntime(
        Settings(),
        model=FunctionModel(stream_function=model_stream),
    )
    session_id = uuid4()
    messages = [
        ModelResponse(
            parts=[
                ThinkingPart(
                    content="private reasoning summary",
                    signature="signed-thinking-block",
                    provider_name="anthropic",
                ),
                ToolCallPart(
                    tool_name="current_datetime",
                    args={"timezone": "UTC"},
                    tool_call_id="time-1",
                ),
            ],
            model_name="claude-sonnet-4-6",
            provider_name="anthropic",
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="current_datetime",
                    content="2026-08-02T00:00:00+00:00",
                    tool_call_id="time-1",
                )
            ]
        ),
    ]
    state = {"messages": ModelMessagesTypeAdapter.dump_python(messages, mode="json")}

    await runtime.restore_session_state(session_id, state)
    restored = await runtime.export_session_state(session_id)
    await runtime.shutdown()

    assert restored == state
    thinking = restored["messages"][0]["parts"][0]
    assert thinking["signature"] == "signed-thinking-block"
    assert thinking["provider_name"] == "anthropic"
