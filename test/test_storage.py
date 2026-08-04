from datetime import UTC, date, datetime
import unittest
from uuid import uuid4

from azure.cosmos.exceptions import CosmosResourceNotFoundError

from app.config import ConfigurationError, Settings
from app.models import Actor, ActorType, EntryKind, LedgerDirection, LedgerEntryCreate
from app.storage.cosmos import CosmosHouseholdStorage
from app.storage.in_memory import InMemoryHouseholdStorage


def make_request() -> LedgerEntryCreate:
    return LedgerEntryCreate(
        id=uuid4(),
        operation_id=uuid4(),
        household_id=uuid4(),
        kind=EntryKind.transaction,
        direction=LedgerDirection.expense,
        occurred_at=datetime(2026, 8, 4, 12, tzinfo=UTC),
        month_start=date(2026, 8, 1),
        channel_id="channel:cash",
        category_id="category:grocery",
        amount_in_fen=8800,
    )


def settings() -> Settings:
    return Settings(
        environment="test",
        firebase_project_id="anke-money-test",
        firebase_check_revoked=False,
        cosmos_endpoint="https://anke-money-dev.documents.azure.com:443/",
        cosmos_database="anke-money-dev",
        cosmos_entities_container="anke_entities",
        cosmos_identities_container="anke_identities",
        cosmos_key="test-key-not-used",
        cosmos_expected_account_name="anke-money-dev",
        cosmos_allow_smoke_write=False,
    )


class FakeCosmosContainer:
    def __init__(self, partition_paths=None):
        self.partition_paths = partition_paths or ["/householdId"]
        self.items = {}
        self.batch_calls = []

    def read(self):
        return {"partitionKey": {"paths": self.partition_paths}}

    def execute_item_batch(self, *, batch_operations, partition_key):
        self.batch_calls.append((batch_operations, partition_key))
        for operation, args, _ in batch_operations:
            if operation != "create":
                raise AssertionError("Only create is expected")
            document = args[0]
            self.items[(partition_key, document["id"])] = document
        return []

    def read_item(self, *, item, partition_key):
        try:
            return self.items[(partition_key, item)]
        except KeyError as exc:
            raise CosmosResourceNotFoundError() from exc

    def create_item(self, *, body):
        self.items[(body["householdId"], body["id"])] = body
        return body


class InMemoryHouseholdStorageTest(unittest.TestCase):
    def test_create_is_atomic_shape_and_idempotent(self):
        storage = InMemoryHouseholdStorage()
        request = make_request()
        actor = Actor(type=ActorType.user, id="firebase-user-1")

        first = storage.create_ledger_entry(request, actor)
        replay = storage.create_ledger_entry(request, actor)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.entry.id, replay.entry.id)
        documents = storage.documents_for_household(str(request.household_id))
        self.assertEqual(len(documents), 3)
        self.assertEqual(
            {document["entityType"] for document in documents},
            {"operation", "ledgerEntry", "auditEvent"},
        )

    def test_same_business_data_with_new_operation_appends(self):
        storage = InMemoryHouseholdStorage()
        actor = Actor(type=ActorType.user, id="firebase-user-1")
        first = make_request()
        second = first.model_copy(
            update={"id": uuid4(), "operation_id": uuid4()}
        )

        storage.create_ledger_entry(first, actor)
        storage.create_ledger_entry(second, actor)

        documents = storage.documents_for_household(str(first.household_id))
        ledger_entries = [item for item in documents if item["entityType"] == "ledgerEntry"]
        self.assertEqual(len(ledger_entries), 2)

    def test_point_read_requires_household_partition(self):
        storage = InMemoryHouseholdStorage()
        request = make_request()
        actor = Actor(type=ActorType.user, id="firebase-user-1")
        result = storage.create_ledger_entry(request, actor)

        self.assertIsNone(
            storage.read_household_document("different-household", result.entry.id)
        )


class CosmosHouseholdStorageTest(unittest.TestCase):
    def test_ledger_create_uses_three_item_household_batch(self):
        container = FakeCosmosContainer()
        storage = CosmosHouseholdStorage(settings(), container=container)
        request = make_request()
        actor = Actor(type=ActorType.user, id="firebase-user-1")

        result = storage.create_ledger_entry(request, actor)

        self.assertFalse(result.replayed)
        self.assertEqual(len(container.batch_calls), 1)
        batch_operations, partition_key = container.batch_calls[0]
        self.assertEqual(partition_key, str(request.household_id))
        self.assertEqual(len(batch_operations), 3)
        documents = [args[0] for _, args, _ in batch_operations]
        self.assertTrue(all(item["householdId"] == partition_key for item in documents))

    def test_verifies_exact_partition_path(self):
        CosmosHouseholdStorage(
            settings(), container=FakeCosmosContainer()
        ).verify_household_partition_contract()

        storage = CosmosHouseholdStorage(
            settings(), container=FakeCosmosContainer(["/uid"])
        )
        with self.assertRaises(ConfigurationError):
            storage.verify_household_partition_contract()

    def test_smoke_probe_requires_synthetic_dev_partition(self):
        storage = CosmosHouseholdStorage(settings(), container=FakeCosmosContainer())
        with self.assertRaises(ValueError):
            storage.create_and_read_smoke_probe(
                {"id": "x", "householdId": "real-household", "entityType": "smokeProbe", "isSynthetic": True}
            )

        probe = {
            "id": "smoke:1",
            "householdId": "smoke-dev-1",
            "entityType": "smokeProbe",
            "isSynthetic": True,
        }
        self.assertEqual(storage.create_and_read_smoke_probe(probe)["id"], "smoke:1")


if __name__ == "__main__":
    unittest.main()
