from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from voice_ai.agent.api.models import IdentityBinding
from voice_ai.agent.persistence.database import Database
from voice_ai.agent.telco.models import Subscriber


async def resolve_subscriber(
    database: Database,
    *,
    tenant_id: str,
    subject_id: str,
) -> UUID | None:
    async with database.session() as session:
        return await session.scalar(
            select(IdentityBinding.subscriber_id).where(
                IdentityBinding.tenant_id == tenant_id,
                IdentityBinding.subject_id == subject_id,
                IdentityBinding.active.is_(True),
            )
        )


async def bind_identity(
    database: Database,
    *,
    tenant_id: str,
    subject_id: str,
    subscriber_id: UUID,
) -> IdentityBinding:
    async with database.session_factory.begin() as session:
        if await session.get(Subscriber, subscriber_id) is None:
            raise ValueError(f"Subscriber {subscriber_id} does not exist")
        binding = await session.get(IdentityBinding, (tenant_id, subject_id))
        if binding is None:
            binding = IdentityBinding(
                tenant_id=tenant_id,
                subject_id=subject_id,
                subscriber_id=subscriber_id,
                active=True,
                created_at=datetime.now(UTC),
            )
            session.add(binding)
        else:
            binding.subscriber_id = subscriber_id
            binding.active = True
    return binding
