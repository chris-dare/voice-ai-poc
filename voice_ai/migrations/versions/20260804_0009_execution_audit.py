"""Persist immutable response execution snapshots.

Revision ID: 20260804_0009
Revises: 20260804_0008
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0009"
down_revision: str | None = "20260804_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_responses",
        sa.Column(
            "execution_snapshot_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.alter_column("api_responses", "execution_snapshot_json", server_default=None)


def downgrade() -> None:
    op.drop_column("api_responses", "execution_snapshot_json")
