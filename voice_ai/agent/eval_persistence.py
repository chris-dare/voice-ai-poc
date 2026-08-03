from __future__ import annotations

import asyncio
import os
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic_evals.reporting import EvaluationReportAdapter, ReportCaseAdapter

from voice_ai.agent.eval_models import EvalCaseResult, EvalRun
from voice_ai.agent.evals import EvalGateResult, load_eval_suite
from voice_ai.agent.persistence.database import Database
from voice_ai.shared.config import Settings


async def persist_eval_run(
    settings: Settings,
    result: EvalGateResult,
    *,
    live: bool,
    suite_path: Path,
) -> str:
    """Persist an immutable experiment plus queryable per-case projections."""

    suite_bytes, suite = await asyncio.gather(
        asyncio.to_thread(suite_path.read_bytes),
        asyncio.to_thread(load_eval_suite, suite_path),
    )
    run_id = f"evalrun_{uuid4().hex}"
    mode = "live" if live else "contract"
    model = settings.agent_model if live else "deterministic-fixture"
    report_json = EvaluationReportAdapter.dump_python(result.report, mode="json")
    config_json = {
        "suite_path": str(suite_path),
        "minimum_assertion_pass_rate": suite.minimum_assertion_pass_rate,
        "agent_model": model,
        "agent_fallback_models": settings.agent_fallback_models if live else [],
        "agent_subagent_model": settings.agent_subagent_model if live else None,
        "agent_thinking_effort": settings.agent_thinking_effort if live else None,
        "agent_deep_agents_enabled": settings.agent_deep_agents_enabled if live else None,
        "limits": {
            "requests": settings.agent_request_limit,
            "tool_calls": settings.agent_tool_call_limit,
            "total_tokens": settings.agent_total_token_limit,
            "max_output_tokens": settings.agent_max_output_tokens,
        },
    }
    database = Database(settings.database_url)
    try:
        async with database.session_factory.begin() as session:
            session.add(
                EvalRun(
                    id=run_id,
                    suite_name=f"agent-service-{result.suite_version}",
                    suite_version=result.suite_version,
                    dataset_digest=sha256(suite_bytes).hexdigest(),
                    mode=mode,
                    model=model,
                    status="completed",
                    passed=result.passed,
                    assertion_pass_rate=result.assertion_pass_rate,
                    failed_cases=result.failed_cases,
                    case_count=len(result.report.cases) + len(result.report.failures),
                    started_at=result.started_at,
                    completed_at=result.completed_at,
                    duration_ms=result.duration_ms,
                    source_revision=(
                        os.getenv("GITHUB_SHA") or os.getenv("CI_COMMIT_SHA")
                    ),
                    trace_id=result.report.trace_id,
                    span_id=result.report.span_id,
                    config_json=config_json,
                    report_json=report_json,
                )
            )
            # Persist the experiment before batching its case projections. The
            # models deliberately have no mutable ORM relationship, so an
            # explicit flush is the portable way to satisfy the PostgreSQL FK.
            await session.flush()
            sequence = 0
            for case in result.report.cases:
                sequence += 1
                case_json = ReportCaseAdapter.dump_python(case, mode="json")
                assertions = case_json.get("assertions") or {}
                passed = all(
                    _evaluation_value(value) is True for value in assertions.values()
                ) and not case.evaluator_failures
                session.add(
                    EvalCaseResult(
                        run_id=run_id,
                        sequence_number=sequence,
                        case_name=case.name,
                        source_case_name=case.source_case_name,
                        status="passed" if passed else "failed",
                        task_duration_ms=case.task_duration * 1_000,
                        total_duration_ms=case.total_duration * 1_000,
                        trace_id=case.trace_id,
                        span_id=case.span_id,
                        output_json=case_json.get("output"),
                        assertions_json=assertions,
                        scores_json=case_json.get("scores") or {},
                        metrics_json=case_json.get("metrics") or {},
                        error_json=None,
                        case_json=case_json,
                    )
                )
            serialized_failures = report_json.get("failures") or []
            for failure_index, failure in enumerate(result.report.failures):
                sequence += 1
                failure_json = serialized_failures[failure_index]
                session.add(
                    EvalCaseResult(
                        run_id=run_id,
                        sequence_number=sequence,
                        case_name=failure.name,
                        source_case_name=failure.source_case_name,
                        status="error",
                        task_duration_ms=None,
                        total_duration_ms=None,
                        trace_id=failure.trace_id,
                        span_id=failure.span_id,
                        output_json=None,
                        assertions_json={},
                        scores_json={},
                        metrics_json={},
                        error_json={
                            "message": failure.error_message,
                            "stacktrace": failure.error_stacktrace,
                        },
                        case_json=failure_json,
                    )
                )
    finally:
        await database.close()
    return run_id


def _evaluation_value(value: Any) -> Any:
    return value.get("value") if isinstance(value, dict) else value
