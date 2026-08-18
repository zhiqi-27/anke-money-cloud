from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import time
import unittest
from uuid import uuid4

import httpx

from app.auth import AuthenticatedIdentity
from app.config import Settings
from app.models import (
    AgentLedgerEntryCreate,
    AgentPrincipal,
    AgentScope,
    DeviceRegistration,
    MigrationManifest,
    MigrationSourceMode,
    MigrationUploadRequest,
    OperationSource,
    PushTokenRegistration,
)
from app.services import CloudService
from app.services.push_notifications import APNsPushNotificationService
from app.storage.in_memory import InMemoryHouseholdStorage


def settings() -> Settings:
    return Settings(
        environment="test",
        clerk_jwks_url="https://clerk.example/.well-known/jwks.json",
        clerk_issuer="https://clerk.example",
        clerk_audience="",
        clerk_secret_key="",
        clerk_backend_api_url="https://api.clerk.com",
        session_signing_secret="s" * 32,
        session_ttl_seconds=3600,
        cosmos_endpoint="https://example.documents.azure.com:443/",
        cosmos_database="test",
        cosmos_entities_container="entities",
        cosmos_identities_container="identities",
        cosmos_key="",
        cosmos_expected_account_name="example",
        cosmos_allow_smoke_write=False,
        apns_team_id="TEAM123",
        apns_key_id="KEY123",
        apns_private_key="unused-by-test",
        apns_topic="app.ankemoney.ios",
    )


class PushNotificationTest(unittest.TestCase):
    def setUp(self):
        self.storage = InMemoryHouseholdStorage()
        self.cloud = CloudService(self.storage)
        self.identity = AuthenticatedIdentity(uid="owner:1")
        self.device_id = uuid4()
        bootstrap = self.cloud.bootstrap(
            self.identity,
            DeviceRegistration(
                device_id=self.device_id,
                name="Test iPhone",
                app_version="0.1.0",
            ),
        )
        self.household_id = str(bootstrap.household_id)
        digest = hashlib.sha256(b"[]").hexdigest()
        session_id = uuid4()
        self.cloud.stage_migration(
            self.identity,
            MigrationUploadRequest(
                device_id=self.device_id,
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
        self.cloud.register_push_token(
            self.identity,
            PushTokenRegistration(
                device_id=self.device_id,
                token="cd" * 32,
                environment="sandbox",
                topic="app.ankemoney.ios",
                app_version="0.1.0",
            ),
        )

    def service(self, handler) -> APNsPushNotificationService:
        client = httpx.Client(transport=httpx.MockTransport(handler), http2=True)
        service = APNsPushNotificationService(self.storage, settings(), client=client)
        service._bearer_token = "test-token"
        service._bearer_created_at = time.time()
        return service

    def test_background_push_contains_only_a_change_hint(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, request=request)

        result = self.service(handler).notify_household(self.household_id)

        self.assertEqual(result.sent, 1)
        self.assertEqual(requests[0].url.host, "api.sandbox.push.apple.com")
        self.assertEqual(requests[0].headers["apns-push-type"], "background")
        self.assertEqual(requests[0].headers["apns-priority"], "5")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload, {
            "aps": {"content-available": 1},
            "reason": "changesAvailable",
        })
        self.assertNotIn(self.household_id, requests[0].content.decode())

    def test_unregistered_token_is_disabled(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                410,
                json={"reason": "Unregistered"},
                request=request,
            )

        result = self.service(handler).notify_household(self.household_id)

        self.assertEqual(result.disabled, 1)
        self.assertEqual(self.storage.active_push_tokens(self.household_id), [])

    def test_agent_write_notifies_immediately_and_replay_retries_notification(self):
        notifications = []
        cloud = CloudService(
            self.storage,
            change_notifier=lambda household_id: notifications.append(household_id),
        )
        principal = AgentPrincipal(
            household_id=self.household_id,
            connection_id=uuid4(),
            scopes=[AgentScope.ledger_create],
            integration=OperationSource.skill,
        )
        request = AgentLedgerEntryCreate(
            id=uuid4(),
            idempotency_key=uuid4(),
            kind="transaction",
            direction="expense",
            occurred_at=datetime.now(UTC),
            month_start=datetime.now(UTC).date().replace(day=1),
            channel_id="cash",
            category_id="grocery",
            amount_in_fen=8500,
        )

        first = cloud.agent_create_ledger_entry(principal, request)
        replay = cloud.agent_create_ledger_entry(principal, request)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(notifications, [self.household_id, self.household_id])

    def test_notification_failure_never_rolls_back_agent_write(self):
        def fail_notification(_: str) -> None:
            raise RuntimeError("synthetic APNs failure")

        cloud = CloudService(self.storage, change_notifier=fail_notification)
        principal = AgentPrincipal(
            household_id=self.household_id,
            connection_id=uuid4(),
            scopes=[AgentScope.ledger_create],
            integration=OperationSource.skill,
        )
        request = AgentLedgerEntryCreate(
            id=uuid4(),
            idempotency_key=uuid4(),
            kind="transaction",
            direction="expense",
            occurred_at=datetime.now(UTC),
            month_start=datetime.now(UTC).date().replace(day=1),
            channel_id="cash",
            category_id="grocery",
            amount_in_fen=8500,
        )

        result = cloud.agent_create_ledger_entry(principal, request)

        self.assertFalse(result.replayed)
        self.assertIsNotNone(
            self.storage.read_household_document(self.household_id, str(request.id))
        )


if __name__ == "__main__":
    unittest.main()
