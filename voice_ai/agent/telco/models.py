from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, Date, DateTime, Index, Numeric, String, Text
from sqlmodel import Field

from voice_ai.agent.persistence.model import TableModel


class Plan(TableModel, table=True):
    __tablename__ = "plans"

    code: str = Field(sa_type=String(32), primary_key=True)
    name: str = Field(sa_type=String(120), nullable=False)
    monthly_price: Decimal = Field(sa_type=Numeric(12, 2), nullable=False)
    data_allowance_mb: int = Field(sa_type=BigInteger, nullable=False)
    voice_minutes: int = Field(sa_type=BigInteger, nullable=False)
    description: str = Field(sa_type=Text, nullable=False)
    active: bool = Field(default=True, nullable=False)


class Subscriber(TableModel, table=True):
    __tablename__ = "subscribers"

    id: UUID = Field(primary_key=True)
    phone_number: str = Field(sa_type=String(32), unique=True, nullable=False)
    display_name: str = Field(sa_type=String(120), nullable=False)
    account_type: str = Field(sa_type=String(16), nullable=False)
    balance: Decimal = Field(sa_type=Numeric(12, 2), nullable=False)
    currency: str = Field(default="GHS", sa_type=String(3), nullable=False)
    plan_code: str = Field(sa_type=String(32), foreign_key="plans.code", nullable=False)
    region: str = Field(sa_type=String(80), nullable=False)
    renewal_date: date = Field(sa_type=Date, nullable=False)


class DataUsage(TableModel, table=True):
    __tablename__ = "data_usage"

    subscriber_id: UUID = Field(
        foreign_key="subscribers.id",
        ondelete="CASCADE",
        primary_key=True,
    )
    cycle_start: date = Field(sa_type=Date, nullable=False)
    cycle_end: date = Field(sa_type=Date, nullable=False)
    used_mb: int = Field(sa_type=BigInteger, nullable=False)


class Charge(TableModel, table=True):
    __tablename__ = "charges"

    id: UUID = Field(primary_key=True)
    subscriber_id: UUID = Field(
        foreign_key="subscribers.id",
        ondelete="CASCADE",
        nullable=False,
    )
    description: str = Field(sa_type=String(240), nullable=False)
    amount: Decimal = Field(sa_type=Numeric(12, 2), nullable=False)
    currency: str = Field(default="GHS", sa_type=String(3), nullable=False)
    charged_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_charges_subscriber_charged_at", "subscriber_id", "charged_at"),)


class Outage(TableModel, table=True):
    __tablename__ = "outages"

    id: UUID = Field(primary_key=True)
    region: str = Field(sa_type=String(80), nullable=False, index=True)
    status: str = Field(sa_type=String(24), nullable=False)
    summary: str = Field(sa_type=Text, nullable=False)
    started_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    estimated_resolution: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )


class PlanChangeRequest(TableModel, table=True):
    __tablename__ = "plan_change_requests"

    id: UUID = Field(primary_key=True)
    subscriber_id: UUID = Field(
        foreign_key="subscribers.id",
        ondelete="CASCADE",
        nullable=False,
    )
    from_plan_code: str = Field(sa_type=String(32), foreign_key="plans.code", nullable=False)
    to_plan_code: str = Field(sa_type=String(32), foreign_key="plans.code", nullable=False)
    quoted_price: Decimal = Field(sa_type=Numeric(12, 2), nullable=False)
    status: str = Field(sa_type=String(24), nullable=False)
    requested_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    confirmation_nonce: str = Field(sa_type=String(64), unique=True, nullable=False)


def model_as_dict(model: TableModel, fields: tuple[str, ...]) -> dict[str, object]:
    return model.model_dump(include=set(fields))
