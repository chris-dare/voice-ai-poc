from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID


class ConfirmationError(RuntimeError):
    pass


@dataclass(slots=True)
class PendingPlanChange:
    subscriber_id: UUID
    plan_code: str
    plan_name: str
    quoted_price: Decimal
    currency: str
    nonce: str
    created_at: datetime
    created_user_turn: int
    confirmed: bool = False
    consumed: bool = False


class ConfirmationGate:
    _yes = frozenset(
        {
            "yes",
            "yes please",
            "i confirm",
            "confirm",
            "go ahead",
            "please do",
            "do it",
            "that is correct",
            "that's correct",
        }
    )
    _no = frozenset(
        {
            "no",
            "no thanks",
            "cancel",
            "stop",
            "never mind",
            "nevermind",
            "do not",
            "don't",
        }
    )

    def __init__(self, *, ttl_seconds: int = 90) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._pending: PendingPlanChange | None = None
        self._user_turn = 0

    @property
    def pending(self) -> PendingPlanChange | None:
        pending = self._pending
        if pending and self._expired(pending):
            self._pending = None
            return None
        return pending

    def prepare(
        self,
        *,
        subscriber_id: UUID,
        plan_code: str,
        plan_name: str,
        quoted_price: Decimal,
        currency: str,
    ) -> PendingPlanChange:
        pending = PendingPlanChange(
            subscriber_id=subscriber_id,
            plan_code=plan_code,
            plan_name=plan_name,
            quoted_price=quoted_price,
            currency=currency,
            nonce=secrets.token_urlsafe(24),
            created_at=datetime.now(UTC),
            created_user_turn=self._user_turn,
        )
        self._pending = pending
        return pending

    def observe_user_turn(self, transcript: str) -> str:
        self._user_turn += 1
        pending = self.pending
        if pending is None:
            return "none"
        normalized = _normalize(transcript)
        if normalized in self._no:
            self._pending = None
            return "cancelled"
        if normalized in self._yes and self._user_turn > pending.created_user_turn:
            pending.confirmed = True
            return "confirmed"
        self._pending = None
        return "ambiguous"

    def authorize(self, *, subscriber_id: UUID, plan_code: str) -> str:
        pending = self.pending
        if pending is None:
            raise ConfirmationError("No active plan change is awaiting confirmation")
        if pending.subscriber_id != subscriber_id or pending.plan_code != plan_code:
            self._pending = None
            raise ConfirmationError("The confirmed action does not match this plan change")
        if not pending.confirmed:
            raise ConfirmationError("The subscriber has not confirmed this change in a later turn")
        if pending.consumed:
            raise ConfirmationError("This confirmation has already been used")
        pending.consumed = True
        self._pending = None
        return pending.nonce

    def clear(self) -> None:
        self._pending = None

    def snapshot(self) -> dict[str, Any]:
        pending = self.pending
        return {
            "user_turn": self._user_turn,
            "pending": (
                {
                    "subscriber_id": str(pending.subscriber_id),
                    "plan_code": pending.plan_code,
                    "plan_name": pending.plan_name,
                    "quoted_price": str(pending.quoted_price),
                    "currency": pending.currency,
                    "nonce": pending.nonce,
                    "created_at": pending.created_at.isoformat(),
                    "created_user_turn": pending.created_user_turn,
                    "confirmed": pending.confirmed,
                    "consumed": pending.consumed,
                }
                if pending
                else None
            ),
        }

    def restore(self, value: dict[str, Any]) -> None:
        self._user_turn = int(value.get("user_turn") or 0)
        raw = value.get("pending")
        if not isinstance(raw, dict):
            self._pending = None
            return
        self._pending = PendingPlanChange(
            subscriber_id=UUID(str(raw["subscriber_id"])),
            plan_code=str(raw["plan_code"]),
            plan_name=str(raw["plan_name"]),
            quoted_price=Decimal(str(raw["quoted_price"])),
            currency=str(raw["currency"]),
            nonce=str(raw["nonce"]),
            created_at=datetime.fromisoformat(str(raw["created_at"])),
            created_user_turn=int(raw["created_user_turn"]),
            confirmed=bool(raw.get("confirmed")),
            consumed=bool(raw.get("consumed")),
        )
        _ = self.pending

    def _expired(self, pending: PendingPlanChange) -> bool:
        return datetime.now(UTC) - pending.created_at > self._ttl


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9' ]+", "", " ".join(text.lower().split())).strip()
