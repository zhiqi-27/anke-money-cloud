from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.models import Actor, LedgerEntryCreate, LedgerEntryDocument


@dataclass(frozen=True, slots=True)
class LedgerCreateResult:
    entry: LedgerEntryDocument
    replayed: bool


class HouseholdStorage(Protocol):
    def create_ledger_entry(
        self,
        request: LedgerEntryCreate,
        actor: Actor,
    ) -> LedgerCreateResult: ...

    def read_household_document(
        self,
        household_id: str,
        item_id: str,
    ) -> dict | None: ...
