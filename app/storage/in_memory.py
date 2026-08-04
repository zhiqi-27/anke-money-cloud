from __future__ import annotations

from datetime import UTC, datetime
from threading import RLock

from app.models import (
    Actor,
    LedgerEntryCreate,
    LedgerEntryDocument,
    build_ledger_transaction_documents,
)
from app.storage.protocols import LedgerCreateResult


class InMemoryHouseholdStorage:
    """Credential-free fake that mirrors household partition and idempotency rules."""

    def __init__(self):
        self._items: dict[tuple[str, str], dict] = {}
        self._lock = RLock()

    def create_ledger_entry(
        self,
        request: LedgerEntryCreate,
        actor: Actor,
    ) -> LedgerCreateResult:
        household_id = str(request.household_id)
        operation_item_id = f"operation:{request.operation_id}"
        with self._lock:
            existing_operation = self._items.get((household_id, operation_item_id))
            if existing_operation is not None:
                entry_data = self._items[
                    (household_id, existing_operation["resultEntityId"])
                ]
                return LedgerCreateResult(
                    entry=LedgerEntryDocument.model_validate(entry_data),
                    replayed=True,
                )

            operation, entry, audit = build_ledger_transaction_documents(
                request,
                actor,
                datetime.now(UTC),
            )
            documents = [
                operation.as_cosmos_document(),
                entry.as_cosmos_document(),
                audit.as_cosmos_document(),
            ]
            for document in documents:
                key = (household_id, document["id"])
                if key in self._items:
                    raise ValueError(f"Document already exists: {document['id']}")
            for document in documents:
                self._items[(household_id, document["id"])] = document
            return LedgerCreateResult(entry=entry, replayed=False)

    def read_household_document(
        self,
        household_id: str,
        item_id: str,
    ) -> dict | None:
        item = self._items.get((household_id, item_id))
        return dict(item) if item is not None else None

    def documents_for_household(self, household_id: str) -> list[dict]:
        return [
            dict(document)
            for (partition, _), document in self._items.items()
            if partition == household_id
        ]
