from datetime import UTC, date, datetime
import hashlib
import unittest
from uuid import uuid4

from app.auth import AuthenticatedIdentity
from app.models import (
    Actor,
    ActorType,
    AgentAssetUpdate,
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
            DeviceRegistration(
                device_id=uuid4(), name="Synthetic iPhone", app_version="0.1.0"
            ),
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

    def test_api_key_has_all_six_capabilities_and_plaintext_is_not_stored(self):
        created = self.cloud.create_agent_api_key(self.identity, self.access)
        principal = self.access.authenticate(created.api_key)
        stored = self.storage.read_household_document(
            str(self.bootstrap.household_id), str(created.connection_id)
        )

        self.assertEqual(set(principal.scopes), set(AgentScope))
        self.assertEqual(set(created.scopes), set(AgentScope))
        self.assertTrue(created.api_key.startswith("ank_"))
        self.assertEqual(len(created.api_key), 59)
        self.assertNotEqual(stored["keyHash"], created.api_key)
        self.assertNotIn("apiKey", stored)
        self.assertNotIn("grantExpiresAt", stored)
        self.assertNotIn("refreshTokenHash", stored)

    def test_precompact_full_capability_api_key_remains_valid_until_reset(self):
        created = self.cloud.create_agent_api_key(self.identity, self.access)
        household_id = str(self.bootstrap.household_id)
        legacy_key = f"ank_{household_id}_{created.connection_id}_{'A' * 43}"
        self.storage.replace_agent_api_key(
            household_id,
            Actor(type=ActorType.user, id=self.identity.uid),
            str(created.connection_id),
            self.access.hash_token(legacy_key),
            legacy_key[:13],
            datetime.now(UTC),
        )

        self.assertEqual(
            self.access.authenticate(legacy_key).connection_id,
            created.connection_id,
        )

    def test_api_key_creates_idempotent_remote_entry_and_app_pull_sees_it(self):
        created = self.cloud.create_agent_api_key(self.identity, self.access)
        principal = self.access.authenticate(created.api_key)
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
        self.assertEqual(pulled.changes[0].payload["amountInFen"], 8800)
        with self.assertRaises(ValueError):
            self.cloud.agent_create_ledger_entry(
                principal, request.model_copy(update={"amount_in_fen": 9_900})
            )

    def test_reset_invalidates_the_previous_key_without_changing_identity(self):
        first = self.cloud.create_agent_api_key(self.identity, self.access)
        second = self.cloud.create_agent_api_key(self.identity, self.access)

        self.assertEqual(first.connection_id, second.connection_id)
        self.assertNotEqual(first.api_key, second.api_key)
        with self.assertRaises(InvalidAgentTokenError):
            self.access.authenticate(first.api_key)
        self.assertEqual(
            self.access.authenticate(second.api_key).connection_id,
            second.connection_id,
        )
        with self.assertRaises(InvalidAgentTokenError):
            self.access.authenticate("legacy.connection.token")
        actions = {event.action for event in self.cloud.audit(self.identity, None, 30).events}
        self.assertTrue({"agent.api_key.create", "agent.api_key.reset"}.issubset(actions))

    def test_revocation_immediately_rejects_the_api_key(self):
        created = self.cloud.create_agent_api_key(self.identity, self.access)
        self.cloud.revoke_agent_api_key(self.identity)

        with self.assertRaises(InvalidAgentTokenError):
            self.access.authenticate(created.api_key)
        self.assertIsNone(self.cloud.agent_api_key(self.identity))
        rejected = [
            event for event in self.cloud.audit(self.identity, None, 30).events
            if event.action == "agent.authenticate" and event.outcome == "rejected"
        ]
        self.assertEqual(rejected[0].reason, "revokedOrInvalid")

    def test_offline_device_and_api_key_writes_merge_without_loss(self):
        second_device = self.cloud.bootstrap(
            self.identity,
            DeviceRegistration(
                device_id=uuid4(), name="Offline iPhone", app_version="0.1.0"
            ),
        )
        created = self.cloud.create_agent_api_key(self.identity, self.access)
        principal = self.access.authenticate(created.api_key)
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
        mutation = SyncMutation(
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
        self.cloud.push(
            self.identity,
            SyncPushRequest(device_id=second_device.device_id, mutations=[mutation]),
        )
        pulled = self.cloud.pull(self.identity, "0", 10)

        self.assertEqual(
            {change.entity_id for change in pulled.changes},
            {str(remote_id), str(local_id)},
        )

    def test_full_api_key_reads_and_updates_assets_and_reference_data(self):
        created = self.cloud.create_agent_api_key(self.identity, self.access)
        principal = self.access.authenticate(created.api_key)
        actor = Actor(type=ActorType.agent, id=str(principal.connection_id))
        now = datetime.now(UTC)
        account_id = uuid4()
        for entity_type, entity_id, payload in (
            ("assetAccount", str(account_id), {"name": "Home", "amountInFen": 1_000}),
            ("paymentChannel", "cash", {"name": "Cash", "sortOrder": 0}),
            ("category", "grocery", {"name": "Grocery", "sortOrder": 0}),
        ):
            self.storage.create_agent_entity(
                str(principal.household_id), actor, entity_type, entity_id,
                str(uuid4()), "test.seed", "test.seed", "skill", payload,
                {"before": None, "after": {"revision": 1}}, now,
            )
        snapshot = AgentAssetUpdate(
            snapshot_id=uuid4(), idempotency_key=uuid4(),
            amount_in_fen=1_200, observed_at=now,
        )

        first = self.cloud.agent_update_asset(principal, account_id, snapshot)
        replay = self.cloud.agent_update_asset(principal, account_id, snapshot)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertTrue(self.cloud.agent_list_assets(principal, 20).items)
        self.cloud.agent_list_ledger_entries(principal, 20)
        self.assertTrue(self.cloud.agent_list_categories(principal, 20).items)
        self.assertTrue(self.cloud.agent_list_channels(principal, 20).items)


if __name__ == "__main__":
    unittest.main()
