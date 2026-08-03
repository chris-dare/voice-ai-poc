"""Add explicit authenticated identity-to-subscriber bindings.

Revision ID: 20260731_0003
Revises: 20260731_0002
Create Date: 2026-07-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260731_0003"
down_revision: str | None = "20260731_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_identity_bindings",
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("subscriber_id", sa.Uuid(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["subscriber_id"], ["subscribers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tenant_id", "subject_id"),
    )
    op.create_index(
        "ix_api_identity_bindings_subscriber",
        "api_identity_bindings",
        ["subscriber_id"],
    )

    # Preserve known, previously authenticated ownership without introducing a
    # universal subscriber fallback. Conflicting historical bindings are skipped.
    op.execute(
        sa.text(
            """
            INSERT INTO api_identity_bindings
                (tenant_id, subject_id, subscriber_id, active, created_at)
            SELECT tenant_id, subject_id, min(subscriber_id::text)::uuid, true, now()
            FROM api_conversations
            GROUP BY tenant_id, subject_id
            HAVING count(DISTINCT subscriber_id) = 1
            ON CONFLICT (tenant_id, subject_id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_api_identity_bindings_subscriber",
        table_name="api_identity_bindings",
    )
    op.drop_table("api_identity_bindings")
