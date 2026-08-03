from uuid import UUID

import pytest
from pydantic import ValidationError

from voice_ai.agent.protocol import AgentTurnRequest, ToolStarted
from voice_ai.voice.agent_client import _confirmation_decision


def test_agent_protocol_serializes_discriminated_events() -> None:
    event = ToolStarted(tool="duckduckgo_search", label="Searching the web")

    assert event.model_dump() == {
        "type": "tool_started",
        "tool": "duckduckgo_search",
        "label": "Searching the web",
        "source": "root",
        "agent": None,
        "tool_call_id": None,
    }


def test_agent_turn_rejects_empty_transcript() -> None:
    with pytest.raises(ValidationError):
        AgentTurnRequest(
            session_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            text="",
        )


@pytest.mark.parametrize(
    ("transcript", "decision"),
    [
        ("Yes, please!", "approve"),
        ("go ahead", "approve"),
        ("No thanks.", "reject"),
        ("What would that include?", None),
    ],
)
def test_voice_confirmation_decision_is_explicit(transcript, decision) -> None:
    assert _confirmation_decision(transcript) == decision
