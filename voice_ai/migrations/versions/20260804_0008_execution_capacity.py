"""Add distributed execution-capacity leases.

Revision ID: 20260804_0008
Revises: 20260803_0007
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0008"
down_revision: str | None = "20260803_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_execution_capacity_leases",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("resource_kind", sa.String(length=24), nullable=False),
        sa.Column("resource_key", sa.String(length=255), nullable=False),
        sa.Column("owner_id", sa.String(length=255), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_api_execution_capacity_resource",
        "api_execution_capacity_leases",
        ["resource_kind", "resource_key", "expires_at"],
    )
    op.create_index(
        "ix_api_execution_capacity_expiry",
        "api_execution_capacity_leases",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_api_execution_capacity_expiry",
        table_name="api_execution_capacity_leases",
    )
    op.drop_index(
        "ix_api_execution_capacity_resource",
        table_name="api_execution_capacity_leases",
    )
    op.drop_table("api_execution_capacity_leases")
