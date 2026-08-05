"""Add durable agent API resources.

Revision ID: 20260731_0002
Revises: 20260727_0001
Create Date: 2026-07-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260731_0002"
down_revision: str | None = "20260727_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_conversations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("runtime_session_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("subscriber_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("session_state", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("runtime_session_id"),
    )
    op.create_index(
        "ix_api_conversations_owner",
        "api_conversations",
        ["tenant_id", "subject_id", "updated_at"],
    )

    op.create_table(
        "api_responses",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=True),
        sa.Column("runtime_session_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("subscriber_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.String(length=64), nullable=True),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("session_state", sa.JSON(), nullable=False),
        sa.Column("output_json", sa.JSON(), nullable=False),
        sa.Column("required_action_json", sa.JSON(), nullable=True),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("usage_json", sa.JSON(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("background", sa.Boolean(), nullable=False),
        sa.Column("stream", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["api_conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_api_responses_owner",
        "api_responses",
        ["tenant_id", "subject_id", "created_at"],
    )
    op.create_index(
        "uq_api_responses_active_conversation",
        "api_responses",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text(
            "conversation_id IS NOT NULL AND status IN ('queued', 'in_progress', 'requires_action')"
        ),
    )

    op.create_table(
        "api_response_events",
        sa.Column("response_id", sa.String(length=64), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["response_id"], ["api_responses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("response_id", "sequence_number"),
    )
    op.create_index(
        "ix_api_response_events_created",
        "api_response_events",
        ["created_at"],
    )

    op.create_table(
        "api_required_actions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("response_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["response_id"], ["api_responses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("response_id"),
    )
    op.create_index(
        "ix_api_required_actions_expiry",
        "api_required_actions",
        ["expires_at", "decision"],
    )

    op.create_table(
        "api_idempotency_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("method", sa.String(length=12), nullable=False),
        sa.Column("route", sa.String(length=255), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "subject_id",
            "method",
            "route",
            "key",
            name="uq_api_idempotency_scope",
        ),
    )
    op.create_index(
        "ix_api_idempotency_expiry",
        "api_idempotency_records",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_api_idempotency_expiry", table_name="api_idempotency_records")
    op.drop_table("api_idempotency_records")
    op.drop_index("ix_api_required_actions_expiry", table_name="api_required_actions")
    op.drop_table("api_required_actions")
    op.drop_index("ix_api_response_events_created", table_name="api_response_events")
    op.drop_table("api_response_events")
    op.drop_index("uq_api_responses_active_conversation", table_name="api_responses")
    op.drop_index("ix_api_responses_owner", table_name="api_responses")
    op.drop_table("api_responses")
    op.drop_index("ix_api_conversations_owner", table_name="api_conversations")
    op.drop_table("api_conversations")
