from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import select

from voice_ai.agent.telco.models import (
    Charge,
    DataUsage,
    Outage,
    Plan,
    PlanChangeRequest,
    Subscriber,
)


class ToolError(RuntimeError):
    pass


def money(value: Decimal, currency: str) -> str:
    return f"{currency} {value.quantize(Decimal('0.01'))}"


async def get_account_balance(
    subscriber_id: UUID, session_factory: async_sessionmaker
) -> dict[str, object]:
    async with session_factory() as session:
        subscriber = await session.get(Subscriber, subscriber_id)
        if subscriber is None:
            raise ToolError("Subscriber not found")
        label = "available balance" if subscriber.account_type == "prepaid" else "amount due"
        return {
            "account_type": subscriber.account_type,
            "label": label,
            "amount": money(subscriber.balance, subscriber.currency),
        }


async def get_data_usage(
    subscriber_id: UUID, session_factory: async_sessionmaker
) -> dict[str, object]:
    async with session_factory() as session:
        row = (
            await session.exec(
                select(DataUsage, Plan)
                .join(Subscriber, Subscriber.id == DataUsage.subscriber_id)
                .join(Plan, Plan.code == Subscriber.plan_code)
                .where(DataUsage.subscriber_id == subscriber_id)
            )
        ).one_or_none()
        if row is None:
            raise ToolError("No data usage is available for this subscriber")
        usage, plan = row
        remaining = max(0, plan.data_allowance_mb - usage.used_mb)
        return {
            "used_gb": f"{Decimal(usage.used_mb / 1024).quantize(Decimal('0.1'))}",
            "remaining_gb": f"{Decimal(remaining / 1024).quantize(Decimal('0.1'))}",
            "allowance_gb": f"{Decimal(plan.data_allowance_mb / 1024).quantize(Decimal('0.1'))}",
            "cycle_end": usage.cycle_end.isoformat(),
        }


async def get_current_plan(
    subscriber_id: UUID, session_factory: async_sessionmaker
) -> dict[str, object]:
    async with session_factory() as session:
        row = (
            await session.exec(
                select(Subscriber, Plan)
                .join(Plan, Plan.code == Subscriber.plan_code)
                .where(Subscriber.id == subscriber_id)
            )
        ).one_or_none()
        if row is None:
            raise ToolError("Subscriber not found")
        subscriber, plan = row
        return {
            "code": plan.code,
            "name": plan.name,
            "price": money(plan.monthly_price, subscriber.currency),
            "renewal_date": subscriber.renewal_date.isoformat(),
            "data_gb": f"{Decimal(plan.data_allowance_mb / 1024).quantize(Decimal('0.1'))}",
            "voice_minutes": plan.voice_minutes,
        }


async def list_recent_charges(
    subscriber_id: UUID,
    limit: int,
    session_factory: async_sessionmaker,
) -> dict[str, object]:
    safe_limit = max(1, min(limit, 10))
    async with session_factory() as session:
        charges = (
            (
                await session.exec(
                    select(Charge)
                    .where(Charge.subscriber_id == subscriber_id)
                    .order_by(Charge.charged_at.desc())
                    .limit(safe_limit)
                )
            )
            .unique()
            .all()
        )
        return {
            "charges": [
                {
                    "description": charge.description,
                    "amount": money(charge.amount, charge.currency),
                    "charged_at": charge.charged_at.astimezone(UTC).date().isoformat(),
                }
                for charge in charges
            ]
        }


async def explain_latest_bill(
    subscriber_id: UUID, session_factory: async_sessionmaker
) -> dict[str, object]:
    """Summarize the current mock billing cycle against the recurring plan price."""
    async with session_factory() as session:
        row = (
            await session.exec(
                select(Subscriber, Plan)
                .join(Plan, Plan.code == Subscriber.plan_code)
                .where(Subscriber.id == subscriber_id)
            )
        ).one_or_none()
        if row is None:
            raise ToolError("Subscriber not found")
        subscriber, plan = row
        charges = list(
            (
                await session.exec(
                    select(Charge)
                    .where(Charge.subscriber_id == subscriber_id)
                    .order_by(Charge.charged_at.desc())
                    .limit(10)
                )
            ).all()
        )

        renewal = next(
            (
                charge
                for charge in charges
                if "renewal" in charge.description.lower() and charge.amount == plan.monthly_price
            ),
            None,
        )
        cycle_charges = (
            [charge for charge in charges if charge.charged_at >= renewal.charged_at]
            if renewal is not None
            else charges
        )
        additional = [charge for charge in cycle_charges if charge is not renewal]
        total = sum((charge.amount for charge in cycle_charges), Decimal("0"))
        difference = max(Decimal("0"), total - plan.monthly_price)

        def charge_row(charge: Charge) -> dict[str, str]:
            return {
                "description": charge.description,
                "amount": money(charge.amount, charge.currency),
                "charged_at": charge.charged_at.astimezone(UTC).date().isoformat(),
            }

        return {
            "plan_name": plan.name,
            "usual_plan_charge": money(plan.monthly_price, subscriber.currency),
            "recent_total": money(total, subscriber.currency),
            "amount_above_plan": money(difference, subscriber.currency),
            "additional_charges": [charge_row(charge) for charge in additional],
            "charges": [charge_row(charge) for charge in cycle_charges],
        }


async def check_network_status(
    subscriber_id: UUID, session_factory: async_sessionmaker
) -> dict[str, object]:
    async with session_factory() as session:
        subscriber = await session.get(Subscriber, subscriber_id)
        if subscriber is None:
            raise ToolError("Subscriber not found")
        outages = (
            (
                await session.exec(
                    select(Outage)
                    .where(Outage.region == subscriber.region, Outage.status != "resolved")
                    .order_by(Outage.started_at.desc())
                )
            )
            .unique()
            .all()
        )
        return {
            "region": subscriber.region,
            "has_outage": bool(outages),
            "outages": [
                {
                    "status": outage.status,
                    "summary": outage.summary,
                    "estimated_resolution": (
                        outage.estimated_resolution.isoformat()
                        if outage.estimated_resolution
                        else None
                    ),
                }
                for outage in outages
            ],
        }


async def list_available_plans(
    subscriber_id: UUID, session_factory: async_sessionmaker
) -> dict[str, object]:
    async with session_factory() as session:
        subscriber = await session.get(Subscriber, subscriber_id)
        if subscriber is None:
            raise ToolError("Subscriber not found")
        plans = (
            (
                await session.exec(
                    select(Plan).where(Plan.active.is_(True)).order_by(Plan.monthly_price)
                )
            )
            .unique()
            .all()
        )
        return {
            "current_plan_code": subscriber.plan_code,
            "plans": [
                {
                    "code": plan.code,
                    "name": plan.name,
                    "price": money(plan.monthly_price, subscriber.currency),
                    "data_gb": f"{Decimal(plan.data_allowance_mb / 1024).quantize(Decimal('0.1'))}",
                    "voice_minutes": plan.voice_minutes,
                }
                for plan in plans
            ],
        }


async def quote_plan_change(
    subscriber_id: UUID, plan_code: str, session_factory: async_sessionmaker
) -> dict[str, object]:
    async with session_factory() as session:
        subscriber = await session.get(Subscriber, subscriber_id)
        plan = await session.get(Plan, plan_code)
        if subscriber is None:
            raise ToolError("Subscriber not found")
        if plan is None or not plan.active:
            raise ToolError("That plan is not available")
        if subscriber.plan_code == plan.code:
            raise ToolError("The subscriber is already on that plan")
        return {
            "subscriber_id": subscriber.id,
            "plan_code": plan.code,
            "plan_name": plan.name,
            "quoted_price": plan.monthly_price,
            "currency": subscriber.currency,
            "spoken_price": money(plan.monthly_price, subscriber.currency),
        }


async def request_plan_change(
    subscriber_id: UUID,
    plan_code: str,
    confirmation_nonce: str,
    session_factory: async_sessionmaker,
) -> dict[str, object]:
    async with session_factory.begin() as session:
        subscriber = await session.get(Subscriber, subscriber_id, with_for_update=True)
        plan = await session.get(Plan, plan_code)
        if subscriber is None:
            raise ToolError("Subscriber not found")
        if plan is None or not plan.active:
            raise ToolError("That plan is not available")
        if subscriber.plan_code == plan.code:
            raise ToolError("The subscriber is already on that plan")
        request = PlanChangeRequest(
            id=uuid4(),
            subscriber_id=subscriber.id,
            from_plan_code=subscriber.plan_code,
            to_plan_code=plan.code,
            quoted_price=plan.monthly_price,
            status="requested",
            requested_at=datetime.now(UTC),
            confirmation_nonce=confirmation_nonce,
        )
        session.add(request)
        return {
            "request_id": str(request.id),
            "status": request.status,
            "plan_name": plan.name,
            "quoted_price": money(plan.monthly_price, subscriber.currency),
        }
