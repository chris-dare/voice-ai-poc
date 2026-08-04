from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from pydantic import BaseModel, Field
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext

from voice_ai.agent.evals import EvalGateResult, _failed_case_count

DEFAULT_VOICE_SUITE_PATH = Path(__file__).parents[2] / "evals" / "voice_service_v1.json"


class VoiceEvalInput(BaseModel):
    reference_transcript: str
    contract_transcript: str
    contract_partials: list[str] = Field(default_factory=list)
    contract_agent_text: str = ""
    contract_endpointing_ms: float = Field(default=0, ge=0)
    contract_first_transcript_ms: float = Field(default=0, ge=0)
    contract_first_audio_ms: float = Field(default=0, ge=0)
    contract_barge_in_cancelled: bool = False


class VoiceEvalExpectations(BaseModel):
    category: str = "voice_quality"
    tags: list[str] = Field(default_factory=list)
    max_word_error_rate: float = Field(default=0.15, ge=0, le=1)
    min_partial_transcripts: int = Field(default=0, ge=0)
    required_agent_any_text: list[str] = Field(default_factory=list)
    require_barge_in_cancellation: bool = False
    max_endpointing_ms: float = Field(default=800, gt=0)
    max_first_transcript_ms: float = Field(default=1500, gt=0)
    max_first_audio_ms: float = Field(default=4000, gt=0)


class VoiceEvalCaseDefinition(VoiceEvalExpectations):
    id: str
    reference_transcript: str
    contract_transcript: str
    contract_partials: list[str] = Field(default_factory=list)
    contract_agent_text: str = ""
    contract_endpointing_ms: float = Field(default=0, ge=0)
    contract_first_transcript_ms: float = Field(default=0, ge=0)
    contract_first_audio_ms: float = Field(default=0, ge=0)
    contract_barge_in_cancelled: bool = False


class VoiceEvalSuiteDefinition(BaseModel):
    suite_version: str
    minimum_assertion_pass_rate: float = Field(ge=0, le=1)
    cases: list[VoiceEvalCaseDefinition] = Field(min_length=1)


class VoiceEvalOutcome(BaseModel):
    transcript: str
    partial_transcripts: list[str] = Field(default_factory=list)
    agent_text: str = ""
    endpointing_ms: float = Field(default=0, ge=0)
    first_transcript_ms: float = Field(default=0, ge=0)
    first_audio_ms: float = Field(default=0, ge=0)
    barge_in_cancelled: bool = False
    error: str | None = None


class VoiceScenarioEvaluator(Evaluator[VoiceEvalInput, VoiceEvalOutcome, VoiceEvalExpectations]):
    def evaluate(
        self,
        ctx: EvaluatorContext[VoiceEvalInput, VoiceEvalOutcome, VoiceEvalExpectations],
    ) -> dict[str, bool]:
        expected = ctx.metadata or VoiceEvalExpectations()
        normalized_agent_text = ctx.output.agent_text.casefold()
        assertions = {
            "completed_without_error": ctx.output.error is None,
            "transcript_accuracy": word_error_rate(
                ctx.inputs.reference_transcript, ctx.output.transcript
            )
            <= expected.max_word_error_rate,
            "partial_transcript_delivery": (
                len(ctx.output.partial_transcripts) >= expected.min_partial_transcripts
            ),
            "endpointing_latency": ctx.output.endpointing_ms <= expected.max_endpointing_ms,
            "first_transcript_latency": (
                ctx.output.first_transcript_ms <= expected.max_first_transcript_ms
            ),
            "first_audio_latency": ctx.output.first_audio_ms <= expected.max_first_audio_ms,
        }
        if expected.required_agent_any_text:
            assertions["agent_answer_content"] = any(
                text.casefold() in normalized_agent_text
                for text in expected.required_agent_any_text
            )
        if expected.require_barge_in_cancellation:
            assertions["barge_in_cancellation"] = ctx.output.barge_in_cancelled
        return assertions


def word_error_rate(reference: str, hypothesis: str) -> float:
    reference_words = _words(reference)
    hypothesis_words = _words(hypothesis)
    if not reference_words:
        return 0.0 if not hypothesis_words else 1.0
    previous = list(range(len(hypothesis_words) + 1))
    for reference_index, reference_word in enumerate(reference_words, start=1):
        current = [reference_index]
        for hypothesis_index, hypothesis_word in enumerate(hypothesis_words, start=1):
            substitution = previous[hypothesis_index - 1] + (reference_word != hypothesis_word)
            current.append(
                min(
                    previous[hypothesis_index] + 1,
                    current[hypothesis_index - 1] + 1,
                    substitution,
                )
            )
        previous = current
    return previous[-1] / len(reference_words)


def load_voice_eval_suite(
    path: Path = DEFAULT_VOICE_SUITE_PATH,
) -> VoiceEvalSuiteDefinition:
    return VoiceEvalSuiteDefinition.model_validate_json(path.read_text())


def build_voice_dataset(
    suite: VoiceEvalSuiteDefinition,
) -> Dataset[VoiceEvalInput, VoiceEvalOutcome, VoiceEvalExpectations]:
    excluded = {
        "id",
        "reference_transcript",
        "contract_transcript",
        "contract_partials",
        "contract_agent_text",
        "contract_endpointing_ms",
        "contract_first_transcript_ms",
        "contract_first_audio_ms",
        "contract_barge_in_cancelled",
    }
    return Dataset(
        name=f"voice-service-{suite.suite_version}",
        cases=[
            Case(
                name=definition.id,
                inputs=VoiceEvalInput(
                    reference_transcript=definition.reference_transcript,
                    contract_transcript=definition.contract_transcript,
                    contract_partials=definition.contract_partials,
                    contract_agent_text=definition.contract_agent_text,
                    contract_endpointing_ms=definition.contract_endpointing_ms,
                    contract_first_transcript_ms=definition.contract_first_transcript_ms,
                    contract_first_audio_ms=definition.contract_first_audio_ms,
                    contract_barge_in_cancelled=definition.contract_barge_in_cancelled,
                ),
                metadata=VoiceEvalExpectations.model_validate(
                    definition.model_dump(exclude=excluded)
                ),
            )
            for definition in suite.cases
        ],
        evaluators=[VoiceScenarioEvaluator()],
    )


async def run_voice_eval_gate(
    *,
    suite_path: Path = DEFAULT_VOICE_SUITE_PATH,
) -> EvalGateResult:
    """Run the provider-free voice contract gate.

    Recorded-audio inference is deliberately a separate hardware-profile run; this
    gate validates the dataset, scoring rules, transcript revisions, latency
    budgets, and barge-in contract without pretending fixtures are live audio.
    """

    started_at = datetime.now(UTC)
    started = perf_counter()
    suite = load_voice_eval_suite(suite_path)
    report = await build_voice_dataset(suite).evaluate(
        _contract_voice_task,
        name=f"contract-{suite.suite_version}",
        max_concurrency=1,
        progress=False,
        metadata={"suite_version": suite.suite_version, "mode": "contract"},
    )
    averages = report.averages()
    pass_rate = averages.assertions if averages and averages.assertions is not None else 0.0
    failed_cases = _failed_case_count(report)
    completed_at = datetime.now(UTC)
    return EvalGateResult(
        suite_name="voice-service",
        suite_version=suite.suite_version,
        assertion_pass_rate=pass_rate,
        failed_cases=failed_cases,
        passed=failed_cases == 0 and pass_rate >= suite.minimum_assertion_pass_rate,
        report=report,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=round((perf_counter() - started) * 1_000, 1),
    )


async def _contract_voice_task(inputs: VoiceEvalInput) -> VoiceEvalOutcome:
    return VoiceEvalOutcome(
        transcript=inputs.contract_transcript,
        partial_transcripts=inputs.contract_partials,
        agent_text=inputs.contract_agent_text,
        endpointing_ms=inputs.contract_endpointing_ms,
        first_transcript_ms=inputs.contract_first_transcript_ms,
        first_audio_ms=inputs.contract_first_audio_ms,
        barge_in_cancelled=inputs.contract_barge_in_cancelled,
    )


def _words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", value.casefold())
