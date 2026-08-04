from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import (
    CosmosBatchOperationError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from azure.identity import DefaultAzureCredential

from app.config import ConfigurationError, Settings
from app.models import (
    Actor,
    LedgerEntryCreate,
    LedgerEntryDocument,
    build_ledger_transaction_documents,
)
from app.storage.protocols import LedgerCreateResult


logger = logging.getLogger(__name__)
HOUSEHOLD_PARTITION_PATH = "/householdId"


class CosmosHouseholdStorage:
    """Cosmos adapter for the household-partitioned primary entity container."""

    def __init__(self, settings: Settings, *, container: Any | None = None):
        self._settings = settings
        self._container = container

    @property
    def account_name(self) -> str:
        host = (urlparse(self._settings.cosmos_endpoint).hostname or "").lower()
        return host.split(".", maxsplit=1)[0]

    def create_ledger_entry(
        self,
        request: LedgerEntryCreate,
        actor: Actor,
    ) -> LedgerCreateResult:
        household_id = str(request.household_id)
        operation_id = str(request.operation_id)
        existing = self._read_operation(household_id, operation_id)
        if existing is not None:
            return self._replayed_result(household_id, existing)

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
        self._validate_partition(household_id, documents)
        batch_operations = [
            ("create", (document,), {}) for document in documents
        ]
        try:
            self._entities_container().execute_item_batch(
                batch_operations=batch_operations,
                partition_key=household_id,
            )
        except (CosmosResourceExistsError, CosmosBatchOperationError) as exc:
            if getattr(exc, "status_code", 409) != 409:
                raise
            existing = self._read_operation(household_id, operation_id)
            if existing is None:
                raise
            return self._replayed_result(household_id, existing)
        return LedgerCreateResult(entry=entry, replayed=False)

    def read_household_document(
        self,
        household_id: str,
        item_id: str,
    ) -> dict | None:
        try:
            return self._entities_container().read_item(
                item=item_id,
                partition_key=household_id,
            )
        except CosmosResourceNotFoundError:
            return None

    def verify_household_partition_contract(self) -> None:
        properties = self._entities_container().read()
        partition = properties.get("partitionKey") or {}
        paths = partition.get("paths") or []
        if paths != [HOUSEHOLD_PARTITION_PATH]:
            raise ConfigurationError(
                "Cosmos entities container must use exactly /householdId"
            )

    def create_and_read_smoke_probe(self, document: dict[str, Any]) -> dict[str, Any]:
        household_id = document.get("householdId")
        if not isinstance(household_id, str) or not household_id.startswith("smoke-dev-"):
            raise ValueError("Smoke householdId must use smoke-dev- prefix")
        if document.get("entityType") != "smokeProbe" or document.get("isSynthetic") is not True:
            raise ValueError("Smoke document must be a tagged synthetic probe")
        self._validate_partition(household_id, [document])
        self._entities_container().create_item(body=document)
        return self._entities_container().read_item(
            item=document["id"],
            partition_key=household_id,
        )

    def _read_operation(self, household_id: str, operation_id: str) -> dict | None:
        return self.read_household_document(
            household_id,
            f"operation:{operation_id}",
        )

    def _replayed_result(
        self,
        household_id: str,
        operation: dict,
    ) -> LedgerCreateResult:
        result_id = operation.get("resultEntityId")
        if not isinstance(result_id, str):
            raise RuntimeError("Stored operation has no resultEntityId")
        entry = self.read_household_document(household_id, result_id)
        if entry is None:
            raise RuntimeError("Stored operation result is missing")
        return LedgerCreateResult(
            entry=LedgerEntryDocument.model_validate(entry),
            replayed=True,
        )

    def _entities_container(self):
        if self._container is not None:
            return self._container
        self._settings.require_cosmos()
        credential: str | DefaultAzureCredential
        if self._settings.cosmos_key:
            credential = self._settings.cosmos_key
        else:
            credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        client = CosmosClient(
            self._settings.cosmos_endpoint,
            credential=credential,
            user_agent="anke-money-cloud",
        )
        database = client.get_database_client(self._settings.cosmos_database)
        self._container = database.get_container_client(
            self._settings.cosmos_entities_container
        )
        return self._container

    @staticmethod
    def _validate_partition(household_id: str, documents: list[dict]) -> None:
        if not household_id:
            raise ValueError("householdId is required")
        if any(document.get("householdId") != household_id for document in documents):
            raise ValueError("Every document must match the household partition")
