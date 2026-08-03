from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UserError
from pydantic_ai.models import Model, infer_model, parse_model_id
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider

from voice_ai.agent.capabilities import probe_ollama
from voice_ai.agent.ollama import NativeOllamaModel
from voice_ai.shared.config import Settings


class ModelConfigurationError(RuntimeError):
    """The selected model cannot be called with the current configuration."""


@dataclass(frozen=True, slots=True)
class ModelSelection:
    model: Model
    primary_id: str
    fallback_ids: tuple[str, ...]
    provider: str
    gateway: bool
    configuration_errors: tuple[str, ...] = ()

    @property
    def model_ids(self) -> tuple[str, ...]:
        return (self.primary_id, *self.fallback_ids)


@dataclass(frozen=True, slots=True)
class ModelReadiness:
    ready: bool
    model: str
    provider: str
    detail: str
    fallbacks: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "model": self.model,
            "provider": self.provider,
            "detail": self.detail,
            "fallbacks": list(self.fallbacks),
        }


def resolve_model(
    settings: Settings,
    *,
    model_id: str | None = None,
    include_fallbacks: bool = True,
) -> ModelSelection:
    """Resolve a configured model without coupling orchestration to its host."""
    selected_id = model_id or settings.agent_model
    use_gateway = _uses_gateway(settings, selected_id)
    primary, errors = _resolve_single(settings, selected_id, use_gateway=use_gateway)
    fallback_ids = tuple(settings.agent_fallback_models) if include_fallbacks else ()
    fallback_models: list[Model] = []
    for fallback_id in fallback_ids:
        fallback, fallback_errors = _resolve_single(
            settings,
            fallback_id,
            use_gateway=_uses_gateway(settings, fallback_id),
        )
        fallback_models.append(fallback)
        errors.extend(
            f"Fallback {fallback_id}: {error}" for error in fallback_errors
        )
    model: Model = (
        FallbackModel(primary, *fallback_models, fallback_on=_is_transient_model_error)
        if fallback_models
        else primary
    )
    provider, _ = parse_model_id(selected_id)
    return ModelSelection(
        model=model,
        primary_id=selected_id,
        fallback_ids=fallback_ids,
        provider="openai-compatible" if use_gateway else (provider or "unknown"),
        gateway=use_gateway,
        configuration_errors=tuple(errors),
    )


async def check_model_readiness(
    settings: Settings,
    selection: ModelSelection,
) -> ModelReadiness:
    if selection.configuration_errors:
        return ModelReadiness(
            ready=False,
            model=selection.primary_id,
            provider=selection.provider,
            detail="; ".join(selection.configuration_errors),
            fallbacks=selection.fallback_ids,
        )
    provider, model_name = parse_model_id(selection.primary_id)
    if provider == "ollama" and not selection.gateway:
        capabilities = await probe_ollama(settings.ollama_base_url, model_name)
        if not capabilities.reachable:
            detail = f"Ollama is not reachable at {settings.ollama_base_url}"
        elif not capabilities.installed:
            detail = f"{model_name} is not pulled"
        elif not capabilities.supports_tools:
            detail = f"{model_name} does not advertise tool support"
        else:
            detail = f"{model_name} is installed and supports tools"
        return ModelReadiness(
            ready=(
                capabilities.reachable
                and capabilities.installed
                and capabilities.supports_tools
            ),
            model=selection.primary_id,
            provider="ollama",
            detail=detail,
            fallbacks=selection.fallback_ids,
        )
    endpoint = " through an OpenAI-compatible gateway" if selection.gateway else ""
    return ModelReadiness(
        ready=True,
        model=selection.primary_id,
        provider=selection.provider,
        detail=f"Model configuration is valid{endpoint}; readiness does not make a billable call",
        fallbacks=selection.fallback_ids,
    )


async def check_configured_model(settings: Settings) -> ModelReadiness:
    """Validate provider configuration and locally probe only zero-cost models."""
    try:
        selection = resolve_model(settings)
    except Exception as exc:
        provider, _ = parse_model_id(settings.agent_model)
        return ModelReadiness(
            ready=False,
            model=settings.agent_model,
            provider=provider or "unknown",
            detail=f"Invalid model configuration: {type(exc).__name__}: {exc}",
            fallbacks=tuple(settings.agent_fallback_models),
        )
    await selection.model.__aenter__()
    try:
        return await check_model_readiness(settings, selection)
    finally:
        await selection.model.__aexit__(None, None, None)


def _resolve_single(
    settings: Settings,
    model_id: str,
    *,
    use_gateway: bool,
) -> tuple[Model, list[str]]:
    provider, model_name = parse_model_id(model_id)
    if provider is None:
        raise UserError(
            f"AGENT_MODEL must use Pydantic AI's provider:model format; got {model_id!r}"
        )

    if use_gateway:
        if provider != "openai-chat":
            raise UserError(
                "AGENT_GATEWAY_BASE_URL can only route openai-chat:<gateway-model> model IDs"
            )
        key = _secret_value(settings.agent_gateway_api_key)
        errors = [] if key else ["AGENT_GATEWAY_API_KEY is required for the configured gateway"]
        model = OpenAIChatModel(
            model_name,
            provider=OpenAIProvider(
                base_url=settings.agent_gateway_base_url,
                api_key=key or "gateway-key-not-configured",
            ),
        )
        return model, errors

    if provider == "anthropic":
        key = _secret_value(settings.anthropic_api_key)
        errors = [] if key else ["ANTHROPIC_API_KEY is required for the configured model"]
        return (
            AnthropicModel(
                model_name,
                provider=AnthropicProvider(api_key=key or "anthropic-key-not-configured"),
            ),
            errors,
        )
    if provider == "ollama":
        return (
            NativeOllamaModel(
                model_name,
                base_url=settings.ollama_base_url,
            ),
            [],
        )
    return infer_model(model_id), []


def _uses_gateway(settings: Settings, model_id: str) -> bool:
    provider, _ = parse_model_id(model_id)
    return bool(settings.agent_gateway_base_url and provider == "openai-chat")


def _is_transient_model_error(exc: Exception) -> bool:
    if isinstance(exc, ModelHTTPError):
        return exc.status_code in {408, 409, 425, 429} or exc.status_code >= 500
    return isinstance(exc, ModelAPIError)


def _secret_value(secret: Any) -> str | None:
    if secret is None:
        return None
    value = secret.get_secret_value()
    return value if value else None
