from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext, LLMJudge

from voice_ai.agent.models import probe_model_availability
from voice_ai.agent.protocol import (
    AgentError,
    AgentTurnRequest,
    ResponseCompleted,
    TextDelta,
    ToolStarted,
)
from voice_ai.agent.runtime import AgentRuntime
from voice_ai.shared.config import AgentSettings

DEFAULT_SUITE_PATH = Path(__file__).parents[2] / "evals" / "agent_service_v1.json"


class EvalPreflightError(RuntimeError):
    """The configured live route cannot support a meaningful evaluation run."""

    def __init__(self, *, model: str, code: str | None, detail: str) -> None:
        super().__init__(detail)
        self.model = model
        self.code = code
        self.detail = detail


class EvalInput(BaseModel):
    turns: list[str] = Field(min_length=1)
    contract_output: str
    contract_tools: list[str] = Field(default_factory=list)
    contract_delegations: int = Field(default=0, ge=0)
    contract_model_requests: int = Field(default=1, ge=0)


class EvalExpectations(BaseModel):
    category: str = "general"
    tags: list[str] = Field(default_factory=list)
    required_all_text: list[str] = Field(default_factory=list)
    required_any_text: list[str] = Field(default_factory=list)
    forbidden_text: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    min_url_citations: int = Field(default=0, ge=0)
    min_delegations: int = Field(default=0, ge=0)
    max_tool_calls: int = Field(default=20, ge=0)
    max_model_requests: int = Field(default=16, ge=0)
    max_duration_ms: float = Field(default=30_000, gt=0)
    max_total_tokens: int = Field(default=40_000, ge=0)
    judge_rubric: str | None = None


class EvalCaseDefinition(EvalExpectations):
    id: str
    turns: list[str] = Field(min_length=1)
    contract_output: str
    contract_tools: list[str] = Field(default_factory=list)
    contract_delegations: int = Field(default=0, ge=0)
    contract_model_requests: int = Field(default=1, ge=0)


class EvalSuiteDefinition(BaseModel):
    suite_version: str
    minimum_assertion_pass_rate: float = Field(ge=0, le=1)
    cases: list[EvalCaseDefinition] = Field(min_length=1)


class EvalOutcome(BaseModel):
    text: str
    tools: list[str] = Field(default_factory=list)
    delegations: int = Field(default=0, ge=0)
    model_requests: int = Field(default=0, ge=0)
    duration_ms: float = Field(ge=0)
    total_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    reported_cost_usd: str | None = None
    estimated_cost_usd: str | None = None
    provider_response_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error: str | None = None


@dataclass(repr=False)
class AgentScenarioEvaluator(Evaluator[EvalInput, EvalOutcome, EvalExpectations]):
    def evaluate(
        self,
        ctx: EvaluatorContext[EvalInput, EvalOutcome, EvalExpectations],
    ) -> dict[str, bool]:
        expected = ctx.metadata or EvalExpectations()
        normalized_text = _normalize_evaluation_text(ctx.output.text)
        tool_names = set(ctx.output.tools)
        assertions: dict[str, bool] = {
            "completed_without_error": ctx.output.error is None,
            "returned_text": bool(ctx.output.text.strip()),
        }
        if expected.required_all_text:
            assertions["contains_all_required_text"] = all(
                _normalize_evaluation_text(text) in normalized_text
                for text in expected.required_all_text
            )
        if expected.required_any_text:
            assertions["contains_expected_text"] = any(
                _normalize_evaluation_text(text) in normalized_text
                for text in expected.required_any_text
            )
        if expected.forbidden_text:
            assertions["avoids_forbidden_text"] = all(
                _normalize_evaluation_text(text) not in normalized_text
                for text in expected.forbidden_text
            )
        if expected.required_tools:
            assertions["used_required_tools"] = set(expected.required_tools) <= tool_names
        if expected.forbidden_tools:
            assertions["avoids_forbidden_tools"] = not (set(expected.forbidden_tools) & tool_names)
        if expected.min_url_citations:
            assertions["includes_source_urls"] = (
                len(re.findall(r"https?://[^\s)\]>]+", ctx.output.text))
                >= expected.min_url_citations
            )
        if expected.min_delegations:
            assertions["delegates_complex_work"] = (
                ctx.output.delegations >= expected.min_delegations
            )
        assertions["tool_call_budget"] = len(ctx.output.tools) <= expected.max_tool_calls
        assertions["model_request_budget"] = (
            ctx.output.model_requests <= expected.max_model_requests
        )
        assertions["latency_budget"] = ctx.output.duration_ms <= expected.max_duration_ms
        assertions["token_budget"] = ctx.output.total_tokens <= expected.max_total_tokens
        return assertions


@dataclass(frozen=True, slots=True)
class EvalGateResult:
    suite_name: str
    suite_version: str
    assertion_pass_rate: float
    failed_cases: int
    passed: bool
    report: Any
    started_at: datetime
    completed_at: datetime
    duration_ms: float
    total_tokens: int = 0
    reasoning_tokens: int = 0
    reported_cost_usd: str | None = None
    estimated_cost_usd: str | None = None


def load_eval_suite(path: Path = DEFAULT_SUITE_PATH) -> EvalSuiteDefinition:
    return EvalSuiteDefinition.model_validate_json(path.read_text())


def build_dataset(
    suite: EvalSuiteDefinition,
    *,
    judge_model: str | None = None,
) -> Dataset[EvalInput, EvalOutcome, EvalExpectations]:
    cases = []
    for definition in suite.cases:
        evaluators = ()
        if judge_model and definition.judge_rubric:
            evaluators = (
                LLMJudge(
                    rubric=(
                        "Evaluate only the assistant text in the output object's `text` field. "
                        "Do not reward tool metadata, token counts, or the contract fixture. "
                        f"The user conversation was: {json.dumps(definition.turns)}. "
                        f"{definition.judge_rubric}"
                    ),
                    model=judge_model,
                    include_input=False,
                    assertion={"evaluation_name": "answer_quality", "include_reason": True},
                ),
            )
        cases.append(
            Case(
                name=definition.id,
                inputs=EvalInput(
                    turns=definition.turns,
                    contract_output=definition.contract_output,
                    contract_tools=definition.contract_tools,
                    contract_delegations=definition.contract_delegations,
                    contract_model_requests=definition.contract_model_requests,
                ),
                metadata=EvalExpectations.model_validate(
                    definition.model_dump(
                        exclude={
                            "id",
                            "turns",
                            "contract_output",
                            "contract_tools",
                            "contract_delegations",
                            "contract_model_requests",
                        }
                    )
                ),
                evaluators=evaluators,
            )
        )
    return Dataset(
        name=f"agent-service-{suite.suite_version}",
        cases=cases,
        evaluators=[AgentScenarioEvaluator()],
    )


async def run_eval_gate(
    settings: AgentSettings,
    *,
    live: bool,
    suite_path: Path = DEFAULT_SUITE_PATH,
    judge_model: str | None = None,
) -> EvalGateResult:
    started_at = datetime.now(UTC)
    started = perf_counter()
    suite = load_eval_suite(suite_path)
    effective_judge_model = judge_model or settings.agent_eval_judge_model if live else None
    if live:
        probe = await probe_model_availability(settings, settings.agent_model)
        if probe.status in {"degraded", "unavailable"}:
            raise EvalPreflightError(
                model=settings.agent_model,
                code=probe.reason_code,
                detail=probe.detail,
            )
        if effective_judge_model and effective_judge_model != settings.agent_model:
            judge_probe = await probe_model_availability(settings, effective_judge_model)
            if judge_probe.status in {"degraded", "unavailable"}:
                raise EvalPreflightError(
                    model=effective_judge_model,
                    code=judge_probe.reason_code,
                    detail=f"Evaluation judge is unavailable: {judge_probe.detail}",
                )
    dataset = build_dataset(suite, judge_model=effective_judge_model)
    runtime = AgentRuntime(settings) if live else None
    try:
        task = _live_task(runtime) if runtime is not None else _contract_task
        report = await dataset.evaluate(
            task,
            name=f"{'live' if live else 'contract'}-{suite.suite_version}",
            max_concurrency=1,
            progress=False,
            metadata={
                "suite_version": suite.suite_version,
                "mode": "live" if live else "contract",
                "model": settings.agent_model if live else "deterministic-fixture",
                "judge_model": effective_judge_model,
            },
        )
    finally:
        if runtime is not None:
            await runtime.shutdown()
    averages = report.averages()
    pass_rate = averages.assertions if averages and averages.assertions is not None else 0.0
    failed_cases = _failed_case_count(report)
    usage_totals = _report_usage_totals(report)
    completed_at = datetime.now(UTC)
    return EvalGateResult(
        suite_name="agent-service",
        suite_version=suite.suite_version,
        assertion_pass_rate=pass_rate,
        failed_cases=failed_cases,
        passed=failed_cases == 0 and pass_rate >= suite.minimum_assertion_pass_rate,
        report=report,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=round((perf_counter() - started) * 1_000, 1),
        **usage_totals,
    )


async def _contract_task(inputs: EvalInput) -> EvalOutcome:
    return EvalOutcome(
        text=inputs.contract_output,
        tools=inputs.contract_tools,
        delegations=inputs.contract_delegations,
        model_requests=inputs.contract_model_requests,
        duration_ms=1,
        total_tokens=1,
    )


def _live_task(runtime: AgentRuntime):
    async def run(inputs: EvalInput) -> EvalOutcome:
        session_id = uuid4()
        started = perf_counter()
        final_text = ""
        tools: list[str] = []
        delegations = 0
        model_requests = 0
        total_tokens = 0
        reasoning_tokens = 0
        reported_cost = Decimal()
        estimated_cost = Decimal()
        has_reported_cost = False
        has_estimated_cost = False
        provider_response_ids: list[str] = []
        error_code: str | None = None
        error: str | None = None
        for turn in inputs.turns:
            turn_text = ""
            async for event in runtime.stream_turn(
                AgentTurnRequest(session_id=session_id, text=turn)
            ):
                if isinstance(event, ToolStarted):
                    tools.append(event.tool)
                    if event.tool == "delegate_task":
                        delegations += 1
                elif isinstance(event, TextDelta):
                    turn_text += event.text
                elif isinstance(event, ResponseCompleted) and event.usage is not None:
                    total_tokens += event.usage.total_tokens
                    reasoning_tokens += event.usage.reasoning_tokens
                    model_requests += event.usage.model_requests
                    if event.usage.reported_cost_usd is not None:
                        reported_cost += Decimal(event.usage.reported_cost_usd)
                        has_reported_cost = True
                    if event.usage.estimated_cost_usd is not None:
                        estimated_cost += Decimal(event.usage.estimated_cost_usd)
                        has_estimated_cost = True
                    provider_response_ids.extend(
                        attempt.provider_response_id
                        for attempt in event.usage.attempts
                        if attempt.provider_response_id is not None
                    )
                elif isinstance(event, AgentError):
                    error = event.message
                    error_code = event.code
                    if event.usage is not None:
                        total_tokens += event.usage.total_tokens
                        reasoning_tokens += event.usage.reasoning_tokens
                        model_requests += event.usage.model_requests
                        if event.usage.reported_cost_usd is not None:
                            reported_cost += Decimal(event.usage.reported_cost_usd)
                            has_reported_cost = True
                        if event.usage.estimated_cost_usd is not None:
                            estimated_cost += Decimal(event.usage.estimated_cost_usd)
                            has_estimated_cost = True
                        provider_response_ids.extend(
                            attempt.provider_response_id
                            for attempt in event.usage.attempts
                            if attempt.provider_response_id is not None
                        )
            final_text = turn_text
            if error:
                break
        return EvalOutcome(
            text=final_text,
            tools=tools,
            delegations=delegations,
            model_requests=model_requests,
            duration_ms=round((perf_counter() - started) * 1_000, 1),
            total_tokens=total_tokens,
            reasoning_tokens=reasoning_tokens,
            reported_cost_usd=(_decimal_text(reported_cost) if has_reported_cost else None),
            estimated_cost_usd=(_decimal_text(estimated_cost) if has_estimated_cost else None),
            provider_response_ids=list(dict.fromkeys(provider_response_ids)),
            error_code=error_code,
            error=error,
        )

    return run


def _failed_case_count(report: Any) -> int:
    assertion_failures = sum(
        bool(case.evaluator_failures)
        or any(
            getattr(assertion, "value", assertion) is not True
            for assertion in case.assertions.values()
        )
        for case in report.cases
    )
    return len(report.failures) + assertion_failures


def _normalize_evaluation_text(value: str) -> str:
    """Normalize typography without weakening the semantic assertion."""
    return " ".join(
        value.casefold()
        .translate(
            str.maketrans(
                {
                    "’": "'",
                    "‘": "'",
                    "“": '"',
                    "”": '"',
                    "–": "-",
                    "—": "-",
                    "‑": "-",
                }
            )
        )
        .split()
    )


def _report_usage_totals(report: Any) -> dict[str, Any]:
    total_tokens = 0
    reasoning_tokens = 0
    reported_cost = Decimal()
    estimated_cost = Decimal()
    has_reported_cost = False
    has_estimated_cost = False
    for case in report.cases:
        output = getattr(case, "output", None)
        if not isinstance(output, EvalOutcome):
            continue
        total_tokens += output.total_tokens
        reasoning_tokens += output.reasoning_tokens
        if output.reported_cost_usd is not None:
            reported_cost += Decimal(output.reported_cost_usd)
            has_reported_cost = True
        if output.estimated_cost_usd is not None:
            estimated_cost += Decimal(output.estimated_cost_usd)
            has_estimated_cost = True
    return {
        "total_tokens": total_tokens,
        "reasoning_tokens": reasoning_tokens,
        "reported_cost_usd": _decimal_text(reported_cost) if has_reported_cost else None,
        "estimated_cost_usd": (_decimal_text(estimated_cost) if has_estimated_cost else None),
    }


def _decimal_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000000000001")), "f")


def gate_summary(result: EvalGateResult, *, live: bool) -> str:
    return json.dumps(
        {
            "suite_name": result.suite_name,
            "suite_version": result.suite_version,
            "mode": "live" if live else "contract",
            "assertion_pass_rate": round(result.assertion_pass_rate, 4),
            "failed_cases": result.failed_cases,
            "passed": result.passed,
            "total_tokens": result.total_tokens,
            "reasoning_tokens": result.reasoning_tokens,
            "reported_cost_usd": result.reported_cost_usd,
            "estimated_cost_usd": result.estimated_cost_usd,
        },
        indent=2,
        sort_keys=True,
    )
