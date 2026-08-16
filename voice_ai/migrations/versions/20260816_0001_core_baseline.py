"""Create the frozen general-purpose core schema for a first deployment.

Revision ID: 20260816_0001
Create Date: 2026-08-16

This is a schema snapshot, not a dynamic reflection of current ORM models.
Future model changes must be represented by new explicit migrations. Domain-
specific schemas belong in external MCP services and are not created here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260816_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON()
    eval_json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "agent_eval_runs",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("suite_name", sa.String(255), nullable=False),
        sa.Column("suite_version", sa.String(64), nullable=False),
        sa.Column("dataset_digest", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("assertion_pass_rate", sa.Float(), nullable=False),
        sa.Column("failed_cases", sa.Integer(), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("source_revision", sa.String(255)),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("span_id", sa.String(32)),
        sa.Column("config_json", eval_json_type, nullable=False),
        sa.Column("report_json", eval_json_type, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_eval_runs_model", "agent_eval_runs", ["model", "completed_at"])
    op.create_index(
        "ix_agent_eval_runs_suite",
        "agent_eval_runs",
        ["suite_name", "suite_version", "completed_at"],
    )

    op.create_table(
        "api_conversations",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("runtime_session_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column("agent_id", sa.String(120), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("metadata_json", json_type, nullable=False),
        sa.Column("session_state", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("runtime_session_id"),
    )
    op.create_index(
        "ix_api_conversations_owner", "api_conversations", ["tenant_id", "subject_id", "updated_at"]
    )

    op.create_table(
        "api_execution_capacity_leases",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("resource_kind", sa.String(24), nullable=False),
        sa.Column("resource_key", sa.String(255), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_api_execution_capacity_expiry", "api_execution_capacity_leases", ["expires_at"]
    )
    op.create_index(
        "ix_api_execution_capacity_resource",
        "api_execution_capacity_leases",
        ["resource_kind", "resource_key", "expires_at"],
    )

    op.create_table(
        "api_idempotency_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column("method", sa.String(12), nullable=False),
        sa.Column("route", sa.String(255), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "subject_id", "method", "route", "key", name="uq_api_idempotency_scope"
        ),
    )
    op.create_index("ix_api_idempotency_expiry", "api_idempotency_records", ["expires_at"])

    op.create_table(
        "api_model_availability",
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reason_code", sa.String(80)),
        sa.Column("detail", sa.String(500)),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("model_id"),
    )
    op.create_index(
        "ix_api_model_availability_status", "api_model_availability", ["status", "checked_at"]
    )

    op.create_table(
        "api_rate_limit_buckets",
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "subject_id", "window_started_at"),
    )
    op.create_index("ix_api_rate_limit_expiry", "api_rate_limit_buckets", ["expires_at"])

    op.create_table(
        "api_worker_nodes",
        sa.Column("id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("concurrency", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_api_worker_nodes_heartbeat", "api_worker_nodes", ["status", "heartbeat_at"])

    op.create_table(
        "agent_eval_case_results",
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("case_name", sa.String(255), nullable=False),
        sa.Column("source_case_name", sa.String(255)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("task_duration_ms", sa.Float()),
        sa.Column("total_duration_ms", sa.Float()),
        sa.Column("trace_id", sa.String(64)),
        sa.Column("span_id", sa.String(32)),
        sa.Column("output_json", eval_json_type),
        sa.Column("assertions_json", eval_json_type, nullable=False),
        sa.Column("scores_json", eval_json_type, nullable=False),
        sa.Column("metrics_json", eval_json_type, nullable=False),
        sa.Column("error_json", eval_json_type),
        sa.Column("case_json", eval_json_type, nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_eval_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "sequence_number"),
    )
    op.create_index("ix_agent_eval_cases_name", "agent_eval_case_results", ["case_name", "run_id"])
    op.create_index("ix_agent_eval_cases_status", "agent_eval_case_results", ["status", "run_id"])

    op.create_table(
        "api_responses",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("conversation_id", sa.String(64)),
        sa.Column("runtime_session_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column("agent_id", sa.String(120), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_reason", sa.String(64)),
        sa.Column("input_json", json_type, nullable=False),
        sa.Column("session_state", json_type, nullable=False),
        sa.Column("output_json", json_type, nullable=False),
        sa.Column("required_action_json", json_type),
        sa.Column("error_json", json_type),
        sa.Column("usage_json", json_type),
        sa.Column("metadata_json", json_type, nullable=False),
        sa.Column("execution_snapshot_json", json_type, nullable=False),
        sa.Column("background", sa.Boolean(), nullable=False),
        sa.Column("stream", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["api_conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_api_responses_owner", "api_responses", ["tenant_id", "subject_id", "created_at"]
    )
    active_where = sa.text(
        "conversation_id IS NOT NULL AND status IN ('queued', 'in_progress', 'requires_action')"
    )
    op.create_index(
        "uq_api_responses_active_conversation",
        "api_responses",
        ["conversation_id"],
        unique=True,
        postgresql_where=active_where,
        sqlite_where=active_where,
    )

    op.create_table(
        "api_required_actions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("response_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.String(16)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["response_id"], ["api_responses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("response_id"),
    )
    op.create_index(
        "ix_api_required_actions_expiry", "api_required_actions", ["expires_at", "decision"]
    )

    op.create_table(
        "api_response_events",
        sa.Column("response_id", sa.String(64), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_json", json_type, nullable=False),
        sa.ForeignKeyConstraint(["response_id"], ["api_responses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("response_id", "sequence_number"),
    )
    op.create_index("ix_api_response_events_created", "api_response_events", ["created_at"])

    op.create_table(
        "api_response_jobs",
        sa.Column("response_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("decision", sa.String(16)),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(255)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
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


def downgrade() -> None:
    op.drop_index("ix_api_response_jobs_claim", table_name="api_response_jobs")
    op.drop_table("api_response_jobs")
    op.drop_index("ix_api_response_events_created", table_name="api_response_events")
    op.drop_table("api_response_events")
    op.drop_index("ix_api_required_actions_expiry", table_name="api_required_actions")
    op.drop_table("api_required_actions")
    op.drop_index("uq_api_responses_active_conversation", table_name="api_responses")
    op.drop_index("ix_api_responses_owner", table_name="api_responses")
    op.drop_table("api_responses")
    op.drop_index("ix_agent_eval_cases_status", table_name="agent_eval_case_results")
    op.drop_index("ix_agent_eval_cases_name", table_name="agent_eval_case_results")
    op.drop_table("agent_eval_case_results")
    op.drop_index("ix_api_worker_nodes_heartbeat", table_name="api_worker_nodes")
    op.drop_table("api_worker_nodes")
    op.drop_index("ix_api_rate_limit_expiry", table_name="api_rate_limit_buckets")
    op.drop_table("api_rate_limit_buckets")
    op.drop_index("ix_api_model_availability_status", table_name="api_model_availability")
    op.drop_table("api_model_availability")
    op.drop_index("ix_api_idempotency_expiry", table_name="api_idempotency_records")
    op.drop_table("api_idempotency_records")
    op.drop_index("ix_api_execution_capacity_resource", table_name="api_execution_capacity_leases")
    op.drop_index("ix_api_execution_capacity_expiry", table_name="api_execution_capacity_leases")
    op.drop_table("api_execution_capacity_leases")
    op.drop_index("ix_api_conversations_owner", table_name="api_conversations")
    op.drop_table("api_conversations")
    op.drop_index("ix_agent_eval_runs_suite", table_name="agent_eval_runs")
    op.drop_index("ix_agent_eval_runs_model", table_name="agent_eval_runs")
    op.drop_table("agent_eval_runs")
