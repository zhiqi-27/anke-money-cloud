from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.storage.protocols import HouseholdStorage


@dataclass(frozen=True, slots=True)
class EffectiveEntitlement:
    active: bool
    sources: tuple[str, ...]
    expires_at: datetime | None
    apple_documents: tuple[dict, ...]
    manual_documents: tuple[dict, ...]


class EntitlementResolver:
    """Calculate Pro access from provider evidence and independent manual grants."""

    def __init__(self, storage: HouseholdStorage):
        self._storage = storage

    def resolve(self, uid: str, now: datetime | None = None) -> EffectiveEntitlement:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        apple_documents = tuple(self._storage.subscription_entitlements(uid))
        manual_documents = tuple(self._storage.manual_pro_grants(uid))
        active_apple = tuple(
            document
            for document in apple_documents
            if self._active_apple(document, current)
        )
        active_manual = tuple(
            document
            for document in manual_documents
            if self._active_manual(document, current)
        )
        sources: list[str] = []
        if active_apple:
            sources.append("apple")
        if active_manual:
            sources.append("manualGrant")
        active_documents = active_apple + active_manual
        expirations = [
            parsed
            for document in active_documents
            if (parsed := self._parse_datetime(document.get("expiresAt"))) is not None
        ]
        return EffectiveEntitlement(
            active=bool(active_documents),
            sources=tuple(sources),
            expires_at=None if any(
                not document.get("expiresAt") for document in active_documents
            ) else (max(expirations) if expirations else None),
            apple_documents=apple_documents,
            manual_documents=manual_documents,
        )

    @classmethod
    def _active_apple(cls, document: dict, now: datetime) -> bool:
        if not document.get("active") or document.get("revokedAt"):
            return False
        starts_at = cls._parse_datetime(document.get("startsAt"))
        if starts_at is not None and starts_at > now:
            return False
        return cls._not_expired(document.get("expiresAt"), now)

    @classmethod
    def _active_manual(cls, document: dict, now: datetime) -> bool:
        if document.get("revokedAt"):
            return False
        starts_at = cls._parse_datetime(document.get("startsAt"))
        if starts_at is None or starts_at > now:
            return False
        return cls._not_expired(document.get("expiresAt"), now)

    @classmethod
    def _not_expired(cls, value: Any, now: datetime) -> bool:
        if value in (None, ""):
            return True
        parsed = cls._parse_datetime(value)
        return parsed is not None and parsed > now

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                return None
            return value.astimezone(UTC)
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
