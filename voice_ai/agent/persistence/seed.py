from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select

from voice_ai.agent.persistence.database import Database
from voice_ai.agent.telco.models import (
    Charge,
    DataUsage,
    Outage,
    Plan,
    PlanChangeRequest,
    Subscriber,
)

DEMO_SUBSCRIBER_IDS = (
    UUID("11111111-1111-4111-8111-111111111111"),
    UUID("22222222-2222-4222-8222-222222222222"),
)
DEMO_CHARGE_IDS = (
    UUID("a1111111-1111-4111-8111-111111111111"),
    UUID("a2222222-2222-4222-8222-222222222222"),
    UUID("a3333333-3333-4333-8333-333333333333"),
    UUID("a4444444-4444-4444-8444-444444444444"),
)
DEMO_OUTAGE_ID = UUID("b1111111-1111-4111-8111-111111111111")


async def seed_demo_data(database: Database, *, reset: bool = False) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    today = now.date()
    async with database.session_factory.begin() as session:
        if reset:
            await session.execute(
                delete(PlanChangeRequest).where(
                    PlanChangeRequest.subscriber_id.in_(DEMO_SUBSCRIBER_IDS)
                )
            )
            await session.execute(delete(Charge).where(Charge.subscriber_id.in_(DEMO_SUBSCRIBER_IDS)))
            await session.execute(
                delete(DataUsage).where(DataUsage.subscriber_id.in_(DEMO_SUBSCRIBER_IDS))
            )
            await session.execute(delete(Subscriber).where(Subscriber.id.in_(DEMO_SUBSCRIBER_IDS)))
            await session.execute(delete(Outage).where(Outage.id == DEMO_OUTAGE_ID))

        plans = (
            Plan(code="FLEX_20", name="Flex 20", monthly_price=Decimal("20.00"), data_allowance_mb=5_120, voice_minutes=120, description="5 GB data and 120 voice minutes"),
            Plan(code="SMART_35", name="Smart 35", monthly_price=Decimal("35.00"), data_allowance_mb=12_288, voice_minutes=300, description="12 GB data and 300 voice minutes"),
            Plan(code="MAX_60", name="Max 60", monthly_price=Decimal("60.00"), data_allowance_mb=30_720, voice_minutes=1_000, description="30 GB data and 1,000 voice minutes"),
        )
        for plan in plans:
            await session.merge(plan)

        primary = Subscriber(
            id=DEMO_SUBSCRIBER_IDS[0], phone_number="+233200000001", display_name="Ama Mensah",
            account_type="prepaid", balance=Decimal("42.50"), currency="GHS",
            plan_code="SMART_35", region="Accra Central", renewal_date=today + timedelta(days=8),
        )
        secondary = Subscriber(
            id=DEMO_SUBSCRIBER_IDS[1], phone_number="+233200000002", display_name="Kojo Asare",
            account_type="postpaid", balance=Decimal("18.75"), currency="GHS",
            plan_code="FLEX_20", region="Kumasi North", renewal_date=today + timedelta(days=12),
        )
        if reset or await session.get(Subscriber, primary.id) is None:
            await session.merge(primary)
        if reset or await session.get(Subscriber, secondary.id) is None:
            await session.merge(secondary)

        if reset or await session.get(DataUsage, primary.id) is None:
            await session.merge(DataUsage(subscriber_id=primary.id, cycle_start=today - timedelta(days=22), cycle_end=today + timedelta(days=8), used_mb=9_216))

        charges = (
            Charge(id=DEMO_CHARGE_IDS[0], subscriber_id=primary.id, description="Smart 35 monthly renewal", amount=Decimal("35.00"), currency="GHS", charged_at=now - timedelta(days=22)),
            Charge(id=DEMO_CHARGE_IDS[1], subscriber_id=primary.id, description="International roaming data pass", amount=Decimal("18.50"), currency="GHS", charged_at=now - timedelta(days=5)),
            Charge(id=DEMO_CHARGE_IDS[2], subscriber_id=primary.id, description="Mobile money service fee", amount=Decimal("1.25"), currency="GHS", charged_at=now - timedelta(days=2)),
            Charge(id=DEMO_CHARGE_IDS[3], subscriber_id=primary.id, description="Out-of-bundle voice", amount=Decimal("3.75"), currency="GHS", charged_at=now - timedelta(hours=19)),
        )
        for charge in charges:
            if reset or await session.get(Charge, charge.id) is None:
                await session.merge(charge)

        outage = Outage(
            id=DEMO_OUTAGE_ID, region="Accra Central", status="investigating",
            summary="Intermittent mobile data service affecting parts of Accra Central",
            started_at=now - timedelta(hours=1, minutes=25),
            estimated_resolution=now + timedelta(hours=2),
        )
        if reset or await session.get(Outage, outage.id) is None:
            await session.merge(outage)


async def demo_data_present(database: Database, subscriber_id: UUID) -> bool:
    async with database.session() as session:
        return await session.scalar(select(Subscriber.id).where(Subscriber.id == subscriber_id)) is not None
