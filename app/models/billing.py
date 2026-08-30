from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.models.sync import APIModel


class AppleEntitlementVerificationRequest(APIModel):
    signed_transaction: str = Field(min_length=32, max_length=100_000)


class AppleServerNotificationRequest(APIModel):
    signed_payload: str = Field(min_length=32, max_length=250_000)


class ProEntitlementView(APIModel):
    active: bool
    sources: list[str] = Field(default_factory=list)
    product_id: str | None = None
    original_transaction_id: str | None = None
    expires_at: datetime | None = None
    environment: str | None = None
