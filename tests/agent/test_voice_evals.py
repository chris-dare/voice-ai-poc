import json

from voice_ai.agent.voice_evals import (
    build_voice_dataset,
    load_voice_eval_suite,
    run_voice_eval_gate,
    word_error_rate,
)


def test_word_error_rate_handles_substitutions_insertions_and_empty_text() -> None:
    assert word_error_rate("hello world", "hello world") == 0
    assert word_error_rate("hello world", "hello there") == 0.5
    assert word_error_rate("", "") == 0
    assert word_error_rate("", "unexpected") == 1


def test_versioned_voice_suite_covers_quality_and_barge_in() -> None:
    suite = load_voice_eval_suite()
    dataset = build_voice_dataset(suite)

    assert suite.suite_version == "1.0.0"
    assert len(dataset.cases) >= 5
    categories = {case.metadata.category for case in dataset.cases if case.metadata is not None}
    assert {"transcription", "barge_in", "latency"} <= categories


async def test_voice_contract_gate_is_provider_free_and_deterministic() -> None:
    result = await run_voice_eval_gate()

    assert result.suite_name == "voice-service"
    assert result.passed
    assert result.assertion_pass_rate == 1.0


async def test_voice_contract_gate_fails_excessive_word_error_rate(tmp_path) -> None:
    suite_path = tmp_path / "failing-voice-suite.json"
    suite_path.write_text(
        json.dumps(
            {
                "suite_version": "test-failure",
                "minimum_assertion_pass_rate": 1.0,
                "cases": [
                    {
                        "id": "bad-transcript",
                        "reference_transcript": "please check my account",
                        "contract_transcript": "completely unrelated words",
                        "max_word_error_rate": 0.1,
                    }
                ],
            }
        )
    )

    result = await run_voice_eval_gate(suite_path=suite_path)

    assert not result.passed
    assert result.assertion_pass_rate < 1.0
    assert result.failed_cases == 1
