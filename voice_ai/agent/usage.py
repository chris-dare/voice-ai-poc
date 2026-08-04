from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from time import perf_counter
from typing import Literal

import genai_prices
from pydantic import BaseModel, Field
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Hooks, WrapModelRequestHandler
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.usage import RunUsage

from voice_ai.shared.observability import record_model_attempt


class ModelAttemptUsage(BaseModel):
    """Provider-reported usage and locally observed latency for one model response."""

    sequence: int = Field(ge=1)
    agent: str
    model: str
    provider: str
    provider_response_id: str | None = None
    status: Literal["completed", "failed"]
    duration_ms: float = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    reported_cost_usd: str | None = None
    estimated_cost_usd: str | None = None
    downstream_provider: str | None = None
    is_byok: bool | None = None
    error_type: str | None = None


class TurnUsage(BaseModel):
    """Stable, provider-neutral response usage persisted by the public API."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    model_requests: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    wall_clock_ms: float = Field(ge=0)
    model_duration_ms: float = Field(default=0, ge=0)
    route_model: str
    route_provider: str
    gateway: bool
    fallback_models: list[str] = Field(default_factory=list)
    actual_models: list[str] = Field(default_factory=list)
    reported_cost_usd: str | None = None
    estimated_cost_usd: str | None = None
    cost_currency: Literal["USD"] = "USD"
    cost_status: Literal["reported", "estimated", "partial", "unavailable"] = "unavailable"
    cost_source: Literal["openrouter", "genai-prices"] | None = None
    cost_source_version: str | None = None
    attempts: list[ModelAttemptUsage] = Field(default_factory=list)


@dataclass(slots=True)
class UsageTracker:
    """Request-scoped collector shared by the root and delegated agents."""

    attempts: list[ModelAttemptUsage] = field(default_factory=list)

    def completed(self, response: ModelResponse, *, agent: str, duration_ms: float) -> None:
        estimated_price = _estimated_cost(response)
        reported_price = _reported_cost(response)
        provider_details = response.provider_details or {}
        attempt = ModelAttemptUsage(
            sequence=len(self.attempts) + 1,
            agent=agent,
            model=response.model_name or "unknown",
            provider=response.provider_name or "unknown",
            provider_response_id=response.provider_response_id,
            status="completed",
            duration_ms=duration_ms,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_tokens,
            cache_write_tokens=response.usage.cache_write_tokens,
            reasoning_tokens=response.usage.details.get("reasoning_tokens", 0),
            reported_cost_usd=(
                _decimal_text(reported_price) if reported_price is not None else None
            ),
            estimated_cost_usd=(
                _decimal_text(estimated_price) if estimated_price is not None else None
            ),
            downstream_provider=_optional_text(provider_details.get("downstream_provider")),
            is_byok=(
                provider_details.get("is_byok")
                if isinstance(provider_details.get("is_byok"), bool)
                else None
            ),
        )
        self.attempts.append(attempt)
        record_model_attempt(attempt)

    def failed(self, *, agent: str, duration_ms: float, error: Exception) -> None:
        attempt = ModelAttemptUsage(
            sequence=len(self.attempts) + 1,
            agent=agent,
            model="unknown",
            provider="unknown",
            status="failed",
            duration_ms=duration_ms,
            error_type=type(error).__name__,
        )
        self.attempts.append(attempt)
        record_model_attempt(attempt)

    def summary(
        self,
        *,
        usage: RunUsage | None,
        wall_clock_ms: float,
        route_model: str,
        route_provider: str,
        gateway: bool,
        fallback_models: tuple[str, ...],
        observed_tool_calls: int,
    ) -> TurnUsage:
        completed = [a for a in self.attempts if a.status == "completed"]
        # Delegated agents may have isolated usage budgets. The request hook still
        # sees every response, so attempts are the complete cross-agent accounting
        # source while RunUsage can cover only the root run.
        input_tokens = (
            sum(a.input_tokens for a in completed)
            if completed
            else usage.input_tokens
            if usage
            else 0
        )
        output_tokens = (
            sum(a.output_tokens for a in completed)
            if completed
            else usage.output_tokens
            if usage
            else 0
        )
        cache_read_tokens = (
            sum(a.cache_read_tokens for a in completed)
            if completed
            else usage.cache_read_tokens
            if usage
            else 0
        )
        cache_write_tokens = (
            sum(a.cache_write_tokens for a in completed)
            if completed
            else usage.cache_write_tokens
            if usage
            else 0
        )
        reasoning_tokens = sum(a.reasoning_tokens for a in completed)
        reported = [Decimal(a.reported_cost_usd) for a in completed if a.reported_cost_usd]
        estimated = [Decimal(a.estimated_cost_usd) for a in completed if a.estimated_cost_usd]
        if reported and len(reported) == len(completed):
            cost_status: Literal["reported", "estimated", "partial", "unavailable"] = "reported"
            cost_source: Literal["openrouter", "genai-prices"] | None = "openrouter"
        elif reported:
            cost_status = "partial"
            cost_source = "openrouter"
        elif estimated and len(estimated) == len(completed):
            cost_status = "estimated"
            cost_source = "genai-prices"
        elif estimated:
            cost_status = "partial"
            cost_source = "genai-prices"
        else:
            cost_status = "unavailable"
            cost_source = None
        actual_models = list(dict.fromkeys(a.model for a in completed if a.model != "unknown"))
        return TurnUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
            model_requests=max(usage.requests if usage else 0, len(self.attempts)),
            tool_calls=max(usage.tool_calls if usage else 0, observed_tool_calls),
            wall_clock_ms=wall_clock_ms,
            model_duration_ms=round(sum(a.duration_ms for a in self.attempts), 1),
            route_model=route_model,
            route_provider=route_provider,
            gateway=gateway,
            fallback_models=list(fallback_models),
            actual_models=actual_models,
            reported_cost_usd=(_decimal_text(sum(reported, Decimal())) if reported else None),
            estimated_cost_usd=(_decimal_text(sum(estimated, Decimal())) if estimated else None),
            cost_status=cost_status,
            cost_source=cost_source,
            cost_source_version=(
                genai_prices.__version__ if cost_source == "genai-prices" else None
            ),
            attempts=self.attempts,
        )


def model_usage_hooks() -> Hooks:
    """Capture every root and specialist model response without provider coupling."""

    hooks = Hooks()

    @hooks.on.model_request
    async def capture_model_request(
        ctx: RunContext,
        *,
        request_context: ModelRequestContext,
        handler: WrapModelRequestHandler,
    ) -> ModelResponse:
        started = perf_counter()
        agent_name = ctx.agent.name if ctx.agent is not None else "agent"
        try:
            response = await handler(request_context)
        except Exception as exc:
            tracker = getattr(ctx.deps, "usage", None)
            if isinstance(tracker, UsageTracker):
                tracker.failed(
                    agent=agent_name,
                    duration_ms=round((perf_counter() - started) * 1_000, 1),
                    error=exc,
                )
            raise
        tracker = getattr(ctx.deps, "usage", None)
        if isinstance(tracker, UsageTracker):
            tracker.completed(
                response,
                agent=agent_name,
                duration_ms=round((perf_counter() - started) * 1_000, 1),
            )
        return response

    return hooks


def _estimated_cost(response: ModelResponse) -> Decimal | None:
    try:
        return response.cost().total_price
    except Exception:
        # Usage telemetry must never turn a successful model response into a failed turn.
        return None


def _reported_cost(response: ModelResponse) -> Decimal | None:
    value = (response.provider_details or {}).get("cost")
    if isinstance(value, bool) or value is None:
        return None
    try:
        cost = Decimal(str(value))
    except Exception:
        return None
    return cost if cost >= 0 else None


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000000000001")), "f")
