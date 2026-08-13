from datetime import UTC, date, datetime
import hashlib
import unittest
from uuid import uuid4

from pydantic import ValidationError

from app.auth import AuthenticatedIdentity
from app.main import fastapi_app
from app.models import (
    AgentAssetUpdate,
    AgentLedgerEntryCreate,
    AgentScope,
    DeviceRegistration,
    MigrationManifest,
    MigrationSourceMode,
    MigrationUploadRequest,
)
from app.services import (
    AgentAccessService,
    AgentRateLimitExceededError,
    CloudService,
    InvalidAgentTokenError,
)
from app.storage.in_memory import InMemoryHouseholdStorage


class AgentSecurityTest(unittest.TestCase):
    def setUp(self):
        self.storage = InMemoryHouseholdStorage()
        self.cloud = CloudService(self.storage)
        self.identity = AuthenticatedIdentity(uid="firebase-security-owner")
        bootstrap = self.cloud.bootstrap(
            self.identity,
            DeviceRegistration(
                device_id=uuid4(),
                name="Security test iPhone",
                app_version="0.1.0",
            ),
        )
        digest = hashlib.sha256(b"[]").hexdigest()
        session_id = uuid4()
        self.cloud.stage_migration(
            self.identity,
            MigrationUploadRequest(
                device_id=bootstrap.device_id,
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

    def test_reset_and_revoke_invalidate_the_previous_api_key(self):
        access = AgentAccessService(self.storage)
        created = self.cloud.create_agent_api_key(self.identity, access)
        reset = self.cloud.create_agent_api_key(self.identity, access)

        with self.assertRaises(InvalidAgentTokenError):
            access.authenticate(created.api_key)
        self.assertEqual(access.authenticate(reset.api_key).scopes, list(AgentScope))

        self.cloud.revoke_agent_api_key(self.identity)
        with self.assertRaises(InvalidAgentTokenError):
            access.authenticate(reset.api_key)
        actions = {item.action for item in self.cloud.audit(self.identity, None, 100).events}
        self.assertTrue({
            "agent.api_key.create", "agent.api_key.reset", "agent.api_key.revoke"
        }.issubset(actions))

    def test_rate_limit_is_shared_per_connection_and_audited_once_per_window(self):
        access = AgentAccessService(self.storage, requests_per_minute=2)
        created = self.cloud.create_agent_api_key(self.identity, access)

        access.authenticate(created.api_key)
        access.authenticate(created.api_key)
        with self.assertRaises(AgentRateLimitExceededError):
            access.authenticate(created.api_key)
        with self.assertRaises(AgentRateLimitExceededError):
            access.authenticate(created.api_key)

        events = self.cloud.audit(self.identity, None, 100).events
        rate_events = [item for item in events if item.action == "agent.rate_limit"]
        self.assertEqual(len(rate_events), 1)
        self.assertEqual(rate_events[0].reason, "requestRateExceeded")
        self.assertEqual(rate_events[0].actor_id, str(created.connection_id))

    def test_repeated_invalid_known_connection_tokens_raise_one_anomaly_without_lockout(self):
        access = AgentAccessService(self.storage, failed_auth_threshold=5)
        created = self.cloud.create_agent_api_key(self.identity, access)
        prefix = created.api_key.rsplit("_", maxsplit=1)[0]
        forged = f"{prefix}_forged"

        for _ in range(6):
            with self.assertRaises(InvalidAgentTokenError):
                access.authenticate(forged)

        anomalies = [
            item for item in self.cloud.audit(self.identity, None, 100).events
            if item.action == "agent.authentication.anomaly"
        ]
        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0].reason, "repeatedInvalidToken")
        self.assertEqual(access.authenticate(created.api_key).connection_id, created.connection_id)

    def test_malicious_money_and_date_parameters_are_rejected(self):
        ledger = {
            "id": uuid4(),
            "idempotency_key": uuid4(),
            "kind": "transaction",
            "direction": "expense",
            "occurred_at": datetime(2026, 8, 6, tzinfo=UTC),
            "month_start": date(2026, 8, 1),
            "channel_id": "cash",
            "category_id": "grocery",
            "amount_in_fen": 100,
        }
        for change in (
            {"amount_in_fen": 9_000_000_000_000_001},
            {"amount_in_fen": True},
            {"occurred_at": datetime(2026, 8, 6)},
            {"month_start": date(2026, 8, 2)},
            {"category_id": "x" * 129},
        ):
            with self.subTest(change=change):
                with self.assertRaises(ValidationError):
                    AgentLedgerEntryCreate(**(ledger | change))

        for change in (
            {"amount_in_fen": 9_000_000_000_000_001},
            {"amount_in_fen": 1.5},
            {"observed_at": datetime(2026, 8, 6)},
        ):
            with self.subTest(change=change):
                with self.assertRaises(ValidationError):
                    AgentAssetUpdate(**({
                        "snapshot_id": uuid4(),
                        "idempotency_key": uuid4(),
                        "amount_in_fen": 100,
                        "observed_at": datetime(2026, 8, 6, tzinfo=UTC),
                    } | change))

    def test_agent_surface_has_no_audit_delete_scope_route_or_tool(self):
        self.assertNotIn("audit", {scope.value for scope in AgentScope})
        schema = fastapi_app.openapi()
        agent_paths = {
            path: set(methods)
            for path, methods in schema["paths"].items()
            if path.startswith("/agent/v1/")
        }
        self.assertFalse(any("delete" in methods for methods in agent_paths.values()))
        self.assertFalse(any("audit" in path for path in agent_paths))
        self.assertFalse(any("household" in path for path in agent_paths))


if __name__ == "__main__":
    unittest.main()
