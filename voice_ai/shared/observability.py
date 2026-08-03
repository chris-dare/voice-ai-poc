from __future__ import annotations

import os
import socket
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import logfire
from fastapi import FastAPI
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncEngine

from voice_ai.shared.config import Settings

MessageSender = Callable[[dict[str, Any]], Awaitable[None]]

_logfire_configured = False
_loguru_sink_id: int | None = None
_voice_latency = None
_agent_turn_latency = None
_startup_latency = None
_auth_events = None
_auth_iat_offset = None
_browser_latency = None
_model_request_latency = None
_model_tokens = None
_model_cost = None
_response_queue_delay = None
_response_first_text = None
_response_completion = None
_eval_runs = None
_eval_assertion_pass_rate = None


def configure_observability(settings: Settings, *, service_name: str) -> bool:
    """Configure one process before registering any framework instrumentation."""
    global _agent_turn_latency, _auth_events, _auth_iat_offset, _browser_latency
    global _model_cost, _model_request_latency, _model_tokens
    global _response_completion, _response_first_text, _response_queue_delay
    global _eval_assertion_pass_rate, _eval_runs
    global _logfire_configured, _loguru_sink_id, _startup_latency, _voice_latency

    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    if settings.logfire_enabled:
        if _logfire_configured:
            return True
        try:
            logfire.configure(
                send_to_logfire="if-token-present",
                service_name=service_name,
                service_version=_service_version(),
                environment=settings.logfire_environment,
                resource_attributes={
                    "service.instance.id": f"{socket.gethostname()}:{os.getpid()}",
                    "app.component": (
                        "evals"
                        if service_name.endswith("evals")
                        else "agent"
                        if service_name.endswith("agent")
                        else "gateway"
                    ),
                },
                console=False,
                inspect_arguments=False,
                distributed_tracing=True,
            )
            # Global hooks are registered only after configure(), per Logfire's contract.
            logfire.instrument_httpx(
                capture_headers=False,
                capture_request_body=False,
                capture_response_body=False,
            )
            if service_name.endswith(("agent", "evals")):
                logfire.instrument_asyncpg(capture_parameters=False)
                logfire.instrument_pydantic_ai(
                    include_content=settings.logfire_capture_content,
                )
            logfire.instrument_system_metrics(base="full")
            _loguru_sink_id = logger.add(
                **logfire.loguru_handler(),
                level=settings.log_level.upper(),
            )
            _voice_latency = logfire.metric_histogram(
                "voice.turn.latency",
                unit="s",
                description="User speech stop to assistant speech start",
            )
            _agent_turn_latency = logfire.metric_histogram(
                "agent.turn.duration",
                unit="ms",
                description="Agent turn execution time",
            )
            _startup_latency = logfire.metric_histogram(
                "app.startup.duration",
                unit="s",
                description="Process or dependency initialization duration",
            )
            _auth_events = logfire.metric_counter(
                "auth.request",
                unit="1",
                description="Authentication verification and recovery outcomes",
            )
            _auth_iat_offset = logfire.metric_histogram(
                "auth.token.iat_offset",
                unit="s",
                description="Positive access-token issued-at offset from API host time",
            )
            _browser_latency = logfire.metric_histogram(
                "browser.lifecycle.duration",
                unit="ms",
                description="Sanitized browser startup and authentication durations",
            )
            _model_request_latency = logfire.metric_histogram(
                "agent.model.request.duration",
                unit="ms",
                description="Model request duration observed at the agent boundary",
            )
            _model_tokens = logfire.metric_counter(
                "agent.model.tokens",
                unit="1",
                description="Provider-reported model tokens by direction",
            )
            _model_cost = logfire.metric_counter(
                "agent.model.cost.estimated",
                unit="USD",
                description="Estimated model cost from the pinned genai-prices snapshot",
            )
            _response_queue_delay = logfire.metric_histogram(
                "agent.response.queue_delay",
                unit="ms",
                description="Accepted response to execution start",
            )
            _response_first_text = logfire.metric_histogram(
                "agent.response.time_to_first_text",
                unit="ms",
                description="Execution start to first public text event",
            )
            _response_completion = logfire.metric_histogram(
                "agent.response.completion_duration",
                unit="ms",
                description="Execution start to terminal response state",
            )
            _eval_runs = logfire.metric_counter(
                "agent.eval.runs",
                unit="1",
                description="Agent evaluation runs by suite, mode, and outcome",
            )
            _eval_assertion_pass_rate = logfire.metric_histogram(
                "agent.eval.assertion_pass_rate",
                unit="1",
                description="Fraction of evaluation assertions that passed",
            )
            _logfire_configured = True
            logfire.info(
                "Logfire observability configured for {service_name}",
                service_name=service_name,
                environment=settings.logfire_environment,
                capture_ai_content=settings.logfire_capture_content,
            )
            return True
        except Exception:
            logger.exception("Logfire instrumentation could not be configured")
            return False

    return configure_tracing(settings.otlp_endpoint, service_name=service_name)


def instrument_fastapi(app: FastAPI) -> None:
    if not _logfire_configured:
        return
    logfire.instrument_fastapi(
        app,
        capture_headers=False,
        record_send_receive=False,
    )


def instrument_sqlalchemy(engine: AsyncEngine) -> None:
    if not _logfire_configured:
        return
    logfire.instrument_sqlalchemy(engine=engine, enable_commenter=True)


def record_agent_turn(
    *,
    latency_ms: float,
    route: str,
    status: str,
    model: str = "unknown",
    provider: str = "unknown",
) -> None:
    if _agent_turn_latency is not None:
        _agent_turn_latency.record(
            latency_ms,
            {
                "route": route,
                "status": status,
                "model": model,
                "provider": provider,
            },
        )


def record_model_attempt(attempt: Any) -> None:
    attributes = {
        "agent": attempt.agent,
        "model": attempt.model,
        "provider": attempt.provider,
        "status": attempt.status,
    }
    if _model_request_latency is not None:
        _model_request_latency.record(attempt.duration_ms, attributes)
    if _model_tokens is not None:
        if attempt.input_tokens:
            _model_tokens.add(attempt.input_tokens, {**attributes, "direction": "input"})
        if attempt.output_tokens:
            _model_tokens.add(attempt.output_tokens, {**attributes, "direction": "output"})
    if _model_cost is not None and attempt.estimated_cost_usd is not None:
        _model_cost.add(float(attempt.estimated_cost_usd), attributes)


def record_agent_response(
    *,
    queue_delay_ms: float,
    time_to_first_text_ms: float | None,
    completion_ms: float,
    status: str,
) -> None:
    attributes = {"status": status, "workload": "interactive"}
    if _response_queue_delay is not None:
        _response_queue_delay.record(queue_delay_ms, attributes)
    if _response_first_text is not None and time_to_first_text_ms is not None:
        _response_first_text.record(time_to_first_text_ms, attributes)
    if _response_completion is not None:
        _response_completion.record(completion_ms, attributes)


def record_eval_run(
    *,
    suite_version: str,
    mode: str,
    model: str,
    assertion_pass_rate: float,
    failed_cases: int,
    passed: bool,
    run_id: str,
) -> None:
    attributes = {
        "suite_version": suite_version,
        "mode": mode,
        "model": model,
        "outcome": "passed" if passed else "failed",
        "eval_run_id": run_id,
    }
    if _eval_runs is not None:
        _eval_runs.add(1, attributes)
    if _eval_assertion_pass_rate is not None:
        _eval_assertion_pass_rate.record(assertion_pass_rate, attributes)
    if _logfire_configured:
        logfire.info(
            "Agent evaluation {outcome}: {suite_version} {mode}",
            **attributes,
            assertion_pass_rate=assertion_pass_rate,
            failed_cases=failed_cases,
        )


def record_startup_latency(*, duration_seconds: float, component: str, status: str) -> None:
    if _startup_latency is not None:
        _startup_latency.record(
            duration_seconds,
            {"component": component, "status": status},
        )


def record_auth_event(*, outcome: str, stage: str) -> None:
    if _auth_events is not None:
        _auth_events.add(1, {"outcome": outcome, "stage": stage})


def record_auth_iat_offset(*, seconds: float, outcome: str) -> None:
    if _auth_iat_offset is not None:
        _auth_iat_offset.record(max(0.0, seconds), {"outcome": outcome})


def record_browser_latency(*, duration_ms: float, event: str) -> None:
    if _browser_latency is not None:
        _browser_latency.record(duration_ms, {"event": event})


def configure_tracing(endpoint: str | None, *, service_name: str = "voice-ai") -> bool:
    """Keep the pre-existing generic OTLP exporter as a Logfire-disabled fallback."""
    if not endpoint:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from pipecat.utils.tracing.setup import setup_tracing

        return setup_tracing(
            service_name=service_name,
            exporter=OTLPSpanExporter(endpoint=endpoint),
        )
    except Exception:
        logger.exception("Optional OTLP tracing could not be configured")
        return False


def create_latency_observer(send: MessageSender) -> Any:
    from pipecat.observers.user_bot_latency_observer import (
        LatencyBreakdown,
        UserBotLatencyObserver,
    )

    observer = UserBotLatencyObserver()
    last_latency: float | None = None

    @observer.event_handler("on_latency_measured")
    async def on_latency_measured(_observer: UserBotLatencyObserver, latency: float) -> None:
        nonlocal last_latency
        last_latency = latency
        if _voice_latency is not None:
            _voice_latency.record(latency, {"pipeline": "local_voice"})

    @observer.event_handler("on_latency_breakdown")
    async def on_latency_breakdown(
        _observer: UserBotLatencyObserver, breakdown: LatencyBreakdown
    ) -> None:
        parts: list[dict[str, object]] = []
        if breakdown.user_turn_secs is not None:
            parts.append({"label": "Endpointing", "value": breakdown.user_turn_secs})
        parts.extend(
            {"label": metric.processor, "value": metric.duration_secs} for metric in breakdown.ttfb
        )
        parts.extend(
            {"label": metric.function_name, "value": metric.duration_secs}
            for metric in breakdown.function_calls
        )
        if breakdown.text_aggregation:
            parts.append(
                {
                    "label": "Text aggregation",
                    "value": breakdown.text_aggregation.duration_secs,
                }
            )
        await send(
            {
                "type": "turn-latency",
                "data": {"total": last_latency, "breakdown": parts},
            }
        )

    return observer


def _service_version() -> str:
    try:
        return version("voice-ai")
    except PackageNotFoundError:
        return "0.1.0"
