from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from voice_ai.agent.persistence.database import Base


class Plan(Base):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    monthly_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    data_allowance_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)
    voice_minutes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)


class Subscriber(Base):
    __tablename__ = "subscribers"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    phone_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    account_type: Mapped[str] = mapped_column(String(16), nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="GHS", nullable=False)
    plan_code: Mapped[str] = mapped_column(ForeignKey("plans.code"), nullable=False)
    region: Mapped[str] = mapped_column(String(80), nullable=False)
    renewal_date: Mapped[date] = mapped_column(Date, nullable=False)


class DataUsage(Base):
    __tablename__ = "data_usage"

    subscriber_id: Mapped[UUID] = mapped_column(
        ForeignKey("subscribers.id", ondelete="CASCADE"), primary_key=True
    )
    cycle_start: Mapped[date] = mapped_column(Date, nullable=False)
    cycle_end: Mapped[date] = mapped_column(Date, nullable=False)
    used_mb: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Charge(Base):
    __tablename__ = "charges"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    subscriber_id: Mapped[UUID] = mapped_column(
        ForeignKey("subscribers.id", ondelete="CASCADE"), nullable=False
    )
    description: Mapped[str] = mapped_column(String(240), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="GHS", nullable=False)
    charged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_charges_subscriber_charged_at", "subscriber_id", "charged_at"),)


class Outage(Base):
    __tablename__ = "outages"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    region: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    estimated_resolution: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlanChangeRequest(Base):
    __tablename__ = "plan_change_requests"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    subscriber_id: Mapped[UUID] = mapped_column(
        ForeignKey("subscribers.id", ondelete="CASCADE"), nullable=False
    )
    from_plan_code: Mapped[str] = mapped_column(ForeignKey("plans.code"), nullable=False)
    to_plan_code: Mapped[str] = mapped_column(ForeignKey("plans.code"), nullable=False)
    quoted_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmation_nonce: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)


def model_as_dict(model: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: getattr(model, field) for field in fields}
