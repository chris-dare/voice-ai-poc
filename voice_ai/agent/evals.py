from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from voice_ai.agent.protocol import (
    AgentError,
    AgentTurnRequest,
    ResponseCompleted,
    TextDelta,
    ToolStarted,
)
from voice_ai.agent.runtime import AgentRuntime
from voice_ai.shared.config import Settings

DEFAULT_SUITE_PATH = Path(__file__).parents[2] / "evals" / "agent_service_v1.json"


class EvalInput(BaseModel):
    turns: list[str] = Field(min_length=1)
    contract_output: str
    contract_tools: list[str] = Field(default_factory=list)


class EvalExpectations(BaseModel):
    required_any_text: list[str] = Field(default_factory=list)
    forbidden_text: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    max_tool_calls: int = Field(default=20, ge=0)
    max_duration_ms: float = Field(default=30_000, gt=0)
    max_total_tokens: int = Field(default=40_000, ge=0)


class EvalCaseDefinition(EvalExpectations):
    id: str
    turns: list[str] = Field(min_length=1)
    contract_output: str
    contract_tools: list[str] = Field(default_factory=list)


class EvalSuiteDefinition(BaseModel):
    suite_version: str
    minimum_assertion_pass_rate: float = Field(ge=0, le=1)
    cases: list[EvalCaseDefinition] = Field(min_length=1)


class EvalOutcome(BaseModel):
    text: str
    tools: list[str] = Field(default_factory=list)
    duration_ms: float = Field(ge=0)
    total_tokens: int = Field(default=0, ge=0)
    error: str | None = None


@dataclass(repr=False)
class AgentScenarioEvaluator(Evaluator[EvalInput, EvalOutcome, EvalExpectations]):
    def evaluate(
        self,
        ctx: EvaluatorContext[EvalInput, EvalOutcome, EvalExpectations],
    ) -> dict[str, bool]:
        expected = ctx.metadata or EvalExpectations()
        normalized_text = ctx.output.text.casefold()
        tool_names = set(ctx.output.tools)
        assertions: dict[str, bool] = {"completed_without_error": ctx.output.error is None}
        if expected.required_any_text:
            assertions["contains_expected_text"] = any(
                text.casefold() in normalized_text for text in expected.required_any_text
            )
        if expected.forbidden_text:
            assertions["avoids_forbidden_text"] = all(
                text.casefold() not in normalized_text for text in expected.forbidden_text
            )
        if expected.required_tools:
            assertions["used_required_tools"] = set(expected.required_tools) <= tool_names
        if expected.forbidden_tools:
            assertions["avoids_forbidden_tools"] = not (set(expected.forbidden_tools) & tool_names)
        assertions["tool_call_budget"] = len(ctx.output.tools) <= expected.max_tool_calls
        assertions["latency_budget"] = ctx.output.duration_ms <= expected.max_duration_ms
        assertions["token_budget"] = ctx.output.total_tokens <= expected.max_total_tokens
        return assertions


@dataclass(frozen=True, slots=True)
class EvalGateResult:
    suite_version: str
    assertion_pass_rate: float
    failed_cases: int
    passed: bool
    report: Any
    started_at: datetime
    completed_at: datetime
    duration_ms: float


def load_eval_suite(path: Path = DEFAULT_SUITE_PATH) -> EvalSuiteDefinition:
    return EvalSuiteDefinition.model_validate_json(path.read_text())


def build_dataset(
    suite: EvalSuiteDefinition,
) -> Dataset[EvalInput, EvalOutcome, EvalExpectations]:
    cases = [
        Case(
            name=definition.id,
            inputs=EvalInput(
                turns=definition.turns,
                contract_output=definition.contract_output,
                contract_tools=definition.contract_tools,
            ),
            metadata=EvalExpectations.model_validate(
                definition.model_dump(exclude={"id", "turns", "contract_output", "contract_tools"})
            ),
        )
        for definition in suite.cases
    ]
    return Dataset(
        name=f"agent-service-{suite.suite_version}",
        cases=cases,
        evaluators=[AgentScenarioEvaluator()],
    )


async def run_eval_gate(
    settings: Settings,
    *,
    live: bool,
    suite_path: Path = DEFAULT_SUITE_PATH,
) -> EvalGateResult:
    started_at = datetime.now(UTC)
    started = perf_counter()
    suite = load_eval_suite(suite_path)
    dataset = build_dataset(suite)
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
            },
        )
    finally:
        if runtime is not None:
            await runtime.shutdown()
    averages = report.averages()
    pass_rate = averages.assertions if averages and averages.assertions is not None else 0.0
    failed_cases = len(report.failures)
    completed_at = datetime.now(UTC)
    return EvalGateResult(
        suite_version=suite.suite_version,
        assertion_pass_rate=pass_rate,
        failed_cases=failed_cases,
        passed=failed_cases == 0 and pass_rate >= suite.minimum_assertion_pass_rate,
        report=report,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=round((perf_counter() - started) * 1_000, 1),
    )


async def _contract_task(inputs: EvalInput) -> EvalOutcome:
    return EvalOutcome(
        text=inputs.contract_output,
        tools=inputs.contract_tools,
        duration_ms=1,
        total_tokens=1,
    )


def _live_task(runtime: AgentRuntime):
    async def run(inputs: EvalInput) -> EvalOutcome:
        session_id = uuid4()
        started = perf_counter()
        final_text = ""
        tools: list[str] = []
        total_tokens = 0
        error: str | None = None
        for turn in inputs.turns:
            turn_text = ""
            async for event in runtime.stream_turn(
                AgentTurnRequest(session_id=session_id, text=turn)
            ):
                if isinstance(event, ToolStarted):
                    tools.append(event.tool)
                elif isinstance(event, TextDelta):
                    turn_text += event.text
                elif isinstance(event, ResponseCompleted) and event.usage is not None:
                    total_tokens += event.usage.total_tokens
                elif isinstance(event, AgentError):
                    error = event.message
                    if event.usage is not None:
                        total_tokens += event.usage.total_tokens
            final_text = turn_text
            if error:
                break
        return EvalOutcome(
            text=final_text,
            tools=tools,
            duration_ms=round((perf_counter() - started) * 1_000, 1),
            total_tokens=total_tokens,
            error=error,
        )

    return run


def gate_summary(result: EvalGateResult, *, live: bool) -> str:
    return json.dumps(
        {
            "suite_version": result.suite_version,
            "mode": "live" if live else "contract",
            "assertion_pass_rate": round(result.assertion_pass_rate, 4),
            "failed_cases": result.failed_cases,
            "passed": result.passed,
        },
        indent=2,
        sort_keys=True,
    )
