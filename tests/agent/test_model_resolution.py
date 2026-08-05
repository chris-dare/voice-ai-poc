import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openrouter import OpenRouterModel

from voice_ai.agent.models import (
    check_model_readiness,
    classify_model_failure,
    resolve_model,
)
from voice_ai.agent.ollama import NativeOllamaModel
from voice_ai.agent.runtime import _specialist_model_settings
from voice_ai.shared.config import Settings


@pytest.mark.asyncio
async def test_openrouter_anthropic_model_is_selected_from_configuration() -> None:
    settings = Settings(
        _env_file=None,
        agent_model="openrouter:anthropic/claude-sonnet-4.6",
        openrouter_api_key="test-key",
    )
    selection = resolve_model(settings)
    await selection.model.__aenter__()
    try:
        readiness = await check_model_readiness(settings, selection)
    finally:
        await selection.model.__aexit__(None, None, None)

    assert isinstance(selection.model, OpenRouterModel)
    assert selection.provider == "openrouter"
    assert selection.gateway
    assert readiness.ready


@pytest.mark.asyncio
async def test_missing_openrouter_credential_fails_readiness() -> None:
    settings = Settings(
        _env_file=None,
        agent_model="openrouter:anthropic/claude-sonnet-4.6",
        openrouter_api_key=None,
    )
    selection = resolve_model(settings)
    await selection.model.__aenter__()
    try:
        readiness = await check_model_readiness(settings, selection)
    finally:
        await selection.model.__aexit__(None, None, None)

    assert not readiness.ready
    assert readiness.detail == "OPENROUTER_API_KEY is required for the configured model"


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


def test_openrouter_specialists_exclude_encrypted_reasoning_round_trip() -> None:
    settings = Settings(
        _env_file=None,
        agent_model="openrouter:openai/gpt-5.6-luna",
        openrouter_api_key="test-key",
        agent_deep_thinking_effort="medium",
    )
    selection = resolve_model(settings)

    model_settings = _specialist_model_settings(selection.model, settings)

    assert model_settings["openrouter_reasoning"] == {
        "effort": "medium",
        "enabled": True,
        "exclude": True,
    }


def test_gateway_can_route_a_fallback_without_capturing_the_primary() -> None:
    selection = resolve_model(
        Settings(
            _env_file=None,
            agent_model="openrouter:anthropic/claude-sonnet-4.6",
            agent_fallback_models=["openai-chat:gateway/fallback"],
            agent_gateway_base_url="https://gateway.example/v1",
            agent_gateway_api_key="gateway-key",
            openrouter_api_key="openrouter-key",
        )
    )

    assert isinstance(selection.model, FallbackModel)
    assert isinstance(selection.model.models[0], OpenRouterModel)
    assert isinstance(selection.model.models[1], OpenAIChatModel)
    assert selection.gateway
    assert not selection.configuration_errors


def test_ollama_remains_an_explicit_provider_option() -> None:
    selection = resolve_model(Settings(_env_file=None, agent_model="ollama:qwen3:1.7b"))

    assert isinstance(selection.model, NativeOllamaModel)
    assert selection.provider == "ollama"


def test_provider_billing_failure_marks_any_configured_model_unavailable() -> None:
    failure = classify_model_failure(
        ModelHTTPError(
            status_code=400,
            model_name="configured-model",
            body={"error": {"message": "Your credit balance is too low"}},
        ),
        "provider:configured-model",
    )

    assert failure.code == "provider_account_unavailable"
    assert failure.status == "unavailable"
    assert not failure.retryable
