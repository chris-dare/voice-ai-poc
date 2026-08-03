import pytest
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel

from voice_ai.agent.models import check_model_readiness, resolve_model
from voice_ai.agent.ollama import NativeOllamaModel
from voice_ai.shared.config import Settings


@pytest.mark.asyncio
async def test_anthropic_model_is_selected_from_configuration() -> None:
    settings = Settings(
        _env_file=None,
        agent_model="anthropic:claude-sonnet-4-6",
        anthropic_api_key="test-key",
    )
    selection = resolve_model(settings)
    await selection.model.__aenter__()
    try:
        readiness = await check_model_readiness(settings, selection)
    finally:
        await selection.model.__aexit__(None, None, None)

    assert isinstance(selection.model, AnthropicModel)
    assert selection.provider == "anthropic"
    assert readiness.ready
    assert "billable call" in readiness.detail


@pytest.mark.asyncio
async def test_missing_provider_credential_fails_readiness_without_crashing_startup() -> None:
    settings = Settings(
        _env_file=None,
        agent_model="anthropic:claude-sonnet-4-6",
        anthropic_api_key=None,
    )
    selection = resolve_model(settings)
    await selection.model.__aenter__()
    try:
        readiness = await check_model_readiness(settings, selection)
    finally:
        await selection.model.__aexit__(None, None, None)

    assert not readiness.ready
    assert readiness.detail == "ANTHROPIC_API_KEY is required for the configured model"


def test_openai_compatible_gateway_is_configuration_only() -> None:
    selection = resolve_model(
        Settings(
            _env_file=None,
            agent_model="openai-chat:gateway/assistant",
            agent_gateway_base_url="https://gateway.example/v1",
            agent_gateway_api_key="gateway-key",
        )
    )

    assert isinstance(selection.model, OpenAIChatModel)
    assert selection.gateway
    assert selection.provider == "openai-compatible"


def test_gateway_can_route_a_fallback_without_capturing_the_primary() -> None:
    selection = resolve_model(
        Settings(
            _env_file=None,
            agent_model="anthropic:claude-sonnet-4-6",
            agent_fallback_models=["openai-chat:gateway/fallback"],
            agent_gateway_base_url="https://gateway.example/v1",
            agent_gateway_api_key="gateway-key",
            anthropic_api_key="anthropic-key",
        )
    )

    assert isinstance(selection.model, FallbackModel)
    assert isinstance(selection.model.models[0], AnthropicModel)
    assert isinstance(selection.model.models[1], OpenAIChatModel)
    assert not selection.gateway
    assert not selection.configuration_errors


def test_ollama_remains_an_explicit_provider_option() -> None:
    selection = resolve_model(
        Settings(_env_file=None, agent_model="ollama:qwen3:1.7b")
    )

    assert isinstance(selection.model, NativeOllamaModel)
    assert selection.provider == "ollama"
