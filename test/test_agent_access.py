from datetime import UTC, date, datetime, timedelta
import hashlib
import unittest
from uuid import uuid4

from app.auth import AuthenticatedIdentity
from app.models import (
    Actor,
    ActorType,
    AgentAssetBatchCreate,
    AgentAssetCreate,
    AgentAssetUpdate,
    AgentLedgerBatchCreate,
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
from app.services import (
    AgentAccessService,
    CloudService,
    InvalidAgentTokenError,
    ProEntitlementRequiredError,
)
from app.storage.in_memory import InMemoryHouseholdStorage


class AgentAccessTest(unittest.TestCase):
    def setUp(self):
        self.storage = InMemoryHouseholdStorage()
        self.cloud = CloudService(self.storage)
        self.access = AgentAccessService(self.storage)
        self.identity = AuthenticatedIdentity(uid="apple:owner-1")
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

    def test_free_account_cannot_create_or_use_agent_api_key(self):
        free_cloud = CloudService(
            self.storage,
            entitlement_checker=lambda _: False,
        )
        with self.assertRaises(ProEntitlementRequiredError):
            free_cloud.create_agent_api_key(self.identity, self.access)

        created = self.cloud.create_agent_api_key(self.identity, self.access)
        free_access = AgentAccessService(
            self.storage,
            entitlement_checker=lambda _: False,
        )
        with self.assertRaises(ProEntitlementRequiredError):
            free_access.authenticate(created.api_key)

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

    def test_agent_writes_use_the_workspace_accounting_currency(self):
        settings_id = "5a59e59a-dde4-4555-86f6-188ff576bb03"
        self.cloud.push(
            self.identity,
            SyncPushRequest(
                device_id=self.bootstrap.device_id,
                mutations=[SyncMutation(
                    mutation_id=uuid4(),
                    device_id=self.bootstrap.device_id,
                    sequence=1,
                    entity_type=SyncEntityType.financial_space_settings,
                    entity_id=settings_id,
                    action=MutationAction.create,
                    payload={
                        "accountingCurrencyCode": "USD",
                        "monthlyBudgetMinor": 0,
                        "expenseCategoryBudgets": {},
                    },
                    occurred_at=datetime.now(UTC),
                )],
            ),
        )
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
            amount_in_fen=1299,
        )

        response = self.cloud.agent_create_ledger_entry(principal, request)

        self.assertEqual(response.entry.amount_minor, 1299)
        self.assertEqual(response.entry.currency_code, "USD")
        self.assertEqual(response.entry.amount_in_fen, 1299)

    def test_confirmed_batch_replays_and_date_pages_without_duplicates(self):
        created = self.cloud.create_agent_api_key(self.identity, self.access)
        principal = self.access.authenticate(created.api_key)
        requests = [
            AgentLedgerEntryCreate(
                id=uuid4(),
                idempotency_key=uuid4(),
                kind="transaction",
                direction="expense",
                occurred_at=datetime(year, 8, day, tzinfo=UTC),
                month_start=date(year, 8, 1),
                channel_id="cash",
                category_id="grocery",
                amount_in_fen=day * 100,
            )
            for year, day in ((2025, 1), (2026, 2), (2026, 3), (2026, 4))
        ]
        batch = AgentLedgerBatchCreate(entries=requests)

        first = self.cloud.agent_create_ledger_batch(principal, batch)
        replay = self.cloud.agent_create_ledger_batch(principal, batch)
        first_page = self.cloud.agent_list_ledger_entries(
            principal,
            2,
            None,
            date(2026, 1, 1),
            date(2026, 12, 31),
        )
        second_page = self.cloud.agent_list_ledger_entries(
            principal,
            2,
            first_page.next_cursor,
            date(2026, 1, 1),
            date(2026, 12, 31),
        )

        self.assertEqual(first.created_count, 4)
        self.assertEqual(first.replayed_count, 0)
        self.assertEqual(replay.created_count, 0)
        self.assertEqual(replay.replayed_count, 4)
        self.assertTrue(first_page.has_more)
        self.assertFalse(second_page.has_more)
        returned_ids = {
            item.entity_id for item in first_page.items + second_page.items
        }
        self.assertEqual(returned_ids, {str(item.id) for item in requests[1:]})
        pulled = self.cloud.pull(self.identity, None, 20)
        self.assertEqual(
            {change.entity_id for change in pulled.changes},
            {str(item.id) for item in requests},
        )
        with self.assertRaisesRegex(ValueError, "startDate"):
            self.cloud.agent_list_ledger_entries(
                principal,
                20,
                None,
                date(2026, 12, 31),
                date(2026, 1, 1),
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
            (
                "assetSnapshot",
                str(uuid4()),
                {
                    "accountId": str(account_id),
                    "amountInFen": 900,
                    "observedAt": "2025-08-05T00:00:00Z",
                },
            ),
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
        updated_account = self.storage.read_household_document(
            str(principal.household_id), str(account_id)
        )
        self.assertEqual(updated_account["payload"]["amountInFen"], 1_200)
        self.assertEqual(updated_account["revision"], 2)
        self.assertEqual(
            updated_account["payload"]["balanceObservedAt"],
            now.isoformat().replace("+00:00", "Z"),
        )

        historical = AgentAssetUpdate(
            snapshot_id=uuid4(),
            idempotency_key=uuid4(),
            amount_in_fen=800,
            observed_at=now - timedelta(days=1),
        )
        self.cloud.agent_update_asset(principal, account_id, historical)
        account_after_historical_snapshot = self.storage.read_household_document(
            str(principal.household_id), str(account_id)
        )
        self.assertEqual(
            account_after_historical_snapshot["payload"]["amountInFen"], 1_200
        )
        self.assertEqual(account_after_historical_snapshot["revision"], 2)
        self.assertTrue(self.cloud.agent_list_assets(principal, 20).items)
        current_assets = self.cloud.agent_list_assets(
            principal,
            20,
            None,
            date(now.year, 1, 1),
            date(now.year, 12, 31),
        )
        self.assertEqual(
            {item.entity_type for item in current_assets.items},
            {"assetAccount", "assetSnapshot"},
        )
        self.assertNotIn(
            "2025-08-05T00:00:00Z",
            {item.payload.get("observedAt") for item in current_assets.items},
        )
        self.cloud.agent_list_ledger_entries(principal, 20)
        self.assertTrue(self.cloud.agent_list_categories(principal, 20).items)
        self.assertTrue(self.cloud.agent_list_channels(principal, 20).items)

    def test_creates_asset_account_and_initial_snapshot_idempotently_in_batches(self):
        created = self.cloud.create_agent_api_key(self.identity, self.access)
        principal = self.access.authenticate(created.api_key)
        actor = Actor(type=ActorType.agent, id=str(principal.connection_id))
        now = datetime.now(UTC)
        household_id = str(principal.household_id)
        self.storage.create_agent_entity(
            household_id,
            actor,
            "category",
            "asset-category:stocks",
            str(uuid4()),
            "test.seed",
            "test.seed",
            "skill",
            {
                "name": "Stocks",
                "scope": "asset",
                "assetGroup": "financial",
                "isArchived": False,
            },
            {"before": None, "after": {"revision": 1}},
            now,
        )
        request = AgentAssetCreate(
            account_id=uuid4(),
            snapshot_id=uuid4(),
            idempotency_key=uuid4(),
            name="Brokerage",
            kind="asset",
            asset_group="financial",
            category_id="asset-category:stocks",
            money_bucket="risk",
            amount_in_fen=1_250_000,
            observed_at=now,
        )

        first = self.cloud.agent_create_asset(principal, request)
        replay = self.cloud.agent_create_asset(principal, request)
        batch_request = request.model_copy(
            update={
                "account_id": uuid4(),
                "snapshot_id": uuid4(),
                "idempotency_key": uuid4(),
                "name": "Second Brokerage",
            }
        )
        batch = AgentAssetBatchCreate(accounts=[request, batch_request])
        batch_result = self.cloud.agent_create_asset_batch(principal, batch)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.account.entity_id, str(request.account_id))
        self.assertEqual(first.initial_snapshot.entity_id, str(request.snapshot_id))
        self.assertEqual(first.account.payload["amountInFen"], 1_250_000)
        self.assertEqual(first.initial_snapshot.payload["accountId"], str(request.account_id))
        self.assertEqual(batch_result.created_count, 1)
        self.assertEqual(batch_result.replayed_count, 1)
        pulled = self.cloud.pull(self.identity, None, 20).changes
        pulled_ids = [change.entity_id for change in pulled]
        self.assertLess(pulled_ids.index(str(request.account_id)), pulled_ids.index(str(request.snapshot_id)))
        with self.assertRaisesRegex(ValueError, "Idempotency key"):
            self.cloud.agent_create_asset(
                principal, request.model_copy(update={"amount_in_fen": 1_300_000})
            )
        audit = self.cloud.audit(self.identity, None, 100)
        event = next(item for item in audit.events if item.target_id == str(request.account_id))
        self.assertEqual(event.scope, "assets:update")
        self.assertEqual(event.action, "assets.create")
        self.assertEqual(event.change_summary["after"]["initialSnapshotId"], str(request.snapshot_id))


if __name__ == "__main__":
    unittest.main()
