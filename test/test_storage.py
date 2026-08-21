from datetime import UTC, date, datetime, timedelta
import hashlib
import unittest
from uuid import UUID, uuid4

from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosResourceNotFoundError

from app.config import ConfigurationError, Settings
from app.models import (
    Actor,
    ActorType,
    AgentScope,
    DeviceRegistration,
    EntryKind,
    LedgerDirection,
    LedgerEntryCreate,
    MigrationManifest,
    MigrationSourceMode,
    MigrationUploadRequest,
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
        clerk_jwks_url="https://clerk.example/.well-known/jwks.json",
        clerk_issuer="https://clerk.example",
        clerk_audience="",
        clerk_secret_key="sk_test_" + "s" * 32,
        clerk_backend_api_url="https://api.clerk.com",
        session_signing_secret="s" * 32,
        session_ttl_seconds=3600,
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
            elif operation == "delete":
                self.items.pop((partition_key, args[0]), None)
                continue
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

    def query_items(
        self,
        *,
        query,
        parameters,
        partition_key=None,
        max_item_count=None,
        **_,
    ):
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
        always_include = set(values.get("@alwaysIncludeTypes", []))
        if "occurredAt" in query or "observedAt" in query:
            temporal_field = "occurredAt" if "occurredAt" in query else "observedAt"
            filtered = []
            for item in documents:
                if item.get("entityType") in always_include:
                    filtered.append(item)
                    continue
                payload = item.get("payload") or item
                temporal_value = payload.get(temporal_field)
                if not isinstance(temporal_value, str):
                    continue
                date_text = temporal_value[:10]
                if date_text < values.get("@startDate", date_text):
                    continue
                if date_text > values.get("@endDate", date_text):
                    continue
                filtered.append(item)
            documents = filtered
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
        elif "ORDER BY c.updatedAt DESC" in query:
            documents.sort(
                key=lambda item: (item.get("updatedAt", ""), item["id"]),
                reverse=True,
            )
        if "@limit" in values:
            documents = documents[:values["@limit"]]
        if "@take" in values:
            documents = documents[:values["@take"]]
        if max_item_count is not None:
            return FakePagedResults(documents, max_item_count)
        return documents


class FakePagedResults:
    def __init__(self, documents, page_size):
        self.documents = documents
        self.page_size = page_size

    def by_page(self, continuation_token=None):
        return FakePageIterator(
            self.documents,
            self.page_size,
            continuation_token,
        )


class FakePageIterator:
    def __init__(self, documents, page_size, continuation_token):
        try:
            self.offset = int(continuation_token or "0")
        except ValueError as exc:
            raise ValueError("Invalid synthetic continuation token") from exc
        self.documents = documents
        self.page_size = page_size
        self.continuation_token = continuation_token
        self._used = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._used or self.offset >= len(self.documents):
            raise StopIteration
        self._used = True
        page = self.documents[self.offset:self.offset + self.page_size]
        next_offset = self.offset + len(page)
        self.continuation_token = (
            str(next_offset) if next_offset < len(self.documents) else None
        )
        return iter(page)


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
        actor = Actor(type=ActorType.user, id="apple:subject-1")

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
        actor = Actor(type=ActorType.user, id="apple:subject-1")
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
        actor = Actor(type=ActorType.user, id="apple:subject-1")
        result = storage.create_ledger_entry(request, actor)

        self.assertIsNone(
            storage.read_household_document("different-household", result.entry.id)
        )


class CosmosHouseholdStorageTest(unittest.TestCase):
    def test_active_empty_workspace_rejects_a_new_migration_session(self):
        entities = FakeCosmosContainer()
        identities = FakeIdentityContainer()
        storage = CosmosHouseholdStorage(
            settings(), container=entities, identities_container=identities
        )
        device_id = uuid4()
        bootstrap = storage.bootstrap_owner(
            "apple:subject-1",
            DeviceRegistration(
                device_id=device_id,
                name="Synthetic iPhone",
                app_version="0.1.0",
            ),
        )
        household_id = str(bootstrap.household_id)
        entities.items[(household_id, household_id)]["status"] = "active"
        digest = hashlib.sha256(b"[]").hexdigest()
        request = MigrationUploadRequest(
            device_id=device_id,
            manifest=MigrationManifest(
                session_id=uuid4(),
                source_mode=MigrationSourceMode.local,
                schema_version=1,
                record_counts={},
                content_digest=digest,
            ),
            items=[],
        )

        with self.assertRaisesRegex(
            ValueError, "Migration requires an empty Agent Cloud household"
        ):
            storage.stage_migration(
                household_id,
                Actor(type=ActorType.user, id="apple:subject-1"),
                request,
            )

    def test_ledger_create_uses_three_item_household_batch(self):
        container = FakeCosmosContainer()
        storage = CosmosHouseholdStorage(settings(), container=container)
        request = make_request()
        actor = Actor(type=ActorType.user, id="apple:subject-1")
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

    def test_agent_ledger_pages_use_date_filter_and_continuation_token(self):
        container = FakeCosmosContainer()
        storage = CosmosHouseholdStorage(settings(), container=container)
        household_id = str(uuid4())
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        container.create_item(body={
            "id": household_id,
            "householdId": household_id,
            "entityType": "household",
            "status": "active",
            "lastChangeSequence": 0,
            "createdAt": now,
            "updatedAt": now,
        })
        actor = Actor(type=ActorType.agent, id=str(uuid4()))
        requests = [
            make_request().model_copy(update={
                "household_id": UUID(household_id),
                "id": uuid4(),
                "idempotency_key": uuid4(),
                "occurred_at": datetime(year, 8, day, tzinfo=UTC),
                "month_start": date(year, 8, 1),
            })
            for year, day in ((2025, 1), (2026, 2), (2026, 3))
        ]
        for request in requests:
            storage.create_ledger_entry(request, actor)

        first, cursor, has_more = storage.list_agent_entities_page(
            household_id,
            {"ledgerEntry"},
            1,
            None,
            date(2026, 1, 1),
            date(2026, 12, 31),
            "occurredAt",
            set(),
        )
        second, final_cursor, final_has_more = storage.list_agent_entities_page(
            household_id,
            {"ledgerEntry"},
            1,
            cursor,
            date(2026, 1, 1),
            date(2026, 12, 31),
            "occurredAt",
            set(),
        )

        self.assertTrue(has_more)
        self.assertIsNotNone(cursor)
        self.assertFalse(final_has_more)
        self.assertIsNone(final_cursor)
        self.assertEqual(
            {item["id"] for item in first + second},
            {str(item.id) for item in requests[1:]},
        )

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
            "apple:subject-1",
            DeviceRegistration(
                device_id=device_id,
                name="Synthetic iPhone",
                app_version="0.1.0",
            ),
        )

        self.assertEqual(result.device_id, device_id)
        self.assertEqual(storage.household_for_uid("apple:subject-1"), str(result.household_id))
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
            "apple:subject-1",
            DeviceRegistration(
                device_id=uuid4(),
                name="Synthetic iPhone",
                app_version="0.1.0",
            ),
        )
        household_id = str(bootstrap.household_id)

        deleted = storage.delete_account_data("apple:subject-1")
        replayed = storage.delete_account_data("apple:subject-1")

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
            "apple:subject-1",
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
                Actor(type=ActorType.user, id="apple:subject-1"),
            mutation,
        )
        restored = storage.bootstrap_owner(
            "apple:subject-1",
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

    def test_sync_replaces_legacy_persisted_sequence_gap_atomically(self):
        entities = FakeCosmosContainer()
        storage = CosmosHouseholdStorage(
            settings(),
            container=entities,
            identities_container=FakeIdentityContainer(),
        )
        device_id = uuid4()
        bootstrap = storage.bootstrap_owner(
            "apple:subject-1",
            DeviceRegistration(device_id=device_id, name="Synthetic iPhone", app_version="0.1.0"),
        )
        household_id = str(bootstrap.household_id)
        mutation_id = uuid4()
        operation_id = f"operation:{mutation_id}"
        audit_id = f"audit:{mutation_id}"
        entities.items[(household_id, operation_id)] = {
            "id": operation_id,
            "householdId": household_id,
            "entityType": "operation",
            "_etag": "operation-etag",
            "result": {
                "mutationId": str(mutation_id),
                "entityId": "member-1",
                "status": "rejected",
                "reason": "outboxSequenceGap",
            },
        }
        entities.items[(household_id, audit_id)] = {
            "id": audit_id,
            "householdId": household_id,
            "entityType": "auditEvent",
            "_etag": "audit-etag",
            "outcome": "rejected",
            "reason": "outboxSequenceGap",
        }
        mutation = SyncMutation(
            mutation_id=mutation_id,
            device_id=device_id,
            sequence=1,
            entity_type=SyncEntityType.member_profile,
            entity_id="member-1",
            action=MutationAction.create,
            payload={"name": "Owner"},
            occurred_at=datetime.now(UTC),
        )

        result = storage.push_mutation(
            household_id,
            Actor(type=ActorType.user, id="apple:subject-1"),
            mutation,
        )

        self.assertEqual(result.status, MutationStatus.accepted)
        cleanup_batch, cleanup_partition = entities.batch_calls[-2]
        self.assertEqual(cleanup_partition, household_id)
        self.assertEqual([item[0] for item in cleanup_batch], ["delete", "delete"])
        self.assertEqual(
            entities.items[(household_id, operation_id)]["result"]["status"],
            "accepted",
        )
        self.assertEqual(
            entities.items[(household_id, audit_id)]["outcome"],
            "accepted",
        )

    def test_cosmos_pull_cursor_is_a_persistent_monotonic_checkpoint(self):
        entities = FakeCosmosContainer()
        storage = CosmosHouseholdStorage(
            settings(), container=entities, identities_container=FakeIdentityContainer()
        )
        device_id = uuid4()
        bootstrap = storage.bootstrap_owner(
            "apple:subject-1",
            DeviceRegistration(device_id=device_id, name="Synthetic iPhone", app_version="0.1.0"),
        )
        actor = Actor(type=ActorType.user, id="apple:subject-1")
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

    def test_agent_api_key_hashes_plaintext_and_resets_in_batch(self):
        entities = FakeCosmosContainer()
        identities = FakeIdentityContainer()
        storage = CosmosHouseholdStorage(
            settings(), container=entities, identities_container=identities
        )
        cloud = CloudService(storage)
        access = AgentAccessService(storage)
        identity = AuthenticatedIdentity(uid="apple:subject-1")
        bootstrap = cloud.bootstrap(
            identity,
            DeviceRegistration(device_id=uuid4(), name="Synthetic iPhone", app_version="0.1.0"),
        )
        household = entities.items[(str(bootstrap.household_id), str(bootstrap.household_id))]
        household["status"] = "active"

        connection = cloud.create_agent_api_key(identity, access)
        stored = storage.read_household_document(
            str(bootstrap.household_id), str(connection.connection_id)
        )
        reset = cloud.create_agent_api_key(identity, access)

        self.assertNotEqual(stored["keyHash"], connection.api_key)
        self.assertNotIn("apiKey", stored)
        self.assertNotEqual(reset.api_key, connection.api_key)
        with self.assertRaises(InvalidAgentTokenError):
            access.authenticate(connection.api_key)
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
        identity = AuthenticatedIdentity(uid="apple:security-owner")
        bootstrap = cloud.bootstrap(
            identity,
            DeviceRegistration(
                device_id=uuid4(), name="Synthetic iPhone", app_version="0.1.0"
            ),
        )
        household_id = str(bootstrap.household_id)
        entities.items[(household_id, household_id)]["status"] = "active"
        created = cloud.create_agent_api_key(identity, access)

        access.authenticate(created.api_key)
        with self.assertRaises(AgentRateLimitExceededError):
            access.authenticate(created.api_key)
        forged = created.api_key[:-1] + ("A" if created.api_key[-1] != "A" else "B")
        for _ in range(3):
            with self.assertRaises(InvalidAgentTokenError):
                access.authenticate(forged)

        reset = cloud.create_agent_api_key(identity, access)
        access.authenticate(reset.api_key)
        cloud.revoke_agent_api_key(identity)
        with self.assertRaises(InvalidAgentTokenError):
            access.authenticate(reset.api_key)

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
            "agent.api_key.reset",
            "agent.api_key.revoke",
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
        identity = AuthenticatedIdentity(uid="apple:concurrent-owner")
        bootstrap = cloud.bootstrap(
            identity,
            DeviceRegistration(
                device_id=uuid4(), name="Synthetic iPhone", app_version="0.1.0"
            ),
        )
        household_id = str(bootstrap.household_id)
        entities.items[(household_id, household_id)]["status"] = "active"
        created = cloud.create_agent_api_key(identity, access)

        entities.fail_next_batch = True
        principal = access.authenticate(created.api_key)

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

    def test_agent_asset_snapshot_atomically_updates_account_and_uses_two_sequences(self):
        entities = FakeCosmosContainer()
        storage = CosmosHouseholdStorage(settings(), container=entities)
        household_id = str(uuid4())
        account_id = str(uuid4())
        now = datetime.now(UTC)
        now_text = now.isoformat().replace("+00:00", "Z")
        entities.create_item(body={
            "id": household_id,
            "householdId": household_id,
            "entityType": "household",
            "status": "active",
            "lastChangeSequence": 7,
            "createdAt": now_text,
            "updatedAt": now_text,
        })
        account = {
            "id": account_id,
            "householdId": household_id,
            "entityType": "assetAccount",
            "revision": 1,
            "createdAt": now_text,
            "updatedAt": now_text,
            "payload": {"name": "Home", "amountInFen": 1_000},
        }
        entities.create_item(body=account)
        updated_account = dict(account)
        updated_account["revision"] = 2
        updated_account["payload"] = {"name": "Home", "amountInFen": 1_200}
        snapshot_id = str(uuid4())

        storage.create_agent_entity(
            household_id,
            Actor(type=ActorType.agent, id=str(uuid4())),
            "assetSnapshot",
            snapshot_id,
            str(uuid4()),
            "assets:update",
            "assets.update",
            "skill",
            {
                "accountId": account_id,
                "amountInFen": 1_200,
                "observedAt": now_text,
            },
            {"before": {"amountInFen": 1_000}, "after": {"amountInFen": 1_200}},
            now,
            related_update=updated_account,
        )

        batch, partition = entities.batch_calls[-1]
        self.assertEqual(partition, household_id)
        self.assertEqual(len(batch), 5)
        persisted_account = entities.items[(household_id, account_id)]
        persisted_snapshot = entities.items[(household_id, snapshot_id)]
        persisted_household = entities.items[(household_id, household_id)]
        self.assertEqual(persisted_account["payload"]["amountInFen"], 1_200)
        self.assertEqual(persisted_account["changeSequence"], 8)
        self.assertEqual(persisted_snapshot["changeSequence"], 9)
        self.assertEqual(persisted_household["lastChangeSequence"], 9)

    def test_agent_asset_create_atomically_creates_account_and_initial_snapshot(self):
        entities = FakeCosmosContainer()
        storage = CosmosHouseholdStorage(settings(), container=entities)
        household_id = str(uuid4())
        account_id = str(uuid4())
        snapshot_id = str(uuid4())
        operation_id = str(uuid4())
        now = datetime.now(UTC)
        now_text = now.isoformat().replace("+00:00", "Z")
        entities.create_item(body={
            "id": household_id,
            "householdId": household_id,
            "entityType": "household",
            "status": "active",
            "lastChangeSequence": 0,
            "createdAt": now_text,
            "updatedAt": now_text,
        })

        account, replayed = storage.create_agent_entity(
            household_id,
            Actor(type=ActorType.agent, id=str(uuid4())),
            "assetAccount",
            account_id,
            operation_id,
            "assets:update",
            "assets.create",
            "skill",
            {"name": "Brokerage", "amountInFen": 1_250_000},
            {"before": None, "after": {"initialSnapshotId": snapshot_id}},
            now,
            related_creates=[{
                "entityType": "assetSnapshot",
                "entityId": snapshot_id,
                "payload": {
                    "accountId": account_id,
                    "amountInFen": 1_250_000,
                    "observedAt": now_text,
                },
            }],
        )
        replay_account, replayed_again = storage.create_agent_entity(
            household_id,
            Actor.model_validate(account["actor"]),
            "assetAccount",
            account_id,
            operation_id,
            "assets:update",
            "assets.create",
            "skill",
            {"name": "Brokerage", "amountInFen": 1_250_000},
            {"before": None, "after": {"initialSnapshotId": snapshot_id}},
            now,
            related_creates=[{
                "entityType": "assetSnapshot",
                "entityId": snapshot_id,
                "payload": {
                    "accountId": account_id,
                    "amountInFen": 1_250_000,
                    "observedAt": now_text,
                },
            }],
        )

        self.assertFalse(replayed)
        self.assertTrue(replayed_again)
        self.assertEqual(account["id"], replay_account["id"])
        batch, partition = entities.batch_calls[-1]
        self.assertEqual(partition, household_id)
        self.assertEqual(len(batch), 5)
        self.assertEqual(entities.items[(household_id, account_id)]["changeSequence"], 1)
        self.assertEqual(entities.items[(household_id, snapshot_id)]["changeSequence"], 2)
        self.assertEqual(entities.items[(household_id, household_id)]["lastChangeSequence"], 2)

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
