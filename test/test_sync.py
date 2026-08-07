from datetime import UTC, datetime
import hashlib
import json
import unittest
from uuid import uuid4

from pydantic import ValidationError

from app.auth import AuthenticatedIdentity
from app.models import (
    DeviceRegistration,
    MigrationItem,
    MigrationManifest,
    MigrationSourceMode,
    MigrationStatus,
    MigrationUploadRequest,
    MutationAction,
    MutationStatus,
    SyncEntityType,
    SyncMutation,
    SyncPushRequest,
)
from app.services import CloudService
from app.storage.in_memory import InMemoryHouseholdStorage


def registration(device_id=None):
    return DeviceRegistration(
        device_id=device_id or uuid4(),
        name="Synthetic iPhone",
        app_version="0.1.0",
    )


def ledger_payload(amount=8800):
    return {
        "kind": "transaction",
        "direction": "expense",
        "occurredAt": "2026-08-05T03:00:00Z",
        "monthStart": "2026-08-01",
        "channelId": "cash",
        "categoryId": "grocery",
        "amountInFen": amount,
        "note": "not copied into audit",
    }


def mutation(device_id, sequence=1, **overrides):
    values = {
        "mutation_id": uuid4(),
        "device_id": device_id,
        "sequence": sequence,
        "entity_type": SyncEntityType.ledger_entry,
        "entity_id": str(uuid4()),
        "action": MutationAction.create,
        "payload": ledger_payload(),
        "occurred_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SyncMutation(**values)


def migration_digest(items):
    canonical = [
        item.model_dump(by_alias=True, mode="json", exclude_none=True)
        for item in sorted(items, key=lambda value: (value.entity_type.value, str(value.entity_id)))
    ]
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


class CloudSyncTest(unittest.TestCase):
    def setUp(self):
        self.storage = InMemoryHouseholdStorage()
        self.service = CloudService(self.storage)
        self.identity = AuthenticatedIdentity(uid="firebase-owner-1")
        self.device_id = uuid4()
        self.bootstrap = self.service.bootstrap(self.identity, registration(self.device_id))

    def activate_empty_workspace(self):
        digest = hashlib.sha256(b"[]").hexdigest()
        session_id = uuid4()
        request = MigrationUploadRequest(
            device_id=self.device_id,
            manifest=MigrationManifest(
                session_id=session_id,
                source_mode=MigrationSourceMode.local,
                schema_version=1,
                record_counts={},
                content_digest=digest,
            ),
            items=[],
        )
        self.service.stage_migration(self.identity, request)
        self.service.activate_migration(self.identity, session_id, digest)

    def test_bootstrap_is_stable_and_registers_user_household_device_connection(self):
        replay = self.service.bootstrap(self.identity, registration(self.device_id))

        self.assertEqual(replay.household_id, self.bootstrap.household_id)
        self.assertEqual(replay.user_id, self.bootstrap.user_id)
        documents = self.storage.documents_for_household(str(self.bootstrap.household_id))
        self.assertEqual(
            {item["entityType"] for item in documents},
            {"user", "household", "device", "connection"},
        )

    def test_push_replay_is_idempotent_and_pull_uses_cursor(self):
        self.activate_empty_workspace()
        item = mutation(self.device_id)
        first = self.service.push(self.identity, SyncPushRequest(device_id=self.device_id, mutations=[item]))
        replay = self.service.push(self.identity, SyncPushRequest(device_id=self.device_id, mutations=[item]))
        restored = self.service.bootstrap(self.identity, registration(self.device_id))

        self.assertEqual(first.results[0].status, MutationStatus.accepted)
        self.assertEqual(replay.results[0].status, MutationStatus.accepted)
        self.assertEqual(restored.next_outbox_sequence, 2)
        self.assertEqual(restored.sync_cursor, "0")
        self.assertEqual(restored.workspace_status, "active")
        first_page = self.service.pull(self.identity, None, 1)
        empty_page = self.service.pull(self.identity, first_page.next_cursor, 10)
        self.assertEqual(len(first_page.changes), 1)
        self.assertEqual(empty_page.changes, [])
        household_id = str(self.bootstrap.household_id)
        audit = self.storage.read_household_document(household_id, f"audit:{item.mutation_id}")
        self.assertNotIn("note", json.dumps(audit))
        self.assertNotIn("amountInFen", json.dumps(audit))
        listed = self.service.audit(self.identity, None, 10)
        self.assertEqual(listed.events[0].operation_id, str(item.mutation_id))
        self.assertEqual(listed.events[0].actor_id, self.identity.uid)

    def test_stale_revision_conflicts_and_soft_delete_propagates(self):
        self.activate_empty_workspace()
        entity_id = str(uuid4())
        create = mutation(
            self.device_id,
            entity_type=SyncEntityType.member_profile,
            entity_id=entity_id,
            payload={"name": "Owner"},
        )
        self.service.push(self.identity, SyncPushRequest(device_id=self.device_id, mutations=[create]))
        update = mutation(
            self.device_id,
            sequence=2,
            entity_type=SyncEntityType.member_profile,
            entity_id=entity_id,
            action=MutationAction.update,
            base_revision=1,
            payload={"name": "Me"},
        )
        accepted = self.service.push(self.identity, SyncPushRequest(device_id=self.device_id, mutations=[update]))
        stale = mutation(
            self.device_id,
            sequence=3,
            entity_type=SyncEntityType.member_profile,
            entity_id=entity_id,
            action=MutationAction.update,
            base_revision=1,
            payload={"name": "Stale"},
        )
        conflict = self.service.push(self.identity, SyncPushRequest(device_id=self.device_id, mutations=[stale]))
        delete = mutation(
            self.device_id,
            sequence=4,
            entity_type=SyncEntityType.member_profile,
            entity_id=entity_id,
            action=MutationAction.delete,
            base_revision=2,
            payload=None,
        )
        deleted = self.service.push(self.identity, SyncPushRequest(device_id=self.device_id, mutations=[delete]))

        self.assertEqual(accepted.results[0].revision, 2)
        self.assertEqual(conflict.results[0].status, MutationStatus.conflict)
        self.assertEqual(conflict.results[0].server_entity["payload"]["name"], "Me")
        self.assertIsNotNone(deleted.results[0].server_entity["deletedAt"])

    def test_outbox_gap_and_unregistered_device_are_rejected(self):
        self.activate_empty_workspace()
        gap = mutation(self.device_id, sequence=2)
        result = self.service.push(self.identity, SyncPushRequest(device_id=self.device_id, mutations=[gap]))
        self.assertEqual(result.results[0].reason, "outboxSequenceGap")

        foreign = uuid4()
        result = self.service.push(self.identity, SyncPushRequest(device_id=foreign, mutations=[mutation(foreign)]))
        self.assertEqual(result.results[0].reason, "deviceNotRegistered")

    def test_migration_is_verified_resumable_and_activates_once(self):
        item = MigrationItem(
            entity_type=SyncEntityType.ledger_entry,
            entity_id=str(uuid4()),
            payload=ledger_payload(),
            created_at=datetime.now(UTC),
        )
        tombstone = MigrationItem(
            entity_type=SyncEntityType.payment_channel,
            entity_id="cash",
            payload={},
            created_at=datetime.now(UTC),
            deleted_at=datetime.now(UTC),
        )
        digest = migration_digest([item, tombstone])
        session_id = uuid4()
        request = MigrationUploadRequest(
            device_id=self.device_id,
            manifest=MigrationManifest(
                session_id=session_id,
                source_mode=MigrationSourceMode.local,
                schema_version=1,
                record_counts={"ledgerEntry": 1, "paymentChannel": 1},
                content_digest=digest,
            ),
            items=[item, tombstone],
        )

        staged = self.service.stage_migration(self.identity, request)
        replay = self.service.stage_migration(self.identity, request)
        active = self.service.activate_migration(self.identity, session_id, digest)
        first_pull = self.service.pull(self.identity, None, 10)
        activation_replay = self.service.activate_migration(self.identity, session_id, digest)
        empty_pull = self.service.pull(self.identity, first_pull.next_cursor, 10)

        self.assertEqual(staged.status, MigrationStatus.staged)
        self.assertTrue(replay.replayed)
        self.assertEqual(active.status, MigrationStatus.active)
        self.assertTrue(activation_replay.replayed)
        self.assertEqual(len(first_pull.changes), 2)
        self.assertIsNotNone(
            next(change for change in first_pull.changes if change.entity_id == "cash").deleted_at
        )
        self.assertEqual(empty_pull.changes, [])

    def test_money_rejects_float_and_ledger_update_is_forbidden(self):
        with self.assertRaises(ValidationError):
            mutation(self.device_id, payload=ledger_payload(12.5))
        with self.assertRaises(ValidationError):
            mutation(
                self.device_id,
                action=MutationAction.update,
                base_revision=1,
            )

    def test_every_sync_entity_rejects_payloads_the_ios_replica_cannot_apply(self):
        invalid_payloads = [
            (SyncEntityType.ledger_entry, {**ledger_payload(), "occurredAt": "not-a-date"}),
            (SyncEntityType.asset_account, {"name": "Home", "amountInFen": 12.5}),
            (SyncEntityType.asset_snapshot, {
                "accountId": "not-a-uuid",
                "amountInFen": 100,
                "observedAt": "2026-08-05T03:00:00Z",
            }),
            (SyncEntityType.payment_channel, {"name": "Cash", "sortOrder": 0}),
            (SyncEntityType.category, {
                "name": "Dining",
                "symbolName": "fork.knife",
                "sortOrder": 0,
                "isArchived": False,
                "isSystem": False,
                "direction": "sideways",
            }),
            (SyncEntityType.member_profile, {"name": ""}),
        ]
        for entity_type, payload in invalid_payloads:
            with self.subTest(entity_type=entity_type):
                with self.assertRaises(ValidationError):
                    mutation(self.device_id, entity_type=entity_type, payload=payload)

    def test_live_migration_payload_uses_the_same_replica_contract(self):
        with self.assertRaises(ValidationError):
            MigrationItem(
                entity_type=SyncEntityType.category,
                entity_id="category-invalid",
                payload={"name": "Missing replica fields"},
                created_at=datetime.now(UTC),
            )

    def test_asset_categories_share_the_category_contract_with_an_asset_scope(self):
        payload = {
            "name": "存款",
            "symbolName": "building.columns",
            "sortOrder": 0,
            "isArchived": False,
            "isSystem": True,
            "scope": "asset",
            "assetGroup": "financial",
        }
        item = mutation(
            self.device_id,
            entity_type=SyncEntityType.category,
            entity_id="deposit",
            payload=payload,
        )

        self.assertEqual(item.payload["scope"], "asset")
        self.assertTrue(item.payload["isSystem"])
        with self.assertRaises(ValidationError):
            mutation(
                self.device_id,
                entity_type=SyncEntityType.category,
                payload={**payload, "assetGroup": "liability"},
            )

    def test_migration_tombstone_digest_matches_ios_vector(self):
        item = MigrationItem(
            entity_type=SyncEntityType.payment_channel,
            entity_id="cash",
            payload={},
            created_at=datetime(2026, 8, 5, tzinfo=UTC),
            deleted_at=datetime(2026, 8, 5, 1, tzinfo=UTC),
        )
        self.assertEqual(
            migration_digest([item]),
            "6952b9c110a187a02b3549cc4ae3701d3322eab27b531a3db30d913e57cc5370",
        )


if __name__ == "__main__":
    unittest.main()
