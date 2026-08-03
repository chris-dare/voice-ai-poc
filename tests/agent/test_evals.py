import json

from sqlalchemy import select

from voice_ai.agent.eval_models import EvalCaseResult, EvalRun
from voice_ai.agent.eval_persistence import persist_eval_run
from voice_ai.agent.evals import (
    DEFAULT_SUITE_PATH,
    build_dataset,
    load_eval_suite,
    run_eval_gate,
)
from voice_ai.agent.persistence.database import Database
from voice_ai.shared.config import Settings


def test_versioned_eval_suite_builds_with_unique_cases() -> None:
    suite = load_eval_suite()
    dataset = build_dataset(suite)

    assert suite.suite_version == "1.0.0"
    assert len(dataset.cases) >= 4
    assert len({case.name for case in dataset.cases}) == len(dataset.cases)


async def test_contract_eval_gate_is_deterministic_and_provider_free() -> None:
    result = await run_eval_gate(Settings(_env_file=None), live=False)

    assert result.passed
    assert result.failed_cases == 0
    assert result.assertion_pass_rate == 1.0


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

    result = await run_eval_gate(
        Settings(_env_file=None), live=False, suite_path=suite_path
    )

    assert not result.passed
    assert result.assertion_pass_rate < 1.0


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
                    await session.scalars(
                        select(EvalCaseResult)
                        .where(EvalCaseResult.run_id == run_id)
                        .order_by(EvalCaseResult.sequence_number)
                    )
                ).all()
            )
        assert run is not None
        assert run.dataset_digest
        assert run.suite_name == "agent-service-1.0.0"
        assert run.suite_version == "1.0.0"
        assert run.mode == "contract"
        assert run.passed
        assert run.report_json["cases"]
        assert len(cases) == len(result.report.cases)
        assert all(case.status == "passed" for case in cases)
        assert cases[0].assertions_json
    finally:
        await database.close()
