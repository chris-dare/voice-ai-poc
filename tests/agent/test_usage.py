from decimal import Decimal

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.usage import RequestUsage, RunUsage

from voice_ai.agent.usage import UsageTracker


def test_usage_tracker_aggregates_provider_usage_and_estimated_cost() -> None:
    tracker = UsageTracker()
    tracker.completed(
        ModelResponse(
            parts=[TextPart(content="A useful response")],
            usage=RequestUsage(
                input_tokens=1_000,
                output_tokens=100,
                cache_read_tokens=250,
            ),
            model_name="claude-sonnet-4-6",
            provider_name="anthropic",
        ),
        agent="general_assistant",
        duration_ms=125.4,
    )

    summary = tracker.summary(
        usage=RunUsage(
            requests=1,
            tool_calls=2,
            input_tokens=1_000,
            output_tokens=100,
            cache_read_tokens=250,
        ),
        wall_clock_ms=140.0,
        route_model="anthropic:claude-sonnet-4-6",
        route_provider="anthropic",
        gateway=False,
        fallback_models=(),
        observed_tool_calls=2,
    )

    assert summary.total_tokens == 1_100
    assert summary.model_requests == 1
    assert summary.tool_calls == 2
    assert summary.cache_read_tokens == 250
    assert summary.actual_models == ["claude-sonnet-4-6"]
    assert summary.cost_status == "estimated"
    assert summary.cost_source == "genai-prices"
    assert Decimal(summary.estimated_cost_usd or "0") > 0


def test_openrouter_reported_cost_is_primary_and_reasoning_is_visible() -> None:
    tracker = UsageTracker()
    tracker.completed(
        ModelResponse(
            parts=[TextPart(content="A considered response")],
            usage=RequestUsage(
                input_tokens=500,
                output_tokens=80,
                details={"reasoning_tokens": 42},
            ),
            model_name="openai/gpt-5.6-luna",
            provider_name="openrouter",
            provider_response_id="gen-test-123",
            provider_details={
                "cost": 0.0012345,
                "downstream_provider": "openai",
                "is_byok": False,
            },
        ),
        agent="general_assistant",
        duration_ms=90,
    )

    summary = tracker.summary(
        usage=RunUsage(requests=1, input_tokens=500, output_tokens=80),
        wall_clock_ms=100,
        route_model="openrouter:openai/gpt-5.6-luna",
        route_provider="openrouter",
        gateway=True,
        fallback_models=(),
        observed_tool_calls=0,
    )

    assert summary.reported_cost_usd == "0.001234500000"
    assert summary.reasoning_tokens == 42
    assert summary.cost_status == "reported"
    assert summary.cost_source == "openrouter"
    assert summary.attempts[0].downstream_provider == "openai"
    assert summary.attempts[0].is_byok is False
    assert summary.attempts[0].provider_response_id == "gen-test-123"


def test_attempt_accounting_includes_isolated_subagent_usage() -> None:
    tracker = UsageTracker()
    for agent, input_tokens, output_tokens in (
        ("general_assistant", 100, 20),
        ("researcher", 400, 80),
    ):
        tracker.completed(
            ModelResponse(
                parts=[TextPart(content="response")],
                usage=RequestUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
                model_name="test-model",
                provider_name="test",
            ),
            agent=agent,
            duration_ms=10,
        )

    summary = tracker.summary(
        # The root RunUsage deliberately excludes the isolated child budget.
        usage=RunUsage(requests=1, input_tokens=100, output_tokens=20),
        wall_clock_ms=25,
        route_model="test:model",
        route_provider="test",
        gateway=False,
        fallback_models=(),
        observed_tool_calls=1,
    )

    assert summary.total_tokens == 600
    assert summary.model_requests == 2


def test_unknown_model_preserves_usage_and_marks_cost_unavailable() -> None:
    tracker = UsageTracker()
    tracker.completed(
        ModelResponse(
            parts=[TextPart(content="Test response")],
            usage=RequestUsage(input_tokens=8, output_tokens=2),
            model_name="private-model-v1",
            provider_name="private-gateway",
        ),
        agent="general_assistant",
        duration_ms=10,
    )

    summary = tracker.summary(
        usage=None,
        wall_clock_ms=12,
        route_model="openai-chat:private-model-v1",
        route_provider="openai-compatible",
        gateway=True,
        fallback_models=(),
        observed_tool_calls=0,
    )

    assert summary.total_tokens == 10
    assert summary.estimated_cost_usd is None
    assert summary.cost_status == "unavailable"
    assert summary.gateway is True
