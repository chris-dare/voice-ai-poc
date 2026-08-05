"""Add durable response dispatch and shared API rate limits.

Revision ID: 20260803_0006
Revises: 20260803_0005
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0006"
down_revision: str | None = "20260803_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_response_jobs",
        sa.Column("response_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["response_id"], ["api_responses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("response_id"),
    )
    op.create_index(
        "ix_api_response_jobs_claim",
        "api_response_jobs",
        ["status", "available_at", "lease_expires_at"],
    )
    op.create_table(
        "api_rate_limit_buckets",
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "subject_id", "window_started_at"),
    )
    op.create_index(
        "ix_api_rate_limit_expiry",
        "api_rate_limit_buckets",
        ["expires_at"],
    )
    op.create_table(
        "api_worker_nodes",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("concurrency", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_api_worker_nodes_heartbeat",
        "api_worker_nodes",
        ["status", "heartbeat_at"],
    )

    now = sa.func.now()
    jobs = sa.table(
        "api_response_jobs",
        sa.column("response_id", sa.String()),
        sa.column("status", sa.String()),
        sa.column("decision", sa.String()),
        sa.column("attempt_count", sa.Integer()),
        sa.column("max_attempts", sa.Integer()),
        sa.column("available_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    responses = sa.table(
        "api_responses",
        sa.column("id", sa.String()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        jobs.insert().from_select(
            [
                "response_id",
                "status",
                "decision",
                "attempt_count",
                "max_attempts",
                "available_at",
                "created_at",
                "updated_at",
            ],
            sa.select(
                responses.c.id,
                sa.literal("pending"),
                sa.cast(sa.null(), sa.String(length=16)),
                sa.literal(0),
                sa.literal(3),
                now,
                responses.c.created_at,
                now,
            ).where(responses.c.status.in_(["queued", "in_progress"])),
        )
    )


def downgrade() -> None:
    op.drop_index("ix_api_worker_nodes_heartbeat", table_name="api_worker_nodes")
    op.drop_table("api_worker_nodes")
    op.drop_index("ix_api_rate_limit_expiry", table_name="api_rate_limit_buckets")
    op.drop_table("api_rate_limit_buckets")
    op.drop_index("ix_api_response_jobs_claim", table_name="api_response_jobs")
    op.drop_table("api_response_jobs")
