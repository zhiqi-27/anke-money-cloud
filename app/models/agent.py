from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from uuid import UUID

from pydantic import Field, StrictInt, field_validator, model_validator

from app.models.sync import APIModel
from app.models.documents import EntryKind, LedgerDirection, LedgerEntryDocument


class AgentScope(str, Enum):
    ledger_read = "ledger:read"
    ledger_create = "ledger:create"
    assets_read = "assets:read"
    assets_update = "assets:update"
    categories_read = "categories:read"
    channels_read = "channels:read"


class OperationSource(str, Enum):
    api = "api"
    mcp = "mcp"
    skill = "skill"


class AgentAPIKeyView(APIModel):
    connection_id: UUID
    key_prefix: str
    status: str
    created_at: datetime
    last_used_at: datetime | None = None
    scopes: list[AgentScope]


class AgentAPIKeyCreated(AgentAPIKeyView):
    api_key: str


class AgentPrincipal(APIModel):
    household_id: UUID
    connection_id: UUID
    scopes: list[AgentScope]
    integration: OperationSource


class AgentLedgerEntryCreate(APIModel):
    id: UUID
    idempotency_key: UUID
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
        if self.channel_id is not None and not self.channel_id:
            raise ValueError("channelId must be non-empty when supplied")
        if self.direction is LedgerDirection.income and self.channel_id is not None:
            raise ValueError("Income ledger entries cannot have channelId")
        return self


class AgentLedgerCreateResponse(APIModel):
    entry: LedgerEntryDocument
    replayed: bool


class AgentLedgerBatchCreate(APIModel):
    entries: list[AgentLedgerEntryCreate] = Field(min_length=1, max_length=25)

    @model_validator(mode="after")
    def entries_have_unique_ids(self) -> "AgentLedgerBatchCreate":
        entity_ids = [entry.id for entry in self.entries]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("Batch ledger entry IDs must be unique")
        idempotency_keys = [entry.idempotency_key for entry in self.entries]
        if len(idempotency_keys) != len(set(idempotency_keys)):
            raise ValueError("Batch ledger idempotency keys must be unique")
        return self


class AgentLedgerBatchCreateResponse(APIModel):
    results: list[AgentLedgerCreateResponse]
    created_count: int = Field(ge=0)
    replayed_count: int = Field(ge=0)


class AgentAssetUpdate(APIModel):
    snapshot_id: UUID
    idempotency_key: UUID
    member_profile_id: str | None = Field(default=None, max_length=256)
    amount_in_fen: StrictInt = Field(ge=-9_000_000_000_000_000, le=9_000_000_000_000_000)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def observed_at_requires_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observedAt must include a timezone")
        return value.astimezone(UTC)


class AssetAccountKind(str, Enum):
    asset = "asset"
    liability = "liability"


class AssetGroup(str, Enum):
    financial = "financial"
    living = "living"
    interest = "interest"
    receivable = "receivable"


class AssetMoneyBucket(str, Enum):
    flexible = "flexible"
    stable = "stable"
    risk = "risk"


class AgentAssetCreate(APIModel):
    account_id: UUID
    snapshot_id: UUID
    idempotency_key: UUID
    name: str = Field(min_length=1, max_length=80)
    kind: AssetAccountKind
    asset_group: AssetGroup | None = None
    category_id: str = Field(min_length=1, max_length=128)
    money_bucket: AssetMoneyBucket | None = None
    amount_in_fen: StrictInt = Field(ge=0, le=9_000_000_000_000_000)
    observed_at: datetime
    member_profile_id: str | None = Field(default=None, max_length=256)

    @field_validator("name")
    @classmethod
    def name_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("observed_at")
    @classmethod
    def observed_at_requires_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observedAt must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def classification_is_consistent(self) -> "AgentAssetCreate":
        if self.account_id == self.snapshot_id:
            raise ValueError("accountId and snapshotId must be different")
        if self.kind is AssetAccountKind.liability:
            if self.asset_group is not None or self.money_bucket is not None:
                raise ValueError("Liability accounts cannot have assetGroup or moneyBucket")
            return self
        if self.asset_group is None:
            raise ValueError("Asset accounts require assetGroup")
        if self.asset_group is AssetGroup.financial and self.money_bucket is None:
            raise ValueError("Financial asset accounts require moneyBucket")
        if self.asset_group is not AssetGroup.financial and self.money_bucket is not None:
            raise ValueError("Only financial asset accounts can have moneyBucket")
        return self


class AgentAssetCreateResponse(APIModel):
    account: AgentEntityView
    initial_snapshot: AgentEntityView
    replayed: bool


class AgentAssetBatchCreate(APIModel):
    accounts: list[AgentAssetCreate] = Field(min_length=1, max_length=25)

    @model_validator(mode="after")
    def accounts_have_unique_ids(self) -> "AgentAssetBatchCreate":
        account_ids = [item.account_id for item in self.accounts]
        snapshot_ids = [item.snapshot_id for item in self.accounts]
        idempotency_keys = [item.idempotency_key for item in self.accounts]
        all_entity_ids = account_ids + snapshot_ids
        if len(all_entity_ids) != len(set(all_entity_ids)):
            raise ValueError("Batch asset account and snapshot IDs must be unique")
        if len(idempotency_keys) != len(set(idempotency_keys)):
            raise ValueError("Batch asset idempotency keys must be unique")
        return self


class AgentAssetBatchCreateResponse(APIModel):
    results: list[AgentAssetCreateResponse]
    created_count: int = Field(ge=0)
    replayed_count: int = Field(ge=0)


class AgentEntityView(APIModel):
    entity_type: str
    entity_id: str
    revision: int
    created_at: datetime
    updated_at: datetime
    payload: dict


class AgentEntityListResponse(APIModel):
    items: list[AgentEntityView]
    next_cursor: str | None = None
    has_more: bool = False


class AgentEntityCreateResponse(APIModel):
    item: AgentEntityView
    replayed: bool
