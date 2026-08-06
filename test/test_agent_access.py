from datetime import UTC, date, datetime
import hashlib
import unittest
from uuid import uuid4

from pydantic import ValidationError

from app.auth import AuthenticatedIdentity
from app.models import (
    Actor,
    ActorType,
    AgentAssetUpdate,
    AgentConnectionCreate,
    AgentLedgerEntryCreate,
    AgentScope,
    DeviceRegistration,
    MigrationManifest,
    MigrationSourceMode,
    MigrationUploadRequest,
    MutationAction,
    SyncEntityType,
    SyncMutation,
    SyncPushRequest,
)
from app.services import AgentAccessService, CloudService, InvalidAgentTokenError
from app.storage.in_memory import InMemoryHouseholdStorage


class AgentAccessTest(unittest.TestCase):
    def setUp(self):
        self.storage = InMemoryHouseholdStorage()
        self.cloud = CloudService(self.storage)
        self.access = AgentAccessService(self.storage)
        self.identity = AuthenticatedIdentity(uid="firebase-owner-1")
        self.bootstrap = self.cloud.bootstrap(
            self.identity,
            DeviceRegistration(device_id=uuid4(), name="Synthetic iPhone", app_version="0.1.0"),
        )
        digest = hashlib.sha256(b"[]").hexdigest()
        session_id = uuid4()
        self.cloud.stage_migration(
            self.identity,
            MigrationUploadRequest(
                device_id=self.bootstrap.device_id,
                manifest=MigrationManifest(
                    session_id=session_id,
                    source_mode=MigrationSourceMode.local,
                    schema_version=1,
                    record_counts={},
                    content_digest=digest,
                ),
                items=[],
            ),
        )
        self.cloud.activate_migration(self.identity, session_id, digest)

    def test_scoped_token_creates_idempotent_remote_entry_and_app_pull_sees_it(self):
        connection = self.cloud.create_agent_connection(
            self.identity,
            AgentConnectionCreate(
                name="Synthetic bookkeeping agent",
                scopes=[AgentScope.ledger_create],
            ),
            self.access,
        )
        principal = self.access.authenticate(connection.access_token)
        request = AgentLedgerEntryCreate(
            id=uuid4(),
            idempotency_key=uuid4(),
            kind="transaction",
            direction="expense",
            occurred_at=datetime(2026, 8, 5, tzinfo=UTC),
            month_start=date(2026, 8, 1),
            channel_id="cash",
            category_id="grocery",
            amount_in_fen=8800,
            note="synthetic agent write",
        )

        first = self.cloud.agent_create_ledger_entry(principal, request)
        replay = self.cloud.agent_create_ledger_entry(principal, request)
        pulled = self.cloud.pull(self.identity, None, 10)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(len(pulled.changes), 1)
        self.assertEqual(pulled.changes[0].payload["amountInFen"], 8800)
        stored_connection = self.storage.read_household_document(
            str(self.bootstrap.household_id), str(connection.connection_id)
        )
        self.assertNotEqual(stored_connection["tokenHash"], connection.access_token)
        audit = self.cloud.audit(self.identity, None, 20)
        self.assertTrue(any(event.actor_type == "agent" for event in audit.events))

        conflicting_reuse = request.model_copy(update={"amount_in_fen": 9_900})
        with self.assertRaises(ValueError):
            self.cloud.agent_create_ledger_entry(principal, conflicting_reuse)

    def test_offline_device_write_and_agent_write_merge_without_loss_after_reconnect(self):
        second_device = self.cloud.bootstrap(
            self.identity,
            DeviceRegistration(
                device_id=uuid4(),
                name="Offline iPhone",
                app_version="0.1.0",
            ),
        )
        connection = self.cloud.create_agent_connection(
            self.identity,
            AgentConnectionCreate(
                name="Synthetic bookkeeping agent",
                scopes=[AgentScope.ledger_create],
            ),
            self.access,
        )
        principal = self.access.authenticate(connection.access_token)
        remote_id = uuid4()
        self.cloud.agent_create_ledger_entry(
            principal,
            AgentLedgerEntryCreate(
                id=remote_id,
                idempotency_key=uuid4(),
                kind="transaction",
                direction="expense",
                occurred_at=datetime(2026, 8, 5, 2, tzinfo=UTC),
                month_start=date(2026, 8, 1),
                channel_id="cash",
                category_id="grocery",
                amount_in_fen=8_800,
            ),
        )

        local_id = uuid4()
        local_mutation = SyncMutation(
            mutation_id=uuid4(),
            device_id=second_device.device_id,
            sequence=second_device.next_outbox_sequence,
            entity_type=SyncEntityType.ledger_entry,
            entity_id=str(local_id),
            action=MutationAction.create,
            payload={
                "kind": "transaction",
                "direction": "expense",
                "occurredAt": "2026-08-05T01:00:00Z",
                "monthStart": "2026-08-01",
                "channelId": "cash",
                "categoryId": "grocery",
                "amountInFen": 1_200,
                "note": None,
                "memberProfileId": None,
            },
            occurred_at=datetime(2026, 8, 5, 3, tzinfo=UTC),
        )
        pushed = self.cloud.push(
            self.identity,
            SyncPushRequest(
                device_id=second_device.device_id,
                mutations=[local_mutation],
            ),
        )
        replay = self.cloud.push(
            self.identity,
            SyncPushRequest(
                device_id=second_device.device_id,
                mutations=[local_mutation],
            ),
        )
        pulled = self.cloud.pull(self.identity, "0", 10)

        self.assertEqual(pushed.results[0].status.value, "accepted")
        self.assertEqual(replay.results[0].entity_id, str(local_id))
        self.assertEqual(
            {change.entity_id for change in pulled.changes},
            {str(remote_id), str(local_id)},
        )
        self.assertEqual(
            {change.payload["amountInFen"] for change in pulled.changes},
            {8_800, 1_200},
        )

    def test_revocation_immediately_rejects_token(self):
        connection = self.cloud.create_agent_connection(
            self.identity,
            AgentConnectionCreate(name="Read agent", scopes=[AgentScope.ledger_read]),
            self.access,
        )
        self.cloud.revoke_agent_connection(self.identity, connection.connection_id)

        with self.assertRaises(InvalidAgentTokenError):
            self.access.authenticate(connection.access_token)

        audit = self.cloud.audit(self.identity, None, 20)
        rejected_auth = [
            event for event in audit.events
            if event.action == "agent.authenticate" and event.outcome == "rejected"
        ]
        self.assertEqual(len(rejected_auth), 1)
        self.assertEqual(rejected_auth[0].reason, "revokedExpiredOrInvalid")

    def test_refresh_rotates_access_token_and_cannot_outlive_or_bypass_grant(self):
        connection = self.cloud.create_agent_connection(
            self.identity,
            AgentConnectionCreate(
                name="Long read agent",
                scopes=[AgentScope.ledger_read],
                grant_duration_seconds=24 * 60 * 60,
            ),
            self.access,
        )

        refreshed = self.access.refresh(connection.refresh_token)
        principal = self.access.authenticate(refreshed.access_token)

        self.assertEqual(principal.connection_id, connection.connection_id)
        self.assertNotEqual(refreshed.access_token, connection.access_token)
        self.assertLessEqual(
            refreshed.token_expires_at,
            connection.grant_expires_at,
        )
        with self.assertRaises(InvalidAgentTokenError):
            self.access.authenticate(connection.access_token)

        self.cloud.revoke_agent_connection(self.identity, connection.connection_id)
        with self.assertRaises(InvalidAgentTokenError):
            self.access.refresh(connection.refresh_token)

    def test_write_requires_scope_and_lifetime_caps_are_validated(self):
        connection = self.cloud.create_agent_connection(
            self.identity,
            AgentConnectionCreate(name="Read agent", scopes=[AgentScope.ledger_read]),
            self.access,
        )
        principal = self.access.authenticate(connection.access_token)
        request = AgentLedgerEntryCreate(
            id=uuid4(), idempotency_key=uuid4(),
            kind="transaction", direction="income",
            occurred_at=datetime.now(UTC), month_start=date(2026, 8, 1),
            category_id="salary", amount_in_fen=100,
        )
        with self.assertRaises(PermissionError):
            self.cloud.agent_create_ledger_entry(principal, request)
        with self.assertRaises(ValidationError):
            AgentConnectionCreate(
                name="Too long",
                scopes=[AgentScope.ledger_create],
                grant_duration_seconds=24 * 60 * 60 + 1,
            )

    def test_every_initial_scope_is_enforced_and_remote_asset_write_is_idempotent(self):
        connection = self.cloud.create_agent_connection(
            self.identity,
            AgentConnectionCreate(
                name="Full scoped agent",
                scopes=list(AgentScope),
            ),
            self.access,
        )
        principal = self.access.authenticate(connection.access_token)
        actor = Actor(type=ActorType.agent, id=str(principal.connection_id))
        now = datetime.now(UTC)
        account_id = uuid4()
        self.storage.create_agent_entity(
            str(principal.household_id), actor, "assetAccount", str(account_id),
            str(uuid4()), "test.seed", "test.seed", "api",
            {"name": "Home", "amountInFen": 1_000},
            {"before": None, "after": {"revision": 1}}, now,
        )
        self.storage.create_agent_entity(
            str(principal.household_id), actor, "paymentChannel", "cash",
            str(uuid4()), "test.seed", "test.seed", "api",
            {"name": "Cash", "sortOrder": 0},
            {"before": None, "after": {"revision": 1}}, now,
        )
        self.storage.create_agent_entity(
            str(principal.household_id), actor, "category", "grocery",
            str(uuid4()), "test.seed", "test.seed", "api",
            {"name": "Grocery", "sortOrder": 0},
            {"before": None, "after": {"revision": 1}}, now,
        )
        snapshot = AgentAssetUpdate(
            snapshot_id=uuid4(),
            idempotency_key=uuid4(),
            amount_in_fen=1_200,
            observed_at=now,
        )

        first = self.cloud.agent_update_asset(principal, account_id, snapshot)
        replay = self.cloud.agent_update_asset(principal, account_id, snapshot)
        conflicting_reuse = snapshot.model_copy(update={"amount_in_fen": 1_300})
        with self.assertRaises(ValueError):
            self.cloud.agent_update_asset(principal, account_id, conflicting_reuse)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.item.payload["amountInFen"], 1_200)
        self.assertTrue(self.cloud.agent_list_assets(principal, 20).items)
        self.cloud.agent_list_ledger_entries(principal, 20)
        self.assertTrue(self.cloud.agent_list_categories(principal, 20).items)
        self.assertTrue(self.cloud.agent_list_channels(principal, 20).items)

        read_only = self.cloud.create_agent_connection(
            self.identity,
            AgentConnectionCreate(name="Ledger reader", scopes=[AgentScope.ledger_read]),
            self.access,
        )
        read_principal = self.access.authenticate(read_only.access_token)
        with self.assertRaises(PermissionError):
            self.cloud.agent_list_assets(read_principal, 20)
        with self.assertRaises(PermissionError):
            self.cloud.agent_update_asset(read_principal, account_id, snapshot)

        audit = self.cloud.audit(self.identity, None, 100)
        denied = [event for event in audit.events if event.reason == "insufficientScope"]
        self.assertEqual({event.scope for event in denied}, {
            AgentScope.assets_read.value,
            AgentScope.assets_update.value,
        })


if __name__ == "__main__":
    unittest.main()
