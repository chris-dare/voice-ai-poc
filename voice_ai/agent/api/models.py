from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from voice_ai.agent.persistence.database import Base


class Conversation(Base):
    __tablename__ = "api_conversations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    runtime_session_id: Mapped[UUID] = mapped_column(unique=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subscriber_id: Mapped[UUID | None] = mapped_column(nullable=True)
    agent_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    session_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_api_conversations_owner", "tenant_id", "subject_id", "updated_at"),
    )


class ResponseRecord(Base):
    __tablename__ = "api_responses"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("api_conversations.id", ondelete="CASCADE"), nullable=True
    )
    runtime_session_id: Mapped[UUID] = mapped_column(nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subscriber_id: Mapped[UUID | None] = mapped_column(nullable=True)
    agent_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(String(64))
    input_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    session_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    output_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    required_action_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    usage_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    background: Mapped[bool] = mapped_column(Boolean, nullable=False)
    stream: Mapped[bool] = mapped_column(Boolean, nullable=False)

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


class ResponseEvent(Base):
    __tablename__ = "api_response_events"

    response_id: Mapped[str] = mapped_column(
        ForeignKey("api_responses.id", ondelete="CASCADE"), primary_key=True
    )
    sequence_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_api_response_events_created", "created_at"),
    )


class RequiredAction(Base):
    __tablename__ = "api_required_actions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    response_id: Mapped[str] = mapped_column(
        ForeignKey("api_responses.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(16))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_api_required_actions_expiry", "expires_at", "decision"),
    )


class IdempotencyRecord(Base):
    __tablename__ = "api_idempotency_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    method: Mapped[str] = mapped_column(String(12), nullable=False)
    route: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

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


class IdentityBinding(Base):
    """Tenant-scoped association between an authenticated subject and a subscriber."""

    __tablename__ = "api_identity_bindings"

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    subscriber_id: Mapped[UUID] = mapped_column(
        ForeignKey("subscribers.id", ondelete="RESTRICT"), nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_api_identity_bindings_subscriber", "subscriber_id"),)
