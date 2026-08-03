from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from voice_ai.agent.persistence.database import Base

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class EvalRun(Base):
    """Immutable experiment snapshot for one versioned evaluation run."""

    __tablename__ = "agent_eval_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    suite_name: Mapped[str] = mapped_column(String(255), nullable=False)
    suite_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    assertion_pass_rate: Mapped[float] = mapped_column(Float, nullable=False)
    failed_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    source_revision: Mapped[str | None] = mapped_column(String(255))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    span_id: Mapped[str | None] = mapped_column(String(32))
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)

    __table_args__ = (
        Index(
            "ix_agent_eval_runs_suite",
            "suite_name",
            "suite_version",
            "completed_at",
        ),
        Index("ix_agent_eval_runs_model", "model", "completed_at"),
    )


class EvalCaseResult(Base):
    """Queryable per-case projection backed by the immutable report snapshot."""

    __tablename__ = "agent_eval_case_results"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_eval_runs.id", ondelete="CASCADE"), primary_key=True
    )
    sequence_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_case_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    task_duration_ms: Mapped[float | None] = mapped_column(Float)
    total_duration_ms: Mapped[float | None] = mapped_column(Float)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    span_id: Mapped[str | None] = mapped_column(String(32))
    output_json: Mapped[Any | None] = mapped_column(JSON_DOCUMENT)
    assertions_json: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    scores_json: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    case_json: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)

    __table_args__ = (
        Index("ix_agent_eval_cases_name", "case_name", "run_id"),
        Index("ix_agent_eval_cases_status", "status", "run_id"),
    )
