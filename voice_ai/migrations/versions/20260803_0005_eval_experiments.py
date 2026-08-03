"""Add durable agent evaluation experiments.

Revision ID: 20260803_0005
Revises: 20260801_0004
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260803_0005"
down_revision: str | None = "20260801_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_eval_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("suite_name", sa.String(length=255), nullable=False),
        sa.Column("suite_version", sa.String(length=64), nullable=False),
        sa.Column("dataset_digest", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=24), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("assertion_pass_rate", sa.Float(), nullable=False),
        sa.Column("failed_cases", sa.Integer(), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("source_revision", sa.String(length=255), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("span_id", sa.String(length=32), nullable=True),
        sa.Column("config_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("report_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_eval_runs_suite",
        "agent_eval_runs",
        ["suite_name", "suite_version", "completed_at"],
    )
    op.create_index(
        "ix_agent_eval_runs_model",
        "agent_eval_runs",
        ["model", "completed_at"],
    )
    op.create_table(
        "agent_eval_case_results",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("case_name", sa.String(length=255), nullable=False),
        sa.Column("source_case_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("task_duration_ms", sa.Float(), nullable=True),
        sa.Column("total_duration_ms", sa.Float(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("span_id", sa.String(length=32), nullable=True),
        sa.Column("output_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("assertions_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("scores_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metrics_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("case_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_eval_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "sequence_number"),
    )
    op.create_index(
        "ix_agent_eval_cases_name",
        "agent_eval_case_results",
        ["case_name", "run_id"],
    )
    op.create_index(
        "ix_agent_eval_cases_status",
        "agent_eval_case_results",
        ["status", "run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_eval_cases_status", table_name="agent_eval_case_results")
    op.drop_index("ix_agent_eval_cases_name", table_name="agent_eval_case_results")
    op.drop_table("agent_eval_case_results")
    op.drop_index("ix_agent_eval_runs_model", table_name="agent_eval_runs")
    op.drop_index("ix_agent_eval_runs_suite", table_name="agent_eval_runs")
    op.drop_table("agent_eval_runs")
