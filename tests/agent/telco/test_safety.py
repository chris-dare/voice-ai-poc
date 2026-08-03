from decimal import Decimal
from uuid import UUID

import pytest

from voice_ai.agent.telco.safety import ConfirmationError, ConfirmationGate

SUBSCRIBER = UUID("11111111-1111-4111-8111-111111111111")


def prepare(gate: ConfirmationGate) -> None:
    gate.prepare(
        subscriber_id=SUBSCRIBER,
        plan_code="MAX_60",
        plan_name="Max 60",
        quoted_price=Decimal("60.00"),
        currency="GHS",
    )


def test_confirmation_must_arrive_in_later_user_turn_and_is_one_use() -> None:
    gate = ConfirmationGate()
    prepare(gate)

    with pytest.raises(ConfirmationError, match="not confirmed"):
        gate.authorize(subscriber_id=SUBSCRIBER, plan_code="MAX_60")

    assert gate.observe_user_turn("Yes please") == "confirmed"
    nonce = gate.authorize(subscriber_id=SUBSCRIBER, plan_code="MAX_60")

    assert nonce
    with pytest.raises(ConfirmationError, match="No active"):
        gate.authorize(subscriber_id=SUBSCRIBER, plan_code="MAX_60")


@pytest.mark.parametrize("transcript", ["no", "cancel", "I am not sure", "what does that include?"])
def test_negative_or_ambiguous_response_clears_pending_action(transcript: str) -> None:
    gate = ConfirmationGate()
    prepare(gate)

    outcome = gate.observe_user_turn(transcript)

    assert outcome in {"cancelled", "ambiguous"}
    assert gate.pending is None


def test_confirmation_cannot_be_retargeted() -> None:
    gate = ConfirmationGate()
    prepare(gate)
    gate.observe_user_turn("confirm")

    with pytest.raises(ConfirmationError, match="does not match"):
        gate.authorize(subscriber_id=SUBSCRIBER, plan_code="FLEX_20")
