"""Create telco demo schema.

Revision ID: 20260727_0001
Revises:
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260727_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("monthly_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("data_allowance_mb", sa.BigInteger(), nullable=False),
        sa.Column("voice_minutes", sa.BigInteger(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_table(
        "subscribers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("phone_number", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("account_type", sa.String(length=16), nullable=False),
        sa.Column("balance", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("region", sa.String(length=80), nullable=False),
        sa.Column("renewal_date", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(["plan_code"], ["plans.code"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("phone_number"),
    )
    op.create_table(
        "data_usage",
        sa.Column("subscriber_id", sa.Uuid(), nullable=False),
        sa.Column("cycle_start", sa.Date(), nullable=False),
        sa.Column("cycle_end", sa.Date(), nullable=False),
        sa.Column("used_mb", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["subscriber_id"], ["subscribers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("subscriber_id"),
    )
    op.create_table(
        "charges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subscriber_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.String(length=240), nullable=False),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("charged_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["subscriber_id"], ["subscribers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_charges_subscriber_charged_at",
        "charges",
        ["subscriber_id", "charged_at"],
        unique=False,
    )
    op.create_table(
        "outages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("region", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("estimated_resolution", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outages_region", "outages", ["region"], unique=False)
    op.create_table(
        "plan_change_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("subscriber_id", sa.Uuid(), nullable=False),
        sa.Column("from_plan_code", sa.String(length=32), nullable=False),
        sa.Column("to_plan_code", sa.String(length=32), nullable=False),
        sa.Column("quoted_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmation_nonce", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["from_plan_code"], ["plans.code"]),
        sa.ForeignKeyConstraint(["subscriber_id"], ["subscribers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_plan_code"], ["plans.code"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("confirmation_nonce"),
    )


def downgrade() -> None:
    op.drop_table("plan_change_requests")
    op.drop_index("ix_outages_region", table_name="outages")
    op.drop_table("outages")
    op.drop_index("ix_charges_subscriber_charged_at", table_name="charges")
    op.drop_table("charges")
    op.drop_table("data_usage")
    op.drop_table("subscribers")
    op.drop_table("plans")
