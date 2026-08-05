from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from uuid import UUID

from pydantic import Field, StrictInt, field_validator, model_validator

from app.models.sync import APIModel
from app.models.documents import EntryKind, LedgerDirection, LedgerEntryDocument


class AgentScope(str, Enum):
    ledger_read = "ledger.read"
    ledger_entry_create = "ledger.entry.create"
    assets_read = "assets.read"
    assets_snapshot_create = "assets.snapshot.create"
    reference_data_read = "reference-data.read"


class AgentConnectionCreate(APIModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[AgentScope] = Field(min_length=1, max_length=5)
    grant_duration_seconds: int | None = Field(default=None, ge=60)

    @model_validator(mode="after")
    def scope_and_lifetime(self) -> "AgentConnectionCreate":
        if len(self.scopes) != len(set(self.scopes)):
            raise ValueError("Agent scopes must be unique")
        has_write = any(
            scope in {AgentScope.ledger_entry_create, AgentScope.assets_snapshot_create}
            for scope in self.scopes
        )
        maximum = 24 * 60 * 60 if has_write else 7 * 24 * 60 * 60
        default = 60 * 60 if has_write else 24 * 60 * 60
        duration = self.grant_duration_seconds or default
        if duration > maximum:
            raise ValueError("Agent grant duration exceeds the allowed maximum")
        self.grant_duration_seconds = duration
        return self


class AgentConnectionView(APIModel):
    connection_id: UUID
    name: str
    scopes: list[AgentScope]
    status: str
    grant_expires_at: datetime
    created_at: datetime


class AgentConnectionCreated(AgentConnectionView):
    access_token: str
    token_expires_at: datetime
    refresh_token: str


class AgentAccessToken(APIModel):
    access_token: str
    token_expires_at: datetime


class AgentPrincipal(APIModel):
    household_id: UUID
    connection_id: UUID
    scopes: list[AgentScope]


class AgentLedgerEntryCreate(APIModel):
    id: UUID
    operation_id: UUID
    kind: EntryKind
    direction: LedgerDirection
    occurred_at: datetime
    month_start: date
    channel_id: str | None = Field(default=None, max_length=128)
    category_id: str = Field(min_length=1, max_length=128)
    amount_in_fen: StrictInt = Field(gt=0, le=9_000_000_000_000_000)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_requires_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurredAt must include a timezone")
        return value.astimezone(UTC)

    @field_validator("month_start")
    @classmethod
    def month_start_is_first_day(cls, value: date) -> date:
        if value.day != 1:
            raise ValueError("monthStart must be the first day of a month")
        return value

    @model_validator(mode="after")
    def channel_matches_direction(self) -> "AgentLedgerEntryCreate":
        if self.direction is LedgerDirection.expense and not self.channel_id:
            raise ValueError("Expense ledger entries require channelId")
        if self.direction is LedgerDirection.income and self.channel_id is not None:
            raise ValueError("Income ledger entries cannot have channelId")
        return self


class AgentLedgerCreateResponse(APIModel):
    entry: LedgerEntryDocument
    replayed: bool


class AgentAssetSnapshotCreate(APIModel):
    id: UUID
    operation_id: UUID
    account_id: UUID
    member_profile_id: str | None = Field(default=None, max_length=256)
    amount_in_fen: StrictInt = Field(ge=-9_000_000_000_000_000, le=9_000_000_000_000_000)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def observed_at_requires_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observedAt must include a timezone")
        return value.astimezone(UTC)


class AgentEntityView(APIModel):
    entity_type: str
    entity_id: str
    revision: int
    created_at: datetime
    updated_at: datetime
    payload: dict


class AgentEntityListResponse(APIModel):
    items: list[AgentEntityView]


class AgentEntityCreateResponse(APIModel):
    item: AgentEntityView
    replayed: bool
