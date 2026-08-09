from __future__ import annotations

import os
import socket
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import logfire
from fastapi import FastAPI
from loguru import logger
from opentelemetry import metrics as otel_metrics
from sqlalchemy.ext.asyncio import AsyncEngine

from voice_ai.shared.config import CommonSettings

MessageSender = Callable[[dict[str, Any]], Awaitable[None]]

_logfire_configured = False
_metrics_configured = False
_metric_provider: Any = None
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
_model_reported_cost = None
_response_queue_delay = None
_response_first_text = None
_response_completion = None
_eval_runs = None
_eval_assertion_pass_rate = None
_agent_job_events = None
_agent_queue_depth = None
_agent_response_events = None
_agent_active_responses = None
_agent_tool_duration = None
_agent_worker_recovery = None
_agent_capacity_events = None
_agent_capacity_wait = None
_voice_session_events = None
_voice_active_sessions = None


def configure_observability(settings: CommonSettings, *, service_name: str) -> bool:
    """Configure one process before registering any framework instrumentation."""
    global _agent_turn_latency, _auth_events, _auth_iat_offset, _browser_latency
    global _model_cost, _model_reported_cost, _model_request_latency, _model_tokens
    global _response_completion, _response_first_text, _response_queue_delay
    global _agent_job_events, _agent_queue_depth
    global _agent_active_responses, _agent_response_events
    global _agent_capacity_events, _agent_capacity_wait
    global _agent_tool_duration, _agent_worker_recovery
    global _eval_assertion_pass_rate, _eval_runs
    global _logfire_configured, _loguru_sink_id
    global _voice_active_sessions, _voice_session_events

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
                        else "agent-worker"
                        if service_name.endswith("worker")
                        else "agent-api"
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
            if service_name.endswith(("agent", "worker", "evals")):
                logfire.instrument_asyncpg(capture_parameters=False)
                logfire.instrument_pydantic_ai(
                    include_content=settings.logfire_capture_content,
                )
            logfire.instrument_system_metrics(base="full")
            _loguru_sink_id = logger.add(
                **logfire.loguru_handler(),
                level=settings.log_level.upper(),
            )
            _configure_metric_instruments()
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

    tracing_ready = configure_tracing(settings.otlp_endpoint, service_name=service_name)
    metrics_ready = configure_otlp_metrics(
        settings.otlp_metrics_endpoint,
        service_name=service_name,
        environment=settings.logfire_environment,
    )
    return tracing_ready or metrics_ready


def _configure_metric_instruments() -> None:
    """Create metrics through the OpenTelemetry API for backend portability."""
    global _agent_turn_latency, _auth_events, _auth_iat_offset, _browser_latency
    global _model_cost, _model_reported_cost, _model_request_latency, _model_tokens
    global _response_completion, _response_first_text, _response_queue_delay
    global _agent_job_events, _agent_queue_depth
    global _agent_active_responses, _agent_response_events
    global _agent_capacity_events, _agent_capacity_wait
    global _agent_tool_duration, _agent_worker_recovery
    global _eval_assertion_pass_rate, _eval_runs
    global _metrics_configured, _startup_latency, _voice_latency
    global _voice_active_sessions, _voice_session_events

    if _metrics_configured:
        return
    meter = otel_metrics.get_meter("voice_ai", _service_version())
    _voice_latency = meter.create_histogram(
        "voice.turn.latency", unit="s", description="User speech stop to assistant speech start"
    )
    _agent_turn_latency = meter.create_histogram(
        "agent.turn.duration", unit="ms", description="Agent turn execution time"
    )
    _startup_latency = meter.create_histogram(
        "app.startup.duration",
        unit="s",
        description="Process or dependency initialization duration",
    )
    _auth_events = meter.create_counter(
        "auth.request",
        unit="1",
        description="Authentication verification and recovery outcomes",
    )
    _auth_iat_offset = meter.create_histogram(
        "auth.token.iat_offset",
        unit="s",
        description="Positive access-token issued-at offset from API host time",
    )
    _browser_latency = meter.create_histogram(
        "browser.lifecycle.duration",
        unit="ms",
        description="Sanitized browser startup and authentication durations",
    )
    _model_request_latency = meter.create_histogram(
        "agent.model.request.duration",
        unit="ms",
        description="Model request duration observed at the agent boundary",
    )
    _model_tokens = meter.create_counter(
        "agent.model.tokens", unit="1", description="Provider-reported model tokens by direction"
    )
    _model_cost = meter.create_counter(
        "agent.model.cost.estimated",
        unit="USD",
        description="Estimated model cost from the pinned genai-prices snapshot",
    )
    _model_reported_cost = meter.create_counter(
        "agent.model.cost.reported",
        unit="USD",
        description="Provider-reported billed model cost",
    )
    _response_queue_delay = meter.create_histogram(
        "agent.response.queue_delay",
        unit="ms",
        description="Accepted response to execution start",
    )
    _response_first_text = meter.create_histogram(
        "agent.response.time_to_first_text",
        unit="ms",
        description="Execution start to first public text event",
    )
    _response_completion = meter.create_histogram(
        "agent.response.completion_duration",
        unit="ms",
        description="Execution start to terminal response state",
    )
    _eval_runs = meter.create_counter(
        "agent.eval.runs",
        unit="1",
        description="Agent evaluation runs by suite, mode, and outcome",
    )
    _eval_assertion_pass_rate = meter.create_histogram(
        "agent.eval.assertion_pass_rate",
        unit="1",
        description="Fraction of evaluation assertions that passed",
    )
    _agent_job_events = meter.create_counter(
        "agent.worker.job", unit="1", description="Durable response job lifecycle events"
    )
    _agent_queue_depth = meter.create_histogram(
        "agent.worker.queue_depth", unit="1", description="Observed pending durable response jobs"
    )
    _agent_response_events = meter.create_counter(
        "agent.response.lifecycle",
        unit="1",
        description="Accepted and terminal durable response outcomes",
    )
    _agent_active_responses = meter.create_gauge(
        "agent.response.active",
        unit="1",
        description="Observed non-terminal durable responses",
    )
    _agent_tool_duration = meter.create_histogram(
        "agent.tool.duration",
        unit="ms",
        description="Tool execution duration by tool, source, and outcome",
    )
    _agent_worker_recovery = meter.create_histogram(
        "agent.worker.recovery.delay",
        unit="ms",
        description="Expired lease to successful recovery claim delay",
    )
    _agent_capacity_events = meter.create_counter(
        "agent.execution.capacity",
        unit="1",
        description="Fleet-wide model and tool capacity lifecycle outcomes",
    )
    _agent_capacity_wait = meter.create_histogram(
        "agent.execution.capacity_wait",
        unit="ms",
        description="Wait time to acquire a fleet-wide model or tool slot",
    )
    _voice_session_events = meter.create_counter(
        "voice.session.lifecycle",
        unit="1",
        description="Voice session admission and release outcomes",
    )
    _voice_active_sessions = meter.create_gauge(
        "voice.session.active",
        unit="1",
        description="Active WebRTC voice sessions on this gateway replica",
    )
    _metrics_configured = True


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
    if _model_reported_cost is not None and attempt.reported_cost_usd is not None:
        _model_reported_cost.add(
            float(attempt.reported_cost_usd),
            {
                **attributes,
                "downstream_provider": attempt.downstream_provider or "unknown",
                "is_byok": attempt.is_byok if attempt.is_byok is not None else False,
            },
        )


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


def record_agent_job_event(*, event: str, attempt: int) -> None:
    if _agent_job_events is not None:
        _agent_job_events.add(
            1,
            {"event": event, "attempt": attempt},
        )


def record_agent_queue_depth(depth: int) -> None:
    if _agent_queue_depth is not None:
        _agent_queue_depth.record(depth, {"workload": "interactive"})


def record_agent_response_event(*, event: str, status: str) -> None:
    if _agent_response_events is not None:
        _agent_response_events.add(
            1,
            {"event": event, "status": status, "workload": "interactive"},
        )


def record_agent_active_responses(active: int) -> None:
    if _agent_active_responses is not None:
        _agent_active_responses.set(active, {"workload": "interactive"})


def record_agent_tool(
    *,
    duration_ms: float,
    tool: str,
    source: str,
    status: str,
) -> None:
    if _agent_tool_duration is not None:
        _agent_tool_duration.record(
            duration_ms,
            {"tool": tool, "source": source, "status": status},
        )


def record_agent_worker_recovery(*, delay_ms: float, attempt: int) -> None:
    if _agent_worker_recovery is not None:
        _agent_worker_recovery.record(
            delay_ms,
            {"attempt": attempt, "workload": "interactive"},
        )


def record_agent_capacity(
    *,
    event: str,
    resource_kind: str,
    resource_key: str,
    wait_ms: float,
) -> None:
    attributes = {
        "event": event,
        "resource_kind": resource_kind,
        "resource_key": resource_key,
    }
    if _agent_capacity_events is not None:
        _agent_capacity_events.add(1, attributes)
    if _agent_capacity_wait is not None and event in {"acquired", "rejected"}:
        _agent_capacity_wait.record(wait_ms, attributes)


def record_voice_session(*, event: str, active: int) -> None:
    attributes = {"event": event, "gateway": "local_voice"}
    if _voice_session_events is not None:
        _voice_session_events.add(1, attributes)
    if _voice_active_sessions is not None:
        _voice_active_sessions.set(active, {"gateway": "local_voice"})


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


def configure_otlp_metrics(
    endpoint: str | None,
    *,
    service_name: str,
    environment: str,
) -> bool:
    """Export the same application metrics to any OTLP/HTTP-compatible backend."""
    global _metric_provider

    if not endpoint:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        exporter = OTLPMetricExporter(endpoint=endpoint)
        reader = PeriodicExportingMetricReader(exporter)
        provider = MeterProvider(
            metric_readers=[reader],
            resource=Resource.create(
                {
                    "service.name": service_name,
                    "service.version": _service_version(),
                    "service.instance.id": f"{socket.gethostname()}:{os.getpid()}",
                    "deployment.environment": environment,
                }
            ),
        )
        otel_metrics.set_meter_provider(provider)
        _metric_provider = provider
        _configure_metric_instruments()
        return True
    except Exception:
        logger.exception("Optional OTLP metrics could not be configured")
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
