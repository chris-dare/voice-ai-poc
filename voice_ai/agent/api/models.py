from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlmodel import Field

from voice_ai.agent.persistence.model import TableModel


class Conversation(TableModel, table=True):
    __tablename__ = "api_conversations"

    id: str = Field(sa_type=String(64), primary_key=True)
    runtime_session_id: UUID = Field(unique=True, nullable=False)
    tenant_id: str = Field(sa_type=String(255), nullable=False)
    subject_id: str = Field(sa_type=String(255), nullable=False)
    subscriber_id: UUID | None = Field(default=None, nullable=True)
    agent_id: str = Field(sa_type=String(120), nullable=False)
    model_id: str = Field(sa_type=String(255), nullable=False)
    status: str = Field(sa_type=String(24), nullable=False)
    metadata_json: dict[str, Any] = Field(sa_type=JSON, nullable=False)
    session_state: dict[str, Any] = Field(sa_type=JSON, nullable=False)
    created_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    updated_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_api_conversations_owner", "tenant_id", "subject_id", "updated_at"),)


class ResponseRecord(TableModel, table=True):
    __tablename__ = "api_responses"

    id: str = Field(sa_type=String(64), primary_key=True)
    conversation_id: str | None = Field(
        default=None,
        sa_type=String(64),
        foreign_key="api_conversations.id",
        ondelete="CASCADE",
        nullable=True,
    )
    runtime_session_id: UUID = Field(nullable=False)
    tenant_id: str = Field(sa_type=String(255), nullable=False)
    subject_id: str = Field(sa_type=String(255), nullable=False)
    subscriber_id: UUID | None = Field(default=None, nullable=True)
    agent_id: str = Field(sa_type=String(120), nullable=False)
    model_id: str = Field(sa_type=String(255), nullable=False)
    status: str = Field(sa_type=String(24), nullable=False)
    created_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    started_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )
    completed_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )
    cancellation_reason: str | None = Field(default=None, sa_type=String(64), nullable=True)
    input_json: list[dict[str, Any]] = Field(sa_type=JSON, nullable=False)
    session_state: dict[str, Any] = Field(sa_type=JSON, nullable=False)
    output_json: list[dict[str, Any]] = Field(sa_type=JSON, nullable=False)
    required_action_json: dict[str, Any] | None = Field(default=None, sa_type=JSON, nullable=True)
    error_json: dict[str, Any] | None = Field(default=None, sa_type=JSON, nullable=True)
    usage_json: dict[str, Any] | None = Field(default=None, sa_type=JSON, nullable=True)
    metadata_json: dict[str, Any] = Field(sa_type=JSON, nullable=False)
    execution_snapshot_json: dict[str, Any] = Field(sa_type=JSON, nullable=False)
    background: bool = Field(sa_type=Boolean, nullable=False)
    stream: bool = Field(sa_type=Boolean, nullable=False)

    __table_args__ = (
        Index("ix_api_responses_owner", "tenant_id", "subject_id", "created_at"),
        Index(
            "uq_api_responses_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text(
                "conversation_id IS NOT NULL AND status IN "
                "('queued', 'in_progress', 'requires_action')"
            ),
            sqlite_where=text(
                "conversation_id IS NOT NULL AND status IN "
                "('queued', 'in_progress', 'requires_action')"
            ),
        ),
    )


class ResponseEvent(TableModel, table=True):
    __tablename__ = "api_response_events"

    response_id: str = Field(
        sa_type=String(64),
        foreign_key="api_responses.id",
        ondelete="CASCADE",
        primary_key=True,
    )
    sequence_number: int = Field(sa_type=Integer, primary_key=True)
    type: str = Field(sa_type=String(80), nullable=False)
    created_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    data_json: dict[str, Any] = Field(sa_type=JSON, nullable=False)

    __table_args__ = (Index("ix_api_response_events_created", "created_at"),)


class ResponseJob(TableModel, table=True):
    """Durable, lease-based execution record for an accepted response."""

    __tablename__ = "api_response_jobs"

    response_id: str = Field(
        sa_type=String(64),
        foreign_key="api_responses.id",
        ondelete="CASCADE",
        primary_key=True,
    )
    status: str = Field(sa_type=String(24), nullable=False)
    decision: str | None = Field(default=None, sa_type=String(16), nullable=True)
    attempt_count: int = Field(default=0, sa_type=Integer, nullable=False)
    max_attempts: int = Field(default=3, sa_type=Integer, nullable=False)
    available_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    lease_owner: str | None = Field(default=None, sa_type=String(255), nullable=True)
    lease_expires_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )
    heartbeat_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )
    created_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    updated_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_api_response_jobs_claim",
            "status",
            "available_at",
            "lease_expires_at",
        ),
    )


class RateLimitBucket(TableModel, table=True):
    """Shared fixed-window request counter used by every API replica."""

    __tablename__ = "api_rate_limit_buckets"

    tenant_id: str = Field(sa_type=String(255), primary_key=True)
    subject_id: str = Field(sa_type=String(255), primary_key=True)
    window_started_at: datetime = Field(sa_type=DateTime(timezone=True), primary_key=True)
    request_count: int = Field(default=0, sa_type=Integer, nullable=False)
    expires_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_api_rate_limit_expiry", "expires_at"),)


class WorkerNode(TableModel, table=True):
    """Liveness record for an independently deployed response worker."""

    __tablename__ = "api_worker_nodes"

    id: str = Field(sa_type=String(255), primary_key=True)
    status: str = Field(sa_type=String(24), nullable=False)
    concurrency: int = Field(sa_type=Integer, nullable=False)
    started_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    heartbeat_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_api_worker_nodes_heartbeat", "status", "heartbeat_at"),)


class ModelAvailability(TableModel, table=True):
    """Shared provider/model health observed by all API and worker replicas."""

    __tablename__ = "api_model_availability"

    model_id: str = Field(sa_type=String(255), primary_key=True)
    status: str = Field(sa_type=String(24), nullable=False)
    reason_code: str | None = Field(default=None, sa_type=String(80), nullable=True)
    detail: str | None = Field(default=None, sa_type=String(500), nullable=True)
    checked_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    last_success_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )
    last_failure_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_api_model_availability_status", "status", "checked_at"),)


class ExecutionCapacityLease(TableModel, table=True):
    """Fleet-wide leased slot for a model route or tool execution."""

    __tablename__ = "api_execution_capacity_leases"

    id: str = Field(sa_type=String(64), primary_key=True)
    resource_kind: str = Field(sa_type=String(24), nullable=False)
    resource_key: str = Field(sa_type=String(255), nullable=False)
    owner_id: str = Field(sa_type=String(255), nullable=False)
    acquired_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    heartbeat_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    expires_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_api_execution_capacity_resource",
            "resource_kind",
            "resource_key",
            "expires_at",
        ),
        Index("ix_api_execution_capacity_expiry", "expires_at"),
    )


class RequiredAction(TableModel, table=True):
    __tablename__ = "api_required_actions"

    id: str = Field(sa_type=String(64), primary_key=True)
    response_id: str = Field(
        sa_type=String(64),
        foreign_key="api_responses.id",
        ondelete="CASCADE",
        unique=True,
        nullable=False,
    )
    tenant_id: str = Field(sa_type=String(255), nullable=False)
    subject_id: str = Field(sa_type=String(255), nullable=False)
    type: str = Field(sa_type=String(32), nullable=False)
    title: str = Field(sa_type=String(240), nullable=False)
    description: str = Field(sa_type=Text, nullable=False)
    expires_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    decision: str | None = Field(default=None, sa_type=String(16), nullable=True)
    decided_at: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True), nullable=True
    )
    created_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_api_required_actions_expiry", "expires_at", "decision"),)


class IdempotencyRecord(TableModel, table=True):
    __tablename__ = "api_idempotency_records"

    id: int | None = Field(default=None, sa_type=Integer, primary_key=True)
    tenant_id: str = Field(sa_type=String(255), nullable=False)
    subject_id: str = Field(sa_type=String(255), nullable=False)
    method: str = Field(sa_type=String(12), nullable=False)
    route: str = Field(sa_type=String(255), nullable=False)
    key: str = Field(sa_type=String(255), nullable=False)
    fingerprint: str = Field(sa_type=String(64), nullable=False)
    resource_type: str = Field(sa_type=String(32), nullable=False)
    resource_id: str = Field(sa_type=String(64), nullable=False)
    created_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)
    expires_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "subject_id",
            "method",
            "route",
            "key",
            name="uq_api_idempotency_scope",
        ),
        Index("ix_api_idempotency_expiry", "expires_at"),
    )


class IdentityBinding(TableModel, table=True):
    """Tenant-scoped association between an authenticated subject and a subscriber."""

    __tablename__ = "api_identity_bindings"

    tenant_id: str = Field(sa_type=String(255), primary_key=True)
    subject_id: str = Field(sa_type=String(255), primary_key=True)
    subscriber_id: UUID = Field(
        foreign_key="subscribers.id",
        ondelete="RESTRICT",
        nullable=False,
    )
    active: bool = Field(default=True, sa_type=Boolean, nullable=False)
    created_at: datetime = Field(sa_type=DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_api_identity_bindings_subscriber", "subscriber_id"),)
