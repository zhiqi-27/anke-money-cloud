from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum

from pydantic import Field, field_validator, model_validator

from app.models.sync import APIModel


class AdminGrantType(str, Enum):
    fixed_term = "fixedTerm"
    lifetime = "lifetime"


class AdminUserStatus(str, Enum):
    all = "all"
    pro = "pro"
    free = "free"
    manual_grant = "manualGrant"


class AdminGrantCreateRequest(APIModel):
    grant_type: AdminGrantType = AdminGrantType.fixed_term
    starts_at: datetime
    expires_at: datetime | None = None
    reason: str = Field(min_length=1, max_length=240)

    @field_validator("starts_at", "expires_at")
    @classmethod
    def timestamps_require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)

    @field_validator("reason")
    @classmethod
    def reason_is_trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value

    @model_validator(mode="after")
    def grant_period_is_valid(self) -> "AdminGrantCreateRequest":
        if self.grant_type is AdminGrantType.lifetime:
            if self.expires_at is not None:
                raise ValueError("lifetime grants must not have expiresAt")
            return self
        if self.expires_at is None:
            raise ValueError("fixed-term grants require expiresAt")
        if self.expires_at <= self.starts_at:
            raise ValueError("expiresAt must be after startsAt")
        if self.expires_at - self.starts_at > timedelta(days=366):
            raise ValueError("fixed-term grants cannot exceed 366 days")
        return self


class AdminGrantRevokeRequest(APIModel):
    reason: str = Field(min_length=1, max_length=240)

    @field_validator("reason")
    @classmethod
    def reason_is_trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


class AdminEntitlementSummary(APIModel):
    active: bool
    sources: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class AdminUserSummary(APIModel):
    uid: str
    display_name: str | None = None
    email: str | None = None
    provider: str
    created_at: datetime
    effective_entitlement: AdminEntitlementSummary


class AdminUserListResponse(APIModel):
    items: list[AdminUserSummary]
    next_cursor: str | None = None


class AdminUserDetail(AdminUserSummary):
    household_ready: bool


class AdminAppleSubscriptionView(APIModel):
    product_id: str | None = None
    original_transaction_id: str | None = None
    transaction_id: str | None = None
    active: bool
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    environment: str | None = None


class AdminManualGrantView(APIModel):
    id: str
    uid: str
    grant_type: AdminGrantType
    starts_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    reason: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class AdminUserEntitlementResponse(APIModel):
    uid: str
    effective: AdminEntitlementSummary
    apple_subscriptions: list[AdminAppleSubscriptionView]
    manual_grants: list[AdminManualGrantView]


class AdminManualGrantListItem(AdminManualGrantView):
    display_name: str | None = None
    email: str | None = None
    active: bool


class AdminManualGrantListResponse(APIModel):
    items: list[AdminManualGrantListItem]
    next_cursor: str | None = None


class AdminGrantMutationResponse(APIModel):
    grant: AdminManualGrantView
    effective_entitlement: AdminEntitlementSummary
    replayed: bool


class AdminOverviewResponse(APIModel):
    active_pro_accounts: int
    active_manual_grant_accounts: int
    manual_grants_expiring_within_days: int
    recent_admin_actions: int
    generated_at: datetime


class AdminAuditEventView(APIModel):
    id: str
    action: str
    outcome: str
    target_uid: str
    grant_id: str | None = None
    actor_uid: str
    reason: str | None = None
    request_id: str | None = None
    created_at: datetime


class AdminAuditListResponse(APIModel):
    items: list[AdminAuditEventView]
    next_cursor: str | None = None
