from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from voice_ai.agent.persistence.model import TableModel

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class EvalRun(TableModel, table=True):
    """Immutable experiment snapshot for one versioned evaluation run."""

    __tablename__ = "agent_eval_runs"

    id: str = Field(sa_type=String(64), primary_key=True)
    suite_name: str = Field(sa_type=String(255), nullable=False)
    suite_version: str = Field(sa_type=String(64), nullable=False)
    dataset_digest: str = Field(sa_type=String(64), nullable=False)
    mode: str = Field(sa_type=String(24), nullable=False)
    model: str = Field(sa_type=String(255), nullable=False)
    status: str = Field(sa_type=String(24), nullable=False)
    passed: bool = Field(sa_type=Boolean, nullable=False)
    assertion_pass_rate: float = Field(sa_type=Float, nullable=False)
    failed_cases: int = Field(sa_type=Integer, nullable=False)
    case_count: int = Field(sa_type=Integer, nullable=False)
    started_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    completed_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    duration_ms: float = Field(sa_type=Float, nullable=False)
    source_revision: str | None = Field(default=None, sa_type=String(255), nullable=True)
    trace_id: str | None = Field(default=None, sa_type=String(64), nullable=True)
    span_id: str | None = Field(default=None, sa_type=String(32), nullable=True)
    config_json: dict[str, Any] = Field(sa_type=JSON_DOCUMENT, nullable=False)
    report_json: dict[str, Any] = Field(sa_type=JSON_DOCUMENT, nullable=False)

    __table_args__ = (
        Index(
            "ix_agent_eval_runs_suite",
            "suite_name",
            "suite_version",
            "completed_at",
        ),
        Index("ix_agent_eval_runs_model", "model", "completed_at"),
    )


class EvalCaseResult(TableModel, table=True):
    """Queryable per-case projection backed by the immutable report snapshot."""

    __tablename__ = "agent_eval_case_results"

    run_id: str = Field(
        sa_type=String(64),
        foreign_key="agent_eval_runs.id",
        ondelete="CASCADE",
        primary_key=True,
    )
    sequence_number: int = Field(sa_type=Integer, primary_key=True)
    case_name: str = Field(sa_type=String(255), nullable=False)
    source_case_name: str | None = Field(
        default=None, sa_type=String(255), nullable=True
    )
    status: str = Field(sa_type=String(24), nullable=False)
    task_duration_ms: float | None = Field(default=None, sa_type=Float, nullable=True)
    total_duration_ms: float | None = Field(default=None, sa_type=Float, nullable=True)
    trace_id: str | None = Field(default=None, sa_type=String(64), nullable=True)
    span_id: str | None = Field(default=None, sa_type=String(32), nullable=True)
    output_json: Any | None = Field(default=None, sa_type=JSON_DOCUMENT, nullable=True)
    assertions_json: dict[str, Any] = Field(sa_type=JSON_DOCUMENT, nullable=False)
    scores_json: dict[str, Any] = Field(sa_type=JSON_DOCUMENT, nullable=False)
    metrics_json: dict[str, Any] = Field(sa_type=JSON_DOCUMENT, nullable=False)
    error_json: dict[str, Any] | None = Field(
        default=None, sa_type=JSON_DOCUMENT, nullable=True
    )
    case_json: dict[str, Any] = Field(sa_type=JSON_DOCUMENT, nullable=False)

    __table_args__ = (
        Index("ix_agent_eval_cases_name", "case_name", "run_id"),
        Index("ix_agent_eval_cases_status", "status", "run_id"),
    )
