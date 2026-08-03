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
    status: Literal["completed", "failed"]
    duration_ms: float = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: str | None = None
    error_type: str | None = None


class TurnUsage(BaseModel):
    """Stable, provider-neutral response usage persisted by the public API."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    model_requests: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    wall_clock_ms: float = Field(ge=0)
    model_duration_ms: float = Field(default=0, ge=0)
    route_model: str
    route_provider: str
    gateway: bool
    fallback_models: list[str] = Field(default_factory=list)
    actual_models: list[str] = Field(default_factory=list)
    estimated_cost_usd: str | None = None
    cost_currency: Literal["USD"] = "USD"
    cost_status: Literal["estimated", "partial", "unavailable"] = "unavailable"
    cost_source: Literal["genai-prices"] | None = None
    cost_source_version: str | None = None
    attempts: list[ModelAttemptUsage] = Field(default_factory=list)


@dataclass(slots=True)
class UsageTracker:
    """Request-scoped collector shared by the root and delegated agents."""

    attempts: list[ModelAttemptUsage] = field(default_factory=list)

    def completed(self, response: ModelResponse, *, agent: str, duration_ms: float) -> None:
        price = _estimated_cost(response)
        attempt = ModelAttemptUsage(
            sequence=len(self.attempts) + 1,
            agent=agent,
            model=response.model_name or "unknown",
            provider=response.provider_name or "unknown",
            status="completed",
            duration_ms=duration_ms,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=response.usage.cache_read_tokens,
            cache_write_tokens=response.usage.cache_write_tokens,
            estimated_cost_usd=_decimal_text(price) if price is not None else None,
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
        input_tokens = usage.input_tokens if usage else sum(a.input_tokens for a in self.attempts)
        output_tokens = (
            usage.output_tokens if usage else sum(a.output_tokens for a in self.attempts)
        )
        cache_read_tokens = (
            usage.cache_read_tokens if usage else sum(a.cache_read_tokens for a in self.attempts)
        )
        cache_write_tokens = (
            usage.cache_write_tokens if usage else sum(a.cache_write_tokens for a in self.attempts)
        )
        priced = [Decimal(a.estimated_cost_usd) for a in self.attempts if a.estimated_cost_usd]
        completed = [a for a in self.attempts if a.status == "completed"]
        if priced and len(priced) == len(completed):
            cost_status: Literal["estimated", "partial", "unavailable"] = "estimated"
        elif priced:
            cost_status = "partial"
        else:
            cost_status = "unavailable"
        actual_models = list(dict.fromkeys(a.model for a in completed if a.model != "unknown"))
        return TurnUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            model_requests=usage.requests if usage else len(self.attempts),
            tool_calls=usage.tool_calls if usage else observed_tool_calls,
            wall_clock_ms=wall_clock_ms,
            model_duration_ms=round(sum(a.duration_ms for a in self.attempts), 1),
            route_model=route_model,
            route_provider=route_provider,
            gateway=gateway,
            fallback_models=list(fallback_models),
            actual_models=actual_models,
            estimated_cost_usd=_decimal_text(sum(priced, Decimal())) if priced else None,
            cost_status=cost_status,
            cost_source="genai-prices" if priced else None,
            cost_source_version=genai_prices.__version__ if priced else None,
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


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000000000001")), "f")
