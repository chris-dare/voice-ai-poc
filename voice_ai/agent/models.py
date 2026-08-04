from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UserError
from pydantic_ai.models import Model, infer_model, parse_model_id
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider

from voice_ai.agent.capabilities import probe_ollama
from voice_ai.agent.ollama import NativeOllamaModel
from voice_ai.shared.config import AgentSettings


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


ModelAvailabilityStatus = Literal["available", "degraded", "unavailable", "unknown"]


@dataclass(frozen=True, slots=True)
class ModelProbe:
    status: ModelAvailabilityStatus
    reason_code: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class ModelFailure:
    code: str
    message: str
    retryable: bool
    status: ModelAvailabilityStatus
    detail: str


def resolve_model(
    settings: AgentSettings,
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
        errors.extend(f"Fallback {fallback_id}: {error}" for error in fallback_errors)
    model: Model = (
        FallbackModel(primary, *fallback_models, fallback_on=_is_transient_model_error)
        if fallback_models
        else primary
    )
    provider, _ = parse_model_id(selected_id)
    is_gateway = use_gateway or provider == "openrouter"
    return ModelSelection(
        model=model,
        primary_id=selected_id,
        fallback_ids=fallback_ids,
        provider="openai-compatible" if use_gateway else (provider or "unknown"),
        gateway=is_gateway,
        configuration_errors=tuple(errors),
    )


async def check_model_readiness(
    settings: AgentSettings,
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
                capabilities.reachable and capabilities.installed and capabilities.supports_tools
            ),
            model=selection.primary_id,
            provider="ollama",
            detail=detail,
            fallbacks=selection.fallback_ids,
        )
    if provider == "openrouter":
        endpoint = " through the OpenRouter gateway"
    elif selection.gateway:
        endpoint = " through an OpenAI-compatible gateway"
    else:
        endpoint = ""
    return ModelReadiness(
        ready=True,
        model=selection.primary_id,
        provider=selection.provider,
        detail=f"Model configuration is valid{endpoint}; readiness does not make a billable call",
        fallbacks=selection.fallback_ids,
    )


async def check_configured_model(settings: AgentSettings) -> ModelReadiness:
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


async def probe_model_availability(
    settings: AgentSettings,
    model_id: str,
) -> ModelProbe:
    """Perform the strongest safe, non-generating probe supported by a route."""
    try:
        selection = resolve_model(settings, model_id=model_id, include_fallbacks=False)
    except Exception as exc:
        return ModelProbe(
            status="unavailable",
            reason_code="model_configuration_invalid",
            detail=f"Model configuration is invalid: {type(exc).__name__}",
        )
    if selection.configuration_errors:
        return ModelProbe(
            status="unavailable",
            reason_code="model_configuration_invalid",
            detail="; ".join(selection.configuration_errors),
        )

    provider, model_name = parse_model_id(model_id)
    if provider == "ollama" and not selection.gateway:
        capabilities = await probe_ollama(settings.ollama_base_url, model_name)
        if not capabilities.reachable:
            return ModelProbe("unavailable", "provider_unreachable", capabilities.detail)
        if not capabilities.installed:
            return ModelProbe("unavailable", "model_not_installed", capabilities.detail)
        if not capabilities.supports_tools:
            return ModelProbe("unavailable", "tools_not_supported", capabilities.detail)
        return ModelProbe("available", None, "Installed locally and ready")

    if provider == "openrouter":
        return await _probe_openrouter(settings, model_name)

    return ModelProbe(
        "unknown",
        None,
        "Configured; availability will be confirmed on first use",
    )


def classify_model_failure(exc: Exception, model_id: str) -> ModelFailure:
    """Map provider failures to stable, safe public availability semantics."""
    status_code = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    rendered = f"{body or ''} {exc}".lower()
    display_name = model_display_name(model_id)

    if isinstance(exc, ModelConfigurationError):
        return ModelFailure(
            "model_configuration_invalid",
            f"The agent model is not configured: {exc}",
            False,
            "unavailable",
            "Model configuration is invalid",
        )
    if status_code in {401, 402, 403}:
        return ModelFailure(
            "provider_access_denied",
            f"{display_name} is currently unavailable. Choose another model.",
            False,
            "unavailable",
            "Provider access is unavailable for this model",
        )
    if status_code == 404:
        return ModelFailure(
            "model_not_found",
            f"{display_name} is not available from its provider.",
            False,
            "unavailable",
            "The provider does not offer this model to the configured account",
        )
    if status_code == 400 and any(
        marker in rendered
        for marker in ("credit balance", "billing", "insufficient credit", "quota")
    ):
        return ModelFailure(
            "provider_account_unavailable",
            f"{display_name} is currently unavailable. Choose another model.",
            False,
            "unavailable",
            "Provider account access is unavailable for this model",
        )
    if status_code == 429:
        return ModelFailure(
            "model_rate_limited",
            f"{display_name} is busy right now. Try again shortly or choose another model.",
            True,
            "degraded",
            "The provider is rate limited",
        )
    if isinstance(status_code, int) and status_code >= 500:
        return ModelFailure(
            "provider_unavailable",
            f"{display_name} is temporarily unavailable. Try again or choose another model.",
            True,
            "degraded",
            "The provider is temporarily unavailable",
        )
    if isinstance(exc, (TimeoutError, ConnectionError, ModelAPIError)):
        return ModelFailure(
            "provider_unavailable",
            f"{display_name} could not be reached. Try again or choose another model.",
            True,
            "degraded",
            "The model provider could not be reached",
        )
    return ModelFailure(
        "model_request_failed",
        f"{display_name} could not complete that request.",
        False,
        "unknown",
        "The model request failed",
    )


def model_display_name(model_id: str) -> str:
    """Create a readable label without coupling the catalog to one provider."""
    _provider, name = parse_model_id(model_id)
    words = name.replace("/", " ").replace("_", " ").replace("-", " ").split()
    return (
        " ".join(
            word if any(char.isdigit() for char in word) else word.capitalize() for word in words
        )
        or model_id
    )


def _resolve_single(
    settings: AgentSettings,
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

    if provider == "openrouter":
        key = _secret_value(settings.openrouter_api_key)
        errors = [] if key else ["OPENROUTER_API_KEY is required for the configured model"]
        return (
            OpenRouterModel(
                model_name,
                provider=OpenRouterProvider(api_key=key or "openrouter-key-not-configured"),
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


def _uses_gateway(settings: AgentSettings, model_id: str) -> bool:
    provider, _ = parse_model_id(model_id)
    return bool(settings.agent_gateway_base_url and provider == "openai-chat")


async def _probe_openrouter(
    settings: AgentSettings,
    model_name: str,
) -> ModelProbe:
    key = _secret_value(settings.openrouter_api_key)
    if not key:
        return ModelProbe(
            "unavailable",
            "model_configuration_invalid",
            "OpenRouter credentials are not configured",
        )
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            key_response = await client.get(
                "https://openrouter.ai/api/v1/key",
                headers=headers,
            )
            if key_response.status_code != 200:
                return _http_probe_failure(key_response.status_code)
            model_response = await client.get(
                f"https://openrouter.ai/api/v1/model/{model_name}",
                headers=headers,
            )
            if model_response.status_code != 200:
                return _http_probe_failure(model_response.status_code)
    except (httpx.TimeoutException, httpx.NetworkError):
        return ModelProbe(
            "degraded",
            "provider_unavailable",
            "OpenRouter could not be reached",
        )
    return ModelProbe(
        "available",
        None,
        "OpenRouter accepted the API key and advertises the configured model",
    )


def _http_probe_failure(status_code: int) -> ModelProbe:
    if status_code in {401, 402, 403}:
        return ModelProbe(
            "unavailable",
            "provider_access_denied",
            "OpenRouter access is unavailable for this API key",
        )
    if status_code == 404:
        return ModelProbe(
            "unavailable",
            "model_not_found",
            "OpenRouter does not advertise the configured model",
        )
    if status_code == 429:
        return ModelProbe(
            "degraded",
            "model_rate_limited",
            "OpenRouter is rate limited",
        )
    return ModelProbe(
        "degraded",
        "provider_unavailable",
        "OpenRouter availability could not be confirmed",
    )


def _is_transient_model_error(exc: Exception) -> bool:
    if isinstance(exc, ModelHTTPError):
        return exc.status_code in {408, 409, 425, 429} or exc.status_code >= 500
    return isinstance(exc, ModelAPIError)


def _secret_value(secret: Any) -> str | None:
    if secret is None:
        return None
    value = secret.get_secret_value()
    return value if value else None
