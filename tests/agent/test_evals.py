import json

import pytest
from sqlmodel import select

from voice_ai.agent.eval_models import EvalCaseResult, EvalRun
from voice_ai.agent.eval_persistence import persist_eval_run
from voice_ai.agent.evals import (
    DEFAULT_SUITE_PATH,
    EvalPreflightError,
    build_dataset,
    load_eval_suite,
    run_eval_gate,
)
from voice_ai.agent.models import ModelProbe
from voice_ai.agent.persistence.database import Database
from voice_ai.shared.config import Settings


def test_versioned_eval_suite_builds_with_unique_cases() -> None:
    suite = load_eval_suite()
    dataset = build_dataset(suite)

    assert suite.suite_version == "1.1.4"
    assert len(dataset.cases) >= 12
    assert len({case.name for case in dataset.cases}) == len(dataset.cases)
    assert {case.metadata.category for case in dataset.cases if case.metadata is not None} >= {
        "answer_quality",
        "web_research",
        "security",
        "delegation",
    }


def test_live_dataset_adds_only_configured_case_judges() -> None:
    suite = load_eval_suite()
    without_judge = build_dataset(suite)
    with_judge = build_dataset(suite, judge_model="test:judge")

    assert not any(case.evaluators for case in without_judge.cases)
    assert sum(bool(case.evaluators) for case in with_judge.cases) >= 6


async def test_contract_eval_gate_is_deterministic_and_provider_free() -> None:
    result = await run_eval_gate(Settings(_env_file=None), live=False)

    assert result.passed
    assert result.failed_cases == 0
    assert result.assertion_pass_rate == 1.0


async def test_live_eval_fails_fast_when_model_probe_is_unavailable(monkeypatch) -> None:
    calls = 0

    async def unavailable_probe(_settings, _model_id):
        nonlocal calls
        calls += 1
        return ModelProbe(
            status="unavailable",
            reason_code="provider_account_unavailable",
            detail="Provider account access is unavailable for this model",
        )

    monkeypatch.setattr("voice_ai.agent.evals.probe_model_availability", unavailable_probe)

    with pytest.raises(EvalPreflightError, match="Provider account access"):
        await run_eval_gate(Settings(_env_file=None), live=True)

    assert calls == 1


async def test_live_eval_fails_fast_when_judge_probe_is_unavailable(monkeypatch) -> None:
    probed_models = []

    async def model_probe(_settings, model_id):
        probed_models.append(model_id)
        if model_id == "test:judge":
            return ModelProbe(
                status="unavailable",
                reason_code="provider_account_unavailable",
                detail="Judge account is unavailable",
            )
        return ModelProbe(status="available", reason_code=None, detail="Model is ready")

    monkeypatch.setattr("voice_ai.agent.evals.probe_model_availability", model_probe)
    settings = Settings(_env_file=None)

    with pytest.raises(EvalPreflightError, match="Evaluation judge is unavailable"):
        await run_eval_gate(
            settings,
            live=True,
            judge_model="test:judge",
        )

    assert probed_models == [settings.agent_model, "test:judge"]


async def test_contract_eval_gate_fails_when_an_assertion_fails(tmp_path) -> None:
    suite_path = tmp_path / "failing-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_version": "test-failure",
                "minimum_assertion_pass_rate": 1.0,
                "cases": [
                    {
                        "id": "missing_required_text",
                        "turns": ["Answer the question"],
                        "contract_output": "A different answer",
                        "required_any_text": ["expected phrase"],
                    }
                ],
            }
        )
    )

    result = await run_eval_gate(Settings(_env_file=None), live=False, suite_path=suite_path)

    assert not result.passed
    assert result.assertion_pass_rate < 1.0
    assert result.failed_cases == 1


async def test_eval_run_is_persisted_as_immutable_experiment(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'evals.db'}"
    database = Database(database_url)
    await database.create_schema()
    await database.close()
    settings = Settings(_env_file=None, database_url=database_url)
    result = await run_eval_gate(settings, live=False)

    run_id = await persist_eval_run(
        settings,
        result,
        live=False,
        suite_path=DEFAULT_SUITE_PATH,
    )

    database = Database(database_url)
    try:
        async with database.session() as session:
            run = await session.get(EvalRun, run_id)
            cases = list(
                (
                    await session.exec(
                        select(EvalCaseResult)
                        .where(EvalCaseResult.run_id == run_id)
                        .order_by(EvalCaseResult.sequence_number)
                    )
                ).all()
            )
        assert run is not None
        assert run.dataset_digest
        assert run.suite_name == "agent-service-1.1.4"
        assert run.suite_version == "1.1.4"
        assert run.mode == "contract"
        assert run.passed
        assert run.report_json["cases"]
        assert len(cases) == len(result.report.cases)
        assert all(case.status == "passed" for case in cases)
        assert cases[0].assertions_json
    finally:
        await database.close()
