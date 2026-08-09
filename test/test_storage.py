from datetime import UTC, date, datetime, timedelta
import unittest
from uuid import uuid4

from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosResourceNotFoundError

from app.config import ConfigurationError, Settings
from app.models import (
    Actor,
    ActorType,
    AgentConnectionCreate,
    AgentScope,
    DeviceRegistration,
    EntryKind,
    LedgerDirection,
    LedgerEntryCreate,
    MutationAction,
    MutationStatus,
    SyncEntityType,
    SyncMutation,
)
from app.auth import AuthenticatedIdentity
from app.services import (
    AgentAccessService,
    AgentRateLimitExceededError,
    CloudService,
    InvalidAgentTokenError,
)
from app.storage.cosmos import CosmosHouseholdStorage
from app.storage.in_memory import InMemoryHouseholdStorage


def make_request() -> LedgerEntryCreate:
    return LedgerEntryCreate(
        id=uuid4(),
        idempotency_key=uuid4(),
        source="api",
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
            if operation in {"create", "upsert"}:
                document = args[0]
            elif operation == "replace":
                document = args[1]
            else:
                raise AssertionError(f"Unexpected operation: {operation}")
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

    def upsert_item(self, *, body):
        self.items[(body["householdId"], body["id"])] = body
        return body

    def replace_item(self, *, item, body):
        self.items[(body["householdId"], item)] = body
        return body

    def delete_item(self, *, item, partition_key):
        del self.items[(partition_key, item)]

    def query_items(self, *, query, parameters, partition_key=None, **_):
        values = {item["name"]: item["value"] for item in parameters}
        documents = [
            item for (partition, _), item in self.items.items()
            if partition_key is None or partition == partition_key
        ]
        types = values.get("@types")
        if types is not None:
            documents = [item for item in documents if item.get("entityType") in types]
        if "@afterSequence" in values:
            documents = [
                item for item in documents
                if int(item.get("changeSequence", 0)) > values["@afterSequence"]
            ]
        if values.get("@includeStaged") is False:
            documents = [item for item in documents if item.get("staged") is not True]
        if "c.deletedAt < @cutoff" in query:
            documents = [
                item for item in documents
                if item.get("deletedAt") is not None
                and item["deletedAt"] < values["@cutoff"]
                and item.get("payloadPurgedAt") is None
            ]
        if "c.entityType = 'auditEvent'" in query and "c.createdAt < @cutoff" in query:
            documents = [
                item for item in documents
                if item.get("entityType") == "auditEvent"
                and item["createdAt"] < values["@cutoff"]
            ]
        if "SELECT VALUE COUNT(1)" in query:
            return [len(documents)]
        if "ORDER BY c.changeSequence" in query:
            documents.sort(key=lambda item: item["changeSequence"])
        if "@limit" in values:
            documents = documents[:values["@limit"]]
        if "@take" in values:
            documents = documents[:values["@take"]]
        return documents


class PreconditionRetryCosmosContainer(FakeCosmosContainer):
    def __init__(self):
        super().__init__()
        self.fail_next_batch = False
        self.precondition_attempts = 0

    def execute_item_batch(self, *, batch_operations, partition_key):
        if self.fail_next_batch:
            self.fail_next_batch = False
            self.precondition_attempts += 1
            raise CosmosBatchOperationError(
                headers={},
                status_code=412,
                message="synthetic stale ETag",
                operation_responses=[],
            )
        return super().execute_item_batch(
            batch_operations=batch_operations,
            partition_key=partition_key,
        )


class FakeIdentityContainer:
    def __init__(self):
        self.items = {}

    def read_item(self, *, item, partition_key):
        try:
            return self.items[(partition_key, item)]
        except KeyError as exc:
            raise CosmosResourceNotFoundError() from exc

    def create_item(self, *, body):
        self.items[(body["uid"], body["id"])] = body
        return body

    def delete_item(self, *, item, partition_key):
        try:
            del self.items[(partition_key, item)]
        except KeyError as exc:
            raise CosmosResourceNotFoundError() from exc


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
            update={"id": uuid4(), "idempotency_key": uuid4()}
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
        container.create_item(body={
            "id": str(request.household_id),
            "householdId": str(request.household_id),
            "entityType": "household",
            "status": "active",
            "lastChangeSequence": 0,
            "createdAt": datetime.now(UTC).isoformat(),
            "updatedAt": datetime.now(UTC).isoformat(),
        })

        result = storage.create_ledger_entry(request, actor)

        self.assertFalse(result.replayed)
        self.assertEqual(len(container.batch_calls), 1)
        batch_operations, partition_key = container.batch_calls[0]
        self.assertEqual(partition_key, str(request.household_id))
        self.assertEqual(len(batch_operations), 4)
        documents = [
            args[0] if operation == "create" else args[1]
            for operation, args, _ in batch_operations
        ]
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

    def test_bootstrap_persists_server_owned_membership_device_and_connection(self):
        entities = FakeCosmosContainer()
        identities = FakeIdentityContainer()
        storage = CosmosHouseholdStorage(
            settings(),
            container=entities,
            identities_container=identities,
        )
        device_id = uuid4()

        result = storage.bootstrap_owner(
            "firebase-user-1",
            DeviceRegistration(
                device_id=device_id,
                name="Synthetic iPhone",
                app_version="0.1.0",
            ),
        )

        self.assertEqual(result.device_id, device_id)
        self.assertEqual(storage.household_for_uid("firebase-user-1"), str(result.household_id))
        household_documents = [
            item for (partition, _), item in entities.items.items()
            if partition == str(result.household_id)
        ]
        self.assertEqual(
            {item["entityType"] for item in household_documents},
            {"user", "household", "device", "connection"},
        )

    def test_account_deletion_erases_cosmos_partition_and_identity_idempotently(self):
        entities = FakeCosmosContainer()
        identities = FakeIdentityContainer()
        storage = CosmosHouseholdStorage(
            settings(),
            container=entities,
            identities_container=identities,
        )
        bootstrap = storage.bootstrap_owner(
            "firebase-user-1",
            DeviceRegistration(
                device_id=uuid4(),
                name="Synthetic iPhone",
                app_version="0.1.0",
            ),
        )
        household_id = str(bootstrap.household_id)

        deleted = storage.delete_account_data("firebase-user-1")
        replayed = storage.delete_account_data("firebase-user-1")

        self.assertEqual(deleted, 4)
        self.assertEqual(replayed, 0)
        self.assertFalse(any(
            partition == household_id for partition, _ in entities.items
        ))
        self.assertEqual(identities.items, {})

    def test_sync_acceptance_batches_entity_operation_audit_and_device_sequence(self):
        entities = FakeCosmosContainer()
        storage = CosmosHouseholdStorage(
            settings(),
            container=entities,
            identities_container=FakeIdentityContainer(),
        )
        device_id = uuid4()
        bootstrap = storage.bootstrap_owner(
            "firebase-user-1",
            DeviceRegistration(device_id=device_id, name="Synthetic iPhone", app_version="0.1.0"),
        )
        mutation = SyncMutation(
            mutation_id=uuid4(),
            device_id=device_id,
            sequence=1,
            entity_type=SyncEntityType.member_profile,
            entity_id="member-1",
            action=MutationAction.create,
            payload={"name": "Owner"},
            occurred_at=datetime.now(UTC),
        )

        result = storage.push_mutation(
            str(bootstrap.household_id),
            Actor(type=ActorType.user, id="firebase-user-1"),
            mutation,
        )
        restored = storage.bootstrap_owner(
            "firebase-user-1",
            DeviceRegistration(
                device_id=device_id,
                name="Synthetic iPhone",
                app_version="0.1.0",
            ),
        )

        self.assertEqual(result.status, MutationStatus.accepted)
        self.assertEqual(restored.next_outbox_sequence, 2)
        self.assertEqual(restored.sync_cursor, "0")
        batch, partition = entities.batch_calls[-1]
        self.assertEqual(partition, str(bootstrap.household_id))
        self.assertEqual(len(batch), 5)
        stored_types = {
            item["entityType"]
            for (stored_partition, _), item in entities.items.items()
            if stored_partition == partition
        }
        self.assertTrue({"memberProfile", "operation", "auditEvent", "device"}.issubset(stored_types))

    def test_cosmos_pull_cursor_is_a_persistent_monotonic_checkpoint(self):
        entities = FakeCosmosContainer()
        storage = CosmosHouseholdStorage(
            settings(), container=entities, identities_container=FakeIdentityContainer()
        )
        device_id = uuid4()
        bootstrap = storage.bootstrap_owner(
            "firebase-user-1",
            DeviceRegistration(device_id=device_id, name="Synthetic iPhone", app_version="0.1.0"),
        )
        actor = Actor(type=ActorType.user, id="firebase-user-1")
        for sequence in (1, 2):
            storage.push_mutation(
                str(bootstrap.household_id),
                actor,
                SyncMutation(
                    mutation_id=uuid4(),
                    device_id=device_id,
                    sequence=sequence,
                    entity_type=SyncEntityType.member_profile,
                    entity_id=f"member-{sequence}",
                    action=MutationAction.create,
                    payload={"name": f"Member {sequence}"},
                    occurred_at=datetime.now(UTC),
                ),
            )

        first, first_cursor, first_has_more = storage.pull_changes(
            str(bootstrap.household_id), None, 1
        )
        second, second_cursor, second_has_more = storage.pull_changes(
            str(bootstrap.household_id), first_cursor, 1
        )
        empty, stable_cursor, empty_has_more = storage.pull_changes(
            str(bootstrap.household_id), second_cursor, 10
        )

        self.assertEqual([item.entity_id for item in first], ["member-1"])
        self.assertTrue(first_has_more)
        self.assertEqual([item.entity_id for item in second], ["member-2"])
        self.assertFalse(second_has_more)
        self.assertEqual(empty, [])
        self.assertEqual(stable_cursor, second_cursor)
        self.assertFalse(empty_has_more)

    def test_agent_connection_hashes_refresh_token_and_rotates_access_in_batch(self):
        entities = FakeCosmosContainer()
        identities = FakeIdentityContainer()
        storage = CosmosHouseholdStorage(
            settings(), container=entities, identities_container=identities
        )
        cloud = CloudService(storage)
        access = AgentAccessService(storage)
        identity = AuthenticatedIdentity(uid="firebase-user-1")
        bootstrap = cloud.bootstrap(
            identity,
            DeviceRegistration(device_id=uuid4(), name="Synthetic iPhone", app_version="0.1.0"),
        )
        household = entities.items[(str(bootstrap.household_id), str(bootstrap.household_id))]
        household["status"] = "active"

        connection = cloud.create_agent_connection(
            identity,
            AgentConnectionCreate(name="Read agent", scopes=[AgentScope.ledger_read]),
            access,
        )
        stored = storage.read_household_document(
            str(bootstrap.household_id), str(connection.connection_id)
        )
        refreshed = access.refresh(connection.refresh_token)

        self.assertNotEqual(stored["tokenHash"], connection.access_token)
        self.assertNotEqual(stored["refreshTokenHash"], connection.refresh_token)
        self.assertNotEqual(refreshed.access_token, connection.access_token)
        operations, _ = entities.batch_calls[-1]
        self.assertEqual([item[0] for item in operations], ["replace", "create"])

    def test_cosmos_agent_lifecycle_rate_limit_and_anomaly_use_household_batches(self):
        entities = FakeCosmosContainer()
        identities = FakeIdentityContainer()
        storage = CosmosHouseholdStorage(
            settings(), container=entities, identities_container=identities
        )
        cloud = CloudService(storage)
        access = AgentAccessService(
            storage, requests_per_minute=1, failed_auth_threshold=3
        )
        identity = AuthenticatedIdentity(uid="firebase-security-owner")
        bootstrap = cloud.bootstrap(
            identity,
            DeviceRegistration(
                device_id=uuid4(), name="Synthetic iPhone", app_version="0.1.0"
            ),
        )
        household_id = str(bootstrap.household_id)
        entities.items[(household_id, household_id)]["status"] = "active"
        created = cloud.create_agent_connection(
            identity,
            AgentConnectionCreate(name="Cosmos client", scopes=[AgentScope.ledger_read]),
            access,
        )

        paused = cloud.pause_agent_connection(identity, created.connection_id)
        resumed = cloud.resume_agent_connection(identity, created.connection_id)
        self.assertEqual(paused.status, "paused")
        self.assertEqual(resumed.status, "active")
        self.assertEqual(paused.scopes, resumed.scopes)
        self.assertEqual(paused.grant_expires_at, resumed.grant_expires_at)

        access.authenticate(created.access_token)
        with self.assertRaises(AgentRateLimitExceededError):
            access.authenticate(created.access_token)
        forged = created.access_token.rsplit(".", 1)[0] + ".forged"
        for _ in range(3):
            with self.assertRaises(InvalidAgentTokenError):
                access.authenticate(forged)

        connection = storage.read_household_document(
            household_id, str(created.connection_id)
        )
        self.assertIsNotNone(connection["lastUsedAt"])
        audits = [
            item for (partition, _), item in entities.items.items()
            if partition == household_id and item.get("entityType") == "auditEvent"
        ]
        actions = {item["action"] for item in audits}
        self.assertTrue({
            "agent.pause",
            "agent.resume",
            "agent.rate_limit",
            "agent.authentication.anomaly",
        }.issubset(actions))
        self.assertTrue(all(partition == household_id for _, partition in entities.batch_calls))

    def test_cosmos_agent_counter_retries_a_stale_etag(self):
        entities = PreconditionRetryCosmosContainer()
        storage = CosmosHouseholdStorage(
            settings(), container=entities, identities_container=FakeIdentityContainer()
        )
        cloud = CloudService(storage)
        access = AgentAccessService(storage)
        identity = AuthenticatedIdentity(uid="firebase-concurrent-owner")
        bootstrap = cloud.bootstrap(
            identity,
            DeviceRegistration(
                device_id=uuid4(), name="Synthetic iPhone", app_version="0.1.0"
            ),
        )
        household_id = str(bootstrap.household_id)
        entities.items[(household_id, household_id)]["status"] = "active"
        created = cloud.create_agent_connection(
            identity,
            AgentConnectionCreate(name="Concurrent client", scopes=[AgentScope.ledger_read]),
            access,
        )

        entities.fail_next_batch = True
        principal = access.authenticate(created.access_token)

        self.assertEqual(principal.connection_id, created.connection_id)
        self.assertEqual(entities.precondition_attempts, 1)
        connection = storage.read_household_document(
            household_id, str(created.connection_id)
        )
        self.assertEqual(connection["requestWindowCount"], 1)
        self.assertIsNotNone(connection["lastUsedAt"])

    def test_agent_entity_create_batches_entity_operation_and_redacted_audit(self):
        entities = FakeCosmosContainer()
        storage = CosmosHouseholdStorage(settings(), container=entities)
        household_id = str(uuid4())
        entities.create_item(body={
            "id": household_id,
            "householdId": household_id,
            "entityType": "household",
            "status": "active",
            "lastChangeSequence": 0,
            "createdAt": datetime.now(UTC).isoformat(),
            "updatedAt": datetime.now(UTC).isoformat(),
        })
        actor = Actor(type=ActorType.agent, id=str(uuid4()))
        operation_id = str(uuid4())
        entity_id = str(uuid4())

        first, replayed = storage.create_agent_entity(
            household_id,
            actor,
            "assetSnapshot",
            entity_id,
            operation_id,
            "assets:update",
            "assets.update",
            "api",
            {"amountInFen": 8_800, "note": "must not enter audit"},
            {
                "before": {"amountInFen": 8_000, "revision": 1},
                "after": {"amountInFen": 8_800, "revision": 2},
            },
            datetime.now(UTC),
        )
        second, replayed_again = storage.create_agent_entity(
            household_id,
            actor,
            "assetSnapshot",
            entity_id,
            operation_id,
            "assets:update",
            "assets.update",
            "api",
            {"amountInFen": 8_800, "note": "must not enter audit"},
            {
                "before": {"amountInFen": 8_000, "revision": 1},
                "after": {"amountInFen": 8_800, "revision": 2},
            },
            datetime.now(UTC),
        )

        self.assertFalse(replayed)
        self.assertTrue(replayed_again)
        self.assertEqual(first["id"], second["id"])
        batch, partition = entities.batch_calls[-1]
        self.assertEqual(partition, household_id)
        self.assertEqual(len(batch), 4)
        audit = entities.items[(household_id, f"audit:{operation_id}")]
        self.assertEqual(audit["source"], "api")
        self.assertEqual(audit["idempotencyKey"], operation_id)
        self.assertEqual(audit["changeSummary"]["before"]["amountInFen"], 8_000)
        self.assertEqual(audit["changeSummary"]["after"]["amountInFen"], 8_800)
        self.assertNotIn("must not enter audit", str(audit))

    def test_cosmos_retention_purges_old_tombstone_payload_and_deletes_old_audit(self):
        entities = FakeCosmosContainer()
        storage = CosmosHouseholdStorage(
            settings(), container=entities, identities_container=FakeIdentityContainer()
        )
        now = datetime(2026, 8, 5, tzinfo=UTC)
        household_id = str(uuid4())
        tombstone_id = str(uuid4())
        entities.create_item(body={
            "id": tombstone_id,
            "householdId": household_id,
            "entityType": "ledgerEntry",
            "deletedAt": (now - timedelta(days=31)).isoformat(),
            "payload": {"amountInFen": 8800, "note": "must be purged"},
        })
        entities.create_item(body={
            "id": "audit:old",
            "householdId": household_id,
            "entityType": "auditEvent",
            "createdAt": (now - timedelta(days=366)).isoformat(),
        })

        first = storage.run_retention(now)
        replay = storage.run_retention(now)

        tombstone = entities.items[(household_id, tombstone_id)]
        self.assertIsNone(tombstone["payload"])
        self.assertIsNotNone(tombstone["payloadPurgedAt"])
        self.assertNotIn((household_id, "audit:old"), entities.items)
        self.assertEqual(first.tombstone_payloads_purged, 1)
        self.assertEqual(first.audit_events_deleted, 1)
        self.assertEqual(replay.tombstone_payloads_purged, 0)
        self.assertEqual(replay.audit_events_deleted, 0)


if __name__ == "__main__":
    unittest.main()
