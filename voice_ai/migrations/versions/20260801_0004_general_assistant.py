"""Decouple conversations and responses from telco subscribers.

Revision ID: 20260801_0004
Revises: 20260731_0003
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260801_0004"
down_revision: str | None = "20260731_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "api_conversations",
        "subscriber_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
    op.alter_column(
        "api_responses",
        "subscriber_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM api_responses WHERE subscriber_id IS NULL; "
            "DELETE FROM api_conversations WHERE subscriber_id IS NULL"
        )
    )
    op.alter_column(
        "api_responses",
        "subscriber_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
    op.alter_column(
        "api_conversations",
        "subscriber_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
