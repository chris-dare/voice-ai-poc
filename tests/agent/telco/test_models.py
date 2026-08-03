from decimal import Decimal

from sqlalchemy import Numeric

from voice_ai.agent.telco.models import Charge, Plan, PlanChangeRequest, Subscriber
from voice_ai.agent.telco.tools import money


def test_every_monetary_column_is_numeric_12_2() -> None:
    columns = (
        Subscriber.__table__.c.balance,
        Charge.__table__.c.amount,
        Plan.__table__.c.monthly_price,
        PlanChangeRequest.__table__.c.quoted_price,
    )

    for column in columns:
        assert isinstance(column.type, Numeric)
        assert column.type.precision == 12
        assert column.type.scale == 2
        assert column.type.asdecimal is True


def test_money_is_formatted_without_float_conversion() -> None:
    assert money(Decimal("42.50"), "GHS") == "GHS 42.50"
    assert money(Decimal("42.5000"), "GHS") == "GHS 42.50"
