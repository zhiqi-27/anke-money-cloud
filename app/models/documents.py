from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


def _camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part.capitalize() for part in rest)


class DocumentModel(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True)

    def as_cosmos_document(self) -> dict:
        return self.model_dump(by_alias=True, mode="json")


class ActorType(str, Enum):
    user = "user"
    agent = "agent"
    system = "system"


class EntryKind(str, Enum):
    transaction = "transaction"
    monthly_summary = "monthlySummary"


class LedgerDirection(str, Enum):
    expense = "expense"
    income = "income"


class Actor(DocumentModel):
    type: ActorType
    id: str = Field(min_length=1, max_length=256)


class LedgerEntryCreate(DocumentModel):
    id: UUID
    operation_id: UUID
    household_id: UUID
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
    def channel_matches_direction(self) -> "LedgerEntryCreate":
        if self.direction is LedgerDirection.expense and not self.channel_id:
            raise ValueError("Expense ledger entries require channelId")
        if self.direction is LedgerDirection.income and self.channel_id is not None:
            raise ValueError("Income ledger entries cannot have channelId")
        return self


class HouseholdDocument(DocumentModel):
    id: str
    entity_type: str
    household_id: str
    schema_version: int = 1
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    actor: Actor
    operation_id: str
    last_accepted_mutation_id: str


class LedgerEntryDocument(HouseholdDocument):
    entity_type: str = "ledgerEntry"
    kind: EntryKind
    direction: LedgerDirection
    occurred_at: datetime
    month_start: date
    channel_id: str | None = None
    category_id: str
    amount_in_fen: int
    note: str | None = None


class OperationDocument(HouseholdDocument):
    entity_type: str = "operation"
    action: str = "ledger.create"
    status: str = "accepted"
    result_entity_id: str
    accepted_revision: int = 1


class AuditEventDocument(HouseholdDocument):
    entity_type: str = "auditEvent"
    scope: str = "ledger.entry.create"
    action: str = "ledger.create"
    target_type: str = "ledgerEntry"
    target_id: str
    outcome: str = "accepted"
    change_summary: dict[str, object]


def build_ledger_transaction_documents(
    request: LedgerEntryCreate,
    actor: Actor,
    now: datetime,
) -> tuple[OperationDocument, LedgerEntryDocument, AuditEventDocument]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    now = now.astimezone(UTC)
    household_id = str(request.household_id)
    operation_id = str(request.operation_id)
    entry_id = str(request.id)
    common = {
        "household_id": household_id,
        "revision": 1,
        "created_at": now,
        "updated_at": now,
        "actor": actor,
        "operation_id": operation_id,
        "last_accepted_mutation_id": operation_id,
    }
    operation = OperationDocument(
        id=f"operation:{operation_id}",
        result_entity_id=entry_id,
        **common,
    )
    entry = LedgerEntryDocument(
        id=entry_id,
        kind=request.kind,
        direction=request.direction,
        occurred_at=request.occurred_at,
        month_start=request.month_start,
        channel_id=request.channel_id,
        category_id=request.category_id,
        amount_in_fen=request.amount_in_fen,
        note=request.note,
        **common,
    )
    audit = AuditEventDocument(
        id=f"audit:{operation_id}",
        target_id=entry_id,
        change_summary={
            "created": True,
            "kind": request.kind.value,
            "direction": request.direction.value,
        },
        **common,
    )
    return operation, entry, audit
