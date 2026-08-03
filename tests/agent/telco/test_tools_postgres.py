from __future__ import annotations

import os
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import func
from sqlmodel import select

from voice_ai.agent.persistence.database import Database
from voice_ai.agent.persistence.seed import seed_demo_data
from voice_ai.agent.telco.models import Charge, PlanChangeRequest
from voice_ai.agent.telco.tools import (
    explain_latest_bill,
    get_account_balance,
    list_recent_charges,
    request_plan_change,
)

SUBSCRIBER = UUID("11111111-1111-4111-8111-111111111111")


@pytest.fixture
async def postgres() -> Database:
    url = os.getenv("TEST_DATABASE_URL")
    if not url or "test" not in url.lower():
        pytest.skip("Set an isolated TEST_DATABASE_URL containing 'test' to run PostgreSQL tests")
    database = Database(url)
    await database.create_schema()
    await seed_demo_data(database, reset=True)
    try:
        yield database
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_real_queries_preserve_money_and_seed_is_idempotent(postgres: Database) -> None:
    balance = await get_account_balance(SUBSCRIBER, postgres.session_factory)
    charges = await list_recent_charges(SUBSCRIBER, 10, postgres.session_factory)
    await seed_demo_data(postgres)

    async with postgres.session() as session:
        count = (
            await session.exec(
                select(func.count())
                .select_from(Charge)
                .where(Charge.subscriber_id == SUBSCRIBER)
            )
        ).one()

    assert balance["amount"] == "GHS 42.50"
    assert len(charges["charges"]) == 4
    assert count == 4


@pytest.mark.asyncio
async def test_bill_explanation_compares_cycle_charges_with_plan(postgres: Database) -> None:
    result = await explain_latest_bill(SUBSCRIBER, postgres.session_factory)

    assert result["usual_plan_charge"] == "GHS 35.00"
    assert result["recent_total"] == "GHS 58.50"
    assert result["amount_above_plan"] == "GHS 23.50"
    assert len(result["additional_charges"]) == 3


@pytest.mark.asyncio
async def test_confirmed_plan_change_writes_request_record(postgres: Database) -> None:
    result = await request_plan_change(
        SUBSCRIBER,
        "MAX_60",
        "server-generated-one-use-nonce",
        postgres.session_factory,
    )

    async with postgres.session() as session:
        row = (
            await session.exec(
                select(PlanChangeRequest).where(
                    PlanChangeRequest.id == UUID(result["request_id"])
                )
            )
        ).first()

    assert row is not None
    assert row.quoted_price == Decimal("60.00")
    assert result["quoted_price"] == "GHS 60.00"
