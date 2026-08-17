from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part.capitalize() for part in rest)


class APIModel(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True)


class SyncEntityType(str, Enum):
    ledger_entry = "ledgerEntry"
    asset_account = "assetAccount"
    asset_snapshot = "assetSnapshot"
    payment_channel = "paymentChannel"
    category = "category"
    member_profile = "memberProfile"


class MutationAction(str, Enum):
    create = "create"
    update = "update"
    delete = "delete"


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require_integer(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _require_boolean(payload: dict[str, Any], key: str) -> None:
    if not isinstance(payload.get(key), bool):
        raise ValueError(f"{key} must be a boolean")


def _validate_optional_string(payload: dict[str, Any], key: str) -> None:
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")


def _require_timezone_timestamp(payload: dict[str, Any], key: str) -> None:
    value = _require_string(payload, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{key} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{key} must include a timezone")


def _validate_entity_payload(
    entity_type: SyncEntityType,
    payload: dict[str, Any],
) -> None:
    if entity_type is SyncEntityType.ledger_entry:
        if payload.get("kind") not in {"transaction", "monthlySummary"}:
            raise ValueError("Ledger kind is invalid")
        direction = payload.get("direction")
        if direction not in {"expense", "income"}:
            raise ValueError("Ledger direction is invalid")
        _require_timezone_timestamp(payload, "occurredAt")
        month_start = _require_string(payload, "monthStart")
        try:
            parsed_month = date.fromisoformat(month_start)
        except ValueError as error:
            raise ValueError("monthStart must be an ISO-8601 date") from error
        if parsed_month.day != 1:
            raise ValueError("monthStart must be the first day of a month")
        _require_string(payload, "categoryId")
        amount = _require_integer(payload, "amountInFen")
        if amount <= 0:
            raise ValueError("Ledger amountInFen must be positive")
        channel = payload.get("channelId")
        if direction == "expense" and (not isinstance(channel, str) or not channel):
            raise ValueError("Expense ledger entries require channelId")
        if direction == "income" and channel is not None:
            raise ValueError("Income ledger entries cannot have channelId")
        _validate_optional_string(payload, "note")
        _validate_optional_string(payload, "memberProfileId")
        allocation_values = (
            payload.get("allocationSourceId"),
            payload.get("allocationIndex"),
            payload.get("allocationCount"),
            payload.get("allocationStartMonth"),
        )
        if any(value is not None for value in allocation_values):
            if any(value is None for value in allocation_values):
                raise ValueError("Allocation fields must be supplied together")
            if direction != "expense" or payload.get("kind") != "transaction":
                raise ValueError("Only transaction expenses can be allocations")
            try:
                UUID(str(payload["allocationSourceId"]))
            except ValueError as error:
                raise ValueError("allocationSourceId must be a UUID") from error
            allocation_index = _require_integer(payload, "allocationIndex")
            allocation_count = _require_integer(payload, "allocationCount")
            if allocation_count < 2 or allocation_count > 120:
                raise ValueError("allocationCount must be between 2 and 120")
            if allocation_index < 1 or allocation_index > allocation_count:
                raise ValueError("allocationIndex must be within allocationCount")
            try:
                allocation_start = date.fromisoformat(
                    _require_string(payload, "allocationStartMonth")
                )
            except ValueError as error:
                raise ValueError("allocationStartMonth must be an ISO-8601 date") from error
            if allocation_start.day != 1:
                raise ValueError("allocationStartMonth must be the first day of a month")
    elif entity_type is SyncEntityType.asset_account:
        _require_string(payload, "name")
        _require_integer(payload, "amountInFen")
    elif entity_type is SyncEntityType.asset_snapshot:
        try:
            UUID(_require_string(payload, "accountId"))
        except ValueError as error:
            raise ValueError("accountId must be a UUID") from error
        _require_integer(payload, "amountInFen")
        _require_timezone_timestamp(payload, "observedAt")
        _validate_optional_string(payload, "memberProfileId")
    elif entity_type is SyncEntityType.payment_channel:
        _require_string(payload, "name")
        _require_string(payload, "symbolName")
        _require_integer(payload, "sortOrder")
        _require_boolean(payload, "isArchived")
        _require_boolean(payload, "isSystem")
        _validate_optional_string(payload, "assetName")
    elif entity_type is SyncEntityType.category:
        _require_string(payload, "name")
        _require_string(payload, "symbolName")
        _require_integer(payload, "sortOrder")
        _require_boolean(payload, "isArchived")
        _require_boolean(payload, "isSystem")
        scope = payload.get("scope", "ledger")
        if scope == "asset":
            if payload.get("assetGroup") not in {"financial", "living", "receivable", "liability"}:
                raise ValueError("Asset category group is invalid")
        elif scope == "ledger":
            if payload.get("direction") not in {"expense", "income"}:
                raise ValueError("Category direction is invalid")
        else:
            raise ValueError("Category scope is invalid")
    elif entity_type is SyncEntityType.member_profile:
        _require_string(payload, "name")


class DeviceRegistration(APIModel):
    device_id: UUID
    name: str = Field(min_length=1, max_length=120)
    platform: str = Field(default="ios", pattern="^ios$")
    app_version: str = Field(min_length=1, max_length=40)


class PushTokenRegistration(APIModel):
    device_id: UUID
    token: str = Field(pattern="^[0-9a-f]{64}$")
    environment: str = Field(pattern="^(sandbox|production)$")
    topic: str = Field(min_length=1, max_length=255)
    app_version: str = Field(min_length=1, max_length=40)


class BootstrapResponse(APIModel):
    user_id: str
    household_id: UUID
    device_id: UUID
    connection_id: UUID
    sync_cursor: str | None = None
    next_outbox_sequence: int = Field(ge=1)
    workspace_status: str = Field(pattern="^(empty|active)$")


class SyncMutation(APIModel):
    mutation_id: UUID
    device_id: UUID
    sequence: int = Field(ge=1)
    entity_type: SyncEntityType
    entity_id: str = Field(min_length=1, max_length=256)
    action: MutationAction
    base_revision: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] | None = None
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def timestamp_requires_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurredAt must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def action_shape(self) -> "SyncMutation":
        if self.action is MutationAction.create and self.base_revision is not None:
            raise ValueError("Create mutations cannot include baseRevision")
        if self.action in {MutationAction.update, MutationAction.delete} and self.base_revision is None:
            raise ValueError("Update and delete mutations require baseRevision")
        if self.action is MutationAction.delete and self.payload not in (None, {}):
            raise ValueError("Delete mutations cannot include payload")
        if self.action is not MutationAction.delete and not self.payload:
            raise ValueError("Create and update mutations require payload")
        if self.entity_type is SyncEntityType.ledger_entry and self.action in {
            MutationAction.update,
            MutationAction.delete,
        }:
            raise ValueError("Ledger entries cannot be updated or deleted")
        if self.payload:
            _validate_entity_payload(self.entity_type, self.payload)
        return self


class SyncPushRequest(APIModel):
    device_id: UUID
    mutations: list[SyncMutation] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def device_and_order_match(self) -> "SyncPushRequest":
        if any(item.device_id != self.device_id for item in self.mutations):
            raise ValueError("Every mutation must match deviceId")
        sequences = [item.sequence for item in self.mutations]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("Mutation sequence must be unique and ascending")
        return self


class MutationStatus(str, Enum):
    accepted = "accepted"
    replayed = "replayed"
    conflict = "conflict"
    rejected = "rejected"


class MutationResult(APIModel):
    mutation_id: UUID
    entity_id: str
    status: MutationStatus
    revision: int | None = None
    reason: str | None = None
    server_entity: dict[str, Any] | None = None


class SyncPushResponse(APIModel):
    results: list[MutationResult]


class SyncChange(APIModel):
    entity_type: SyncEntityType
    entity_id: str
    revision: int
    updated_at: datetime
    deleted_at: datetime | None = None
    payload: dict[str, Any] | None = None


class SyncPullResponse(APIModel):
    changes: list[SyncChange]
    next_cursor: str | None = None
    has_more: bool = False


class AuditEventView(APIModel):
    operation_id: str
    idempotency_key: str
    actor_type: str
    actor_id: str
    scope: str
    action: str
    source: str
    target_id: str
    outcome: str
    reason: str | None = None
    prior_revision: int | None = None
    new_revision: int | None = None
    change_summary: dict[str, Any]
    created_at: datetime


class AuditListResponse(APIModel):
    events: list[AuditEventView]
    next_cursor: str | None = None
    has_more: bool = False


class MigrationSourceMode(str, Enum):
    local = "local"


class MigrationManifest(APIModel):
    session_id: UUID
    source_mode: MigrationSourceMode
    schema_version: int = Field(ge=1)
    record_counts: dict[str, int]
    content_digest: str = Field(pattern="^[a-f0-9]{64}$")

    @field_validator("record_counts")
    @classmethod
    def counts_are_nonnegative(cls, value: dict[str, int]) -> dict[str, int]:
        if any(count < 0 for count in value.values()):
            raise ValueError("recordCounts cannot contain negative values")
        return value


class MigrationItem(APIModel):
    entity_type: SyncEntityType
    entity_id: str = Field(min_length=1, max_length=256)
    payload: dict[str, Any]
    created_at: datetime
    deleted_at: datetime | None = None

    @field_validator("created_at")
    @classmethod
    def created_at_requires_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("createdAt must include a timezone")
        return value.astimezone(UTC)

    @field_validator("deleted_at")
    @classmethod
    def deleted_at_requires_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deletedAt must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def payload_matches_entity(self) -> "MigrationItem":
        if self.deleted_at is None:
            if not self.payload:
                raise ValueError("Live migration items require payload")
            _validate_entity_payload(self.entity_type, self.payload)
        return self


class MigrationUploadRequest(APIModel):
    device_id: UUID
    manifest: MigrationManifest
    items: list[MigrationItem] = Field(max_length=5_000)

    @model_validator(mode="after")
    def cosmos_ids_are_unique_within_household(self) -> "MigrationUploadRequest":
        entity_ids = [item.entity_id for item in self.items]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("Migration entityIds must be unique within the household")
        return self


class MigrationStatus(str, Enum):
    staged = "staged"
    active = "active"


class MigrationResponse(APIModel):
    session_id: UUID
    status: MigrationStatus
    household_id: UUID
    verified_counts: dict[str, int]
    content_digest: str
    replayed: bool = False


class MigrationActivateRequest(APIModel):
    session_id: UUID
    content_digest: str = Field(pattern="^[a-f0-9]{64}$")
