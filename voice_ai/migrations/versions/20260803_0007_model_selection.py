"""Add user-selectable model routing and shared availability state.

Revision ID: 20260803_0007
Revises: 20260803_0006
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0007"
down_revision: str | None = "20260803_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_MODEL = "legacy:unspecified"


def upgrade() -> None:
    op.add_column(
        "api_conversations",
        sa.Column(
            "model_id",
            sa.String(length=255),
            nullable=False,
            server_default=_LEGACY_MODEL,
        ),
    )
    op.add_column(
        "api_responses",
        sa.Column(
            "model_id",
            sa.String(length=255),
            nullable=False,
            server_default=_LEGACY_MODEL,
        ),
    )
    op.alter_column("api_conversations", "model_id", server_default=None)
    op.alter_column("api_responses", "model_id", server_default=None)
    op.create_table(
        "api_model_availability",
        sa.Column("model_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=True),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("model_id"),
    )
    op.create_index(
        "ix_api_model_availability_status",
        "api_model_availability",
        ["status", "checked_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_api_model_availability_status",
        table_name="api_model_availability",
    )
    op.drop_table("api_model_availability")
    op.drop_column("api_responses", "model_id")
    op.drop_column("api_conversations", "model_id")
