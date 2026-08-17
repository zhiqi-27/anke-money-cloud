import hashlib
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.auth import AuthenticatedIdentity, InvalidTokenError
from app.config import ConfigurationError
from app.dependencies import (
    agent_access_service,
    auth_service,
    cloud_service,
    clerk_management_service,
)
from app.main import fastapi_app
from app.models import (
    AnkeSessionResponse,
    MigrationManifest,
    MigrationSourceMode,
    MigrationUploadRequest,
)
from app.services import CloudService
from app.services import AgentAccessService
from app.storage.in_memory import InMemoryHouseholdStorage


class FakeTokenVerifier:
    def verify_bearer_token(self, authorization: str) -> AuthenticatedIdentity:
        if authorization != "Bearer valid-test-token":
            raise InvalidTokenError("invalid")
        return AuthenticatedIdentity(
            uid="clerk:subject-1",
            provider="clerk",
            provider_subject="subject-1",
        )


class MisconfiguredTokenVerifier:
    def verify_bearer_token(self, authorization: str) -> AuthenticatedIdentity:
        raise ConfigurationError("missing secret detail")


class FakeClerkManagement:
    def delete_user(self, provider_subject: str):
        if provider_subject != "subject-1":
            raise AssertionError("unexpected Clerk subject")


class FakeClerkAuthService:
    def sign_in_with_clerk(self, authorization: str) -> AnkeSessionResponse:
        if authorization != "Bearer valid-clerk-token":
            raise AssertionError("unexpected Clerk authorization")
        return AnkeSessionResponse(
            access_token="anke-session-token",
            expires_at="2026-08-14T12:00:00Z",
            uid="clerk:subject-1",
            provider="clerk",
            display_name="Anke User",
            email="user@example.com",
        )


class ApiContractTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(fastapi_app, raise_server_exceptions=False)

    def tearDown(self):
        fastapi_app.dependency_overrides.clear()

    @staticmethod
    def activate_empty_workspace(storage: InMemoryHouseholdStorage, device_id: str):
        service = CloudService(storage)
        identity = AuthenticatedIdentity(
            uid="clerk:subject-1", provider="clerk", provider_subject="subject-1"
        )
        digest = hashlib.sha256(b"[]").hexdigest()
        session_id = uuid4()
        service.stage_migration(
            identity,
            MigrationUploadRequest(
                device_id=device_id,
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
        service.activate_migration(identity, session_id, digest)

    def test_ping_is_public_and_non_sensitive(self):
        response = self.client.get("/ping")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertNotIn("cosmos", response.text.lower())
        self.assertNotIn("token", response.text.lower())

    def test_openapi_includes_health_and_protected_identity_route(self):
        response = self.client.get("/openapi.json")

        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        self.assertIn("/ping", paths)
        self.assertIn("/api/v1/me", paths)
        self.assertIn("/api/v1/account", paths)
        self.assertIn("/api/v1/bootstrap", paths)
        self.assertIn("/api/v1/devices/push-token", paths)
        self.assertIn("/api/v1/sync/push", paths)
        self.assertIn("/api/v1/sync/pull", paths)
        self.assertIn("/api/v1/migrations", paths)
        self.assertIn("/api/v1/audit", paths)
        self.assertIn("/api/v1/agent-api-key", paths)
        self.assertNotIn("/api/v1/agent-connections", paths)
        self.assertNotIn("/agent/v1/token/refresh", paths)
        self.assertIn("/agent/v1/ledger/entries", paths)
        self.assertIn("/agent/v1/assets", paths)
        self.assertIn("/agent/v1/assets/{account_id}", paths)
        self.assertIn("/agent/v1/categories", paths)
        self.assertIn("/agent/v1/channels", paths)
        identity_operation = paths["/api/v1/me"]["get"]
        self.assertEqual(identity_operation["security"], [{"HTTPBearer": []}])
        self.assertEqual(
            paths["/agent/v1/assets"]["get"]["security"],
            [{"AgentBearer": []}],
        )
        self.assertIn("HTTPBearer", response.json()["components"]["securitySchemes"])
        self.assertIn("AgentBearer", response.json()["components"]["securitySchemes"])
        self.assertEqual(
            response.json()["components"]["schemas"]["MigrationSourceMode"]["enum"],
            ["local"],
        )
        agent_methods = {
            path: set(operation.keys())
            for path, operation in paths.items()
            if path.startswith("/agent/")
        }
        self.assertEqual(agent_methods, {
            "/agent/v1/ledger/entries": {"get", "post"},
            "/agent/v1/assets": {"get"},
            "/agent/v1/assets/{account_id}": {"patch"},
            "/agent/v1/categories": {"get"},
            "/agent/v1/channels": {"get"},
        })
        self.assertFalse(any(
            fragment in path
            for path in agent_methods
            for fragment in (
                "member", "setting", "migration", "export",
                "connection", "audit", "import",
            )
        ))
        self.assertNotIn("AgentRefreshBearer", response.json()["components"]["securitySchemes"])

    def test_cloudkit_migration_source_is_rejected(self):
        storage = InMemoryHouseholdStorage()
        service = CloudService(storage)
        fastapi_app.dependency_overrides[cloud_service] = lambda: service
        headers = {"Authorization": "Bearer valid-test-token"}
        device_id = "abababab-abab-abab-abab-abababababab"
        digest = hashlib.sha256(b"[]").hexdigest()
        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            self.client.post(
                "/api/v1/bootstrap",
                headers=headers,
                json={
                    "deviceId": device_id,
                    "name": "iPhone",
                    "platform": "ios",
                    "appVersion": "0.1.0",
                },
            )
            response = self.client.post(
                "/api/v1/migrations",
                headers=headers,
                json={
                    "deviceId": device_id,
                    "manifest": {
                        "sessionId": "11111111-1111-1111-1111-111111111111",
                        "sourceMode": "cloudkit",
                        "schemaVersion": 1,
                        "recordCounts": {},
                        "contentDigest": digest,
                    },
                    "items": [],
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {
            "detail": "Request validation failed",
            "errors": [{
                "location": "body.manifest.sourceMode",
                "type": "enum",
            }],
        })
        self.assertNotIn("cloudkit", response.text)

    def test_migration_validation_reports_only_safe_field_location_and_type(self):
        storage = InMemoryHouseholdStorage()
        service = CloudService(storage)
        fastapi_app.dependency_overrides[cloud_service] = lambda: service
        headers = {"Authorization": "Bearer valid-test-token"}
        body = {
            "deviceId": "abababab-abab-abab-abab-abababababab",
            "manifest": {
                "sessionId": "11111111-1111-1111-1111-111111111111",
                "sourceMode": "local",
                "schemaVersion": 1,
                "recordCounts": {"ledgerEntry": 1},
                "contentDigest": hashlib.sha256(b"[]").hexdigest(),
            },
            "items": [{
                "entityType": "ledgerEntry",
                "entityId": "private-entry-id",
                "payload": {
                    "kind": "transaction",
                    "direction": "expense",
                    "occurredAt": "2026-08-13T00:00:00Z",
                    "monthStart": "2026-08-01",
                    "channelId": "private-channel-id",
                    "categoryId": "private-category-id",
                    "amountInFen": 0,
                    "note": "private financial note",
                },
                "createdAt": "2026-08-13T00:00:00Z",
                "deletedAt": None,
            }],
        }

        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            response = self.client.post("/api/v1/migrations", headers=headers, json=body)

        self.assertEqual(response.status_code, 422)
        error = response.json()["errors"][0]
        self.assertEqual(error["location"], "body.items.0")
        self.assertEqual(error["type"], "value_error")
        self.assertNotIn("private", response.text)
        self.assertNotIn("amountInFen", response.text)

    def test_migration_rejects_cross_type_cosmos_id_collisions(self):
        headers = {"Authorization": "Bearer valid-test-token"}
        timestamp = "2026-08-13T00:00:00Z"
        items = [
            {
                "entityType": "paymentChannel",
                "entityId": "other",
                "payload": {
                    "name": "Channel",
                    "symbolName": "wallet",
                    "assetName": None,
                    "sortOrder": 0,
                    "isArchived": False,
                    "isSystem": True,
                },
                "createdAt": timestamp,
            },
            {
                "entityType": "category",
                "entityId": "other",
                "payload": {
                    "name": "Category",
                    "symbolName": "tag",
                    "sortOrder": 0,
                    "isArchived": False,
                    "isSystem": True,
                    "direction": "expense",
                },
                "createdAt": timestamp,
            },
        ]
        body = {
            "deviceId": "abababab-abab-abab-abab-abababababab",
            "manifest": {
                "sessionId": "11111111-1111-1111-1111-111111111111",
                "sourceMode": "local",
                "schemaVersion": 1,
                "recordCounts": {"paymentChannel": 1, "category": 1},
                "contentDigest": hashlib.sha256(b"collision").hexdigest(),
            },
            "items": items,
        }

        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            response = self.client.post("/api/v1/migrations", headers=headers, json=body)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["errors"], [{
            "location": "body",
            "type": "value_error",
        }])
        self.assertNotIn("other", response.text)

    def test_protected_route_rejects_missing_or_invalid_token(self):
        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            missing = self.client.get("/api/v1/me")
            invalid = self.client.get(
                "/api/v1/me", headers={"Authorization": "Bearer wrong"}
            )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(missing.headers["www-authenticate"], "Bearer")

    def test_protected_route_redacts_auth_configuration_failure(self):
        with patch(
            "app.dependencies.get_token_verifier",
            return_value=MisconfiguredTokenVerifier(),
        ):
            response = self.client.get(
                "/api/v1/me",
                headers={"Authorization": "Bearer any-token"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Authentication service unavailable"})
        self.assertNotIn("secret", response.text)

    def test_protected_route_returns_only_verified_uid(self):
        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            response = self.client.get(
                "/api/v1/me",
                headers={"Authorization": "Bearer valid-test-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "uid": "clerk:subject-1",
                "provider": "clerk",
                "displayName": None,
                "email": None,
            },
        )

    def test_clerk_exchange_returns_an_anke_session(self):
        fastapi_app.dependency_overrides[auth_service] = lambda: FakeClerkAuthService()

        response = self.client.post(
            "/api/v1/auth/clerk/exchange",
            headers={"Authorization": "Bearer valid-clerk-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["accessToken"], "anke-session-token")
        self.assertEqual(response.json()["uid"], "clerk:subject-1")
        self.assertEqual(response.json()["provider"], "clerk")

    def test_account_deletion_erases_household_and_is_idempotent(self):
        storage = InMemoryHouseholdStorage()
        fastapi_app.dependency_overrides[cloud_service] = lambda: CloudService(storage)
        headers = {"Authorization": "Bearer valid-test-token"}
        body = {
            "deviceId": "22222222-2222-2222-2222-222222222222",
            "name": "Synthetic iPhone",
            "platform": "ios",
            "appVersion": "0.1.0",
        }

        fastapi_app.dependency_overrides[clerk_management_service] = lambda: FakeClerkManagement()
        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            self.assertEqual(self.client.post("/api/v1/bootstrap", headers=headers, json=body).status_code, 200)
            first = self.client.request("DELETE", "/api/v1/account", headers=headers)
            second = self.client.request("DELETE", "/api/v1/account", headers=headers)

        self.assertEqual(first.status_code, 204)
        self.assertEqual(second.status_code, 204)
        self.assertIsNone(storage.household_for_uid("clerk:subject-1"))

    def test_azure_functions_entry_imports_without_credentials(self):
        import function_app

        self.assertIsNotNone(function_app.app)
        self.assertEqual(
            {item.get_function_name() for item in function_app.app.get_functions()},
            {
                "http_app_func",
                "enforce_data_retention",
                "notify_devices_of_cloud_changes",
            },
        )

    def test_authenticated_bootstrap_and_sync_routes_never_accept_household_from_client(self):
        storage = InMemoryHouseholdStorage()
        fastapi_app.dependency_overrides[cloud_service] = lambda: CloudService(storage)
        headers = {"Authorization": "Bearer valid-test-token"}
        bootstrap_body = {
            "deviceId": "22222222-2222-2222-2222-222222222222",
            "name": "Synthetic iPhone",
            "platform": "ios",
            "appVersion": "0.1.0",
        }
        mutation = {
            "mutationId": "11111111-1111-1111-1111-111111111111",
            "deviceId": bootstrap_body["deviceId"],
            "sequence": 1,
            "entityType": "ledgerEntry",
            "entityId": "entry-1",
            "action": "create",
            "baseRevision": None,
            "occurredAt": "2026-08-05T00:00:00Z",
            "payload": {
                "kind": "transaction",
                "direction": "expense",
                "occurredAt": "2026-08-05T00:00:00Z",
                "monthStart": "2026-08-01",
                "channelId": "cash",
                "categoryId": "grocery",
                "amountInFen": 8800,
            },
        }
        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            bootstrap = self.client.post("/api/v1/bootstrap", headers=headers, json=bootstrap_body)
            self.activate_empty_workspace(storage, bootstrap_body["deviceId"])
            pushed = self.client.post(
                "/api/v1/sync/push",
                headers=headers,
                json={"deviceId": bootstrap_body["deviceId"], "mutations": [mutation]},
            )
            pulled = self.client.get("/api/v1/sync/pull?limit=10", headers=headers)
            audit = self.client.get("/api/v1/audit?limit=10", headers=headers)

        self.assertEqual(bootstrap.status_code, 200)
        self.assertEqual(bootstrap.json()["nextOutboxSequence"], 1)
        self.assertEqual(bootstrap.json()["workspaceStatus"], "empty")
        self.assertEqual(pushed.status_code, 200)
        self.assertEqual(pushed.json()["results"][0]["status"], "accepted")
        self.assertEqual(pulled.status_code, 200)
        self.assertEqual(pulled.json()["changes"][0]["entityId"], "entry-1")
        self.assertEqual(audit.status_code, 200)
        self.assertEqual(audit.json()["events"][0]["operationId"], mutation["mutationId"])
        self.assertNotIn("amountInFen", audit.text)
        self.assertNotIn("note", audit.text)
        self.assertNotIn("householdId", mutation)

    def test_sync_requires_bootstrap_membership(self):
        fastapi_app.dependency_overrides[cloud_service] = lambda: CloudService(InMemoryHouseholdStorage())
        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            response = self.client.get(
                "/api/v1/sync/pull",
                headers={"Authorization": "Bearer valid-test-token"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "Bootstrap required"})

    def test_empty_workspace_rejects_normal_sync_and_agent_authorization(self):
        storage = InMemoryHouseholdStorage()
        fastapi_app.dependency_overrides[cloud_service] = lambda: CloudService(storage)
        fastapi_app.dependency_overrides[agent_access_service] = lambda: AgentAccessService(storage)
        headers = {"Authorization": "Bearer valid-test-token"}
        device_id = "22222222-2222-2222-2222-222222222222"
        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            self.client.post(
                "/api/v1/bootstrap",
                headers=headers,
                json={"deviceId": device_id, "name": "iPhone", "platform": "ios", "appVersion": "0.1.0"},
            )
            sync = self.client.post(
                "/api/v1/sync/push",
                headers=headers,
                json={"deviceId": device_id, "mutations": [{
                    "mutationId": "11111111-1111-1111-1111-111111111111",
                    "deviceId": device_id,
                    "sequence": 1,
                    "entityType": "memberProfile",
                    "entityId": "owner",
                    "action": "create",
                    "occurredAt": "2026-08-05T00:00:00Z",
                    "payload": {"name": "Owner"},
                }]},
            )
            connection = self.client.post(
                "/api/v1/agent-api-key",
                headers=headers,
            )

        self.assertEqual(sync.status_code, 409)
        self.assertEqual(connection.status_code, 409)
        self.assertEqual(sync.json(), {"detail": "Agent Cloud workspace is not active"})

    def test_agent_api_key_writes_while_owner_app_is_offline(self):
        storage = InMemoryHouseholdStorage()
        fastapi_app.dependency_overrides[cloud_service] = lambda: CloudService(storage)
        fastapi_app.dependency_overrides[agent_access_service] = lambda: AgentAccessService(storage)
        owner_headers = {"Authorization": "Bearer valid-test-token"}
        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            bootstrap = self.client.post(
                "/api/v1/bootstrap",
                headers=owner_headers,
                json={"deviceId": "22222222-2222-2222-2222-222222222222", "name": "iPhone", "platform": "ios", "appVersion": "0.1.0"},
            )
            self.activate_empty_workspace(
                storage, "22222222-2222-2222-2222-222222222222"
            )
            connection = self.client.post(
                "/api/v1/agent-api-key",
                headers=owner_headers,
            )
        self.assertEqual(bootstrap.status_code, 200)
        self.assertEqual(connection.status_code, 200)
        agent_token = connection.json()["apiKey"]
        agent_write = self.client.post(
            "/agent/v1/ledger/entries",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "id": "33333333-3333-3333-3333-333333333333",
                "idempotencyKey": "44444444-4444-4444-4444-444444444444",
                "source": "skill",
                "kind": "transaction",
                "direction": "expense",
                "occurredAt": "2026-08-05T00:00:00Z",
                "monthStart": "2026-08-01",
                "channelId": "cash",
                "categoryId": "grocery",
                "amountInFen": 8800,
            },
        )
        replay = self.client.post(
            "/agent/v1/ledger/entries",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={
                "id": "33333333-3333-3333-3333-333333333333",
                "idempotencyKey": "44444444-4444-4444-4444-444444444444",
                "source": "skill",
                "kind": "transaction",
                "direction": "expense",
                "occurredAt": "2026-08-05T00:00:00Z",
                "monthStart": "2026-08-01",
                "channelId": "cash",
                "categoryId": "grocery",
                "amountInFen": 8800,
            },
        )
        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            owner_push = self.client.post(
                "/api/v1/sync/push",
                headers=owner_headers,
                json={
                    "deviceId": "22222222-2222-2222-2222-222222222222",
                    "mutations": [{
                        "mutationId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                        "deviceId": "22222222-2222-2222-2222-222222222222",
                        "sequence": 1,
                        "entityType": "ledgerEntry",
                        "entityId": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                        "action": "create",
                        "occurredAt": "2026-08-05T00:30:00Z",
                        "payload": {
                            "kind": "transaction",
                            "direction": "expense",
                            "occurredAt": "2026-08-05T00:30:00Z",
                            "monthStart": "2026-08-01",
                            "channelId": "cash",
                            "categoryId": "grocery",
                            "amountInFen": 1200,
                        },
                    }],
                },
            )
            owner_pull = self.client.get(
                f"/api/v1/sync/pull?cursor={bootstrap.json()['syncCursor']}",
                headers=owner_headers,
            )
        self.assertEqual(agent_write.status_code, 200)
        self.assertEqual(set(connection.json()["scopes"]), {
            "ledger:read", "ledger:create", "assets:read", "assets:update",
            "categories:read", "channels:read",
        })
        self.assertFalse(agent_write.json()["replayed"])
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(owner_push.status_code, 200)
        self.assertEqual(
            {item["entityId"] for item in owner_pull.json()["changes"]},
            {
                "33333333-3333-3333-3333-333333333333",
                "cccccccc-cccc-cccc-cccc-cccccccccccc",
            },
        )
        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            audit = self.client.get("/api/v1/audit?limit=100", headers=owner_headers)
        agent_event = next(
            event for event in audit.json()["events"]
            if event["targetId"] == "33333333-3333-3333-3333-333333333333"
        )
        self.assertEqual(agent_event["source"], "skill")

    def test_all_initial_agent_scopes_have_separate_enforced_routes(self):
        storage = InMemoryHouseholdStorage()
        fastapi_app.dependency_overrides[cloud_service] = lambda: CloudService(storage)
        fastapi_app.dependency_overrides[agent_access_service] = lambda: AgentAccessService(storage)
        owner_headers = {"Authorization": "Bearer valid-test-token"}
        device_id = "55555555-5555-5555-5555-555555555555"
        account_id = "66666666-6666-6666-6666-666666666666"
        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            self.client.post(
                "/api/v1/bootstrap",
                headers=owner_headers,
                json={"deviceId": device_id, "name": "iPhone", "platform": "ios", "appVersion": "0.1.0"},
            )
            self.activate_empty_workspace(storage, device_id)
            seeded = self.client.post(
                "/api/v1/sync/push",
                headers=owner_headers,
                json={"deviceId": device_id, "mutations": [
                    {
                        "mutationId": "77777777-7777-7777-7777-777777777777",
                        "deviceId": device_id,
                        "sequence": 1,
                        "entityType": "assetAccount",
                        "entityId": account_id,
                        "action": "create",
                        "occurredAt": "2026-08-05T00:00:00Z",
                        "payload": {"name": "Home", "amountInFen": 1000},
                    },
                    {
                        "mutationId": "88888888-8888-8888-8888-888888888888",
                        "deviceId": device_id,
                        "sequence": 2,
                        "entityType": "paymentChannel",
                        "entityId": "cash",
                        "action": "create",
                        "occurredAt": "2026-08-05T00:00:00Z",
                        "payload": {
                            "name": "Cash",
                            "symbolName": "banknote",
                            "assetName": None,
                            "sortOrder": 0,
                            "isArchived": False,
                            "isSystem": False,
                        },
                    },
                    {
                        "mutationId": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                        "deviceId": device_id,
                        "sequence": 3,
                        "entityType": "category",
                        "entityId": "grocery",
                        "action": "create",
                        "occurredAt": "2026-08-05T00:00:00Z",
                        "payload": {
                            "name": "Grocery",
                            "symbolName": "cart",
                            "sortOrder": 0,
                            "isArchived": False,
                            "isSystem": False,
                            "direction": "expense",
                        },
                    },
                ]},
            )
            connection = self.client.post(
                "/api/v1/agent-api-key",
                headers=owner_headers,
            )
        self.assertEqual(seeded.status_code, 200)
        token = connection.json()["apiKey"]
        agent_headers = {"Authorization": f"Bearer {token}"}

        assets = self.client.get("/agent/v1/assets", headers=agent_headers)
        categories = self.client.get("/agent/v1/categories", headers=agent_headers)
        channels = self.client.get("/agent/v1/channels", headers=agent_headers)
        ledger = self.client.get("/agent/v1/ledger/entries", headers=agent_headers)
        snapshot_body = {
            "snapshotId": "99999999-9999-9999-9999-999999999999",
            "idempotencyKey": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "amountInFen": 1200,
            "observedAt": "2026-08-05T01:00:00Z",
        }
        snapshot = self.client.patch(
            f"/agent/v1/assets/{account_id}", headers=agent_headers, json=snapshot_body
        )
        snapshot_replay = self.client.patch(
            f"/agent/v1/assets/{account_id}", headers=agent_headers, json=snapshot_body
        )
        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            audit = self.client.get("/api/v1/audit?limit=100", headers=owner_headers)

        self.assertEqual(assets.status_code, 200)
        self.assertEqual(assets.json()["items"][0]["entityType"], "assetAccount")
        self.assertEqual(categories.status_code, 200)
        self.assertEqual(categories.json()["items"][0]["entityType"], "category")
        self.assertEqual(channels.status_code, 200)
        self.assertEqual(channels.json()["items"][0]["entityType"], "paymentChannel")
        self.assertEqual(ledger.status_code, 200)
        self.assertEqual(snapshot.status_code, 200)
        self.assertFalse(snapshot.json()["replayed"])
        self.assertTrue(snapshot_replay.json()["replayed"])
        self.assertIn('"source":"skill"', audit.text)
        self.assertIn('"idempotencyKey":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"', audit.text)
        self.assertIn('"amountInFen":1000', audit.text)
        self.assertIn('"amountInFen":1200', audit.text)
        self.assertNotIn("must not enter audit", audit.text)

    def test_owner_reset_revoke_last_use_and_agent_rate_limit_contract(self):
        storage = InMemoryHouseholdStorage()
        service = CloudService(storage)
        access = AgentAccessService(storage, requests_per_minute=2)
        fastapi_app.dependency_overrides[cloud_service] = lambda: service
        fastapi_app.dependency_overrides[agent_access_service] = lambda: access
        owner_headers = {"Authorization": "Bearer valid-test-token"}
        device_id = "abababab-abab-abab-abab-abababababab"
        with patch("app.dependencies.get_token_verifier", return_value=FakeTokenVerifier()):
            self.client.post(
                "/api/v1/bootstrap",
                headers=owner_headers,
                json={
                    "deviceId": device_id,
                    "name": "iPhone",
                    "platform": "ios",
                    "appVersion": "0.1.0",
                },
            )
            self.activate_empty_workspace(storage, device_id)
            created = self.client.post(
                "/api/v1/agent-api-key",
                headers=owner_headers,
            )
            token = created.json()["apiKey"]
            agent_headers = {"Authorization": f"Bearer {token}"}
            first = self.client.get("/agent/v1/ledger/entries", headers=agent_headers)
            second = self.client.get("/agent/v1/ledger/entries", headers=agent_headers)
            limited = self.client.get("/agent/v1/ledger/entries", headers=agent_headers)
            metadata = self.client.get(
                "/api/v1/agent-api-key", headers=owner_headers
            )
            reset = self.client.post("/api/v1/agent-api-key", headers=owner_headers)
            old_key_request = self.client.get(
                "/agent/v1/ledger/entries", headers=agent_headers
            )
            new_headers = {"Authorization": f"Bearer {reset.json()['apiKey']}"}
            revoked = self.client.delete("/api/v1/agent-api-key", headers=owner_headers)
            revoked_request = self.client.get(
                "/agent/v1/ledger/entries", headers=new_headers
            )
            audit = self.client.get("/api/v1/audit?limit=100", headers=owner_headers)

        self.assertEqual(created.status_code, 200)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.headers["retry-after"], "60")
        self.assertIsNotNone(metadata.json()["lastUsedAt"])
        self.assertEqual(old_key_request.status_code, 401)
        self.assertEqual(revoked.json()["status"], "revoked")
        self.assertEqual(revoked_request.status_code, 401)
        self.assertIn('"action":"agent.api_key.reset"', audit.text)
        self.assertIn('"action":"agent.api_key.revoke"', audit.text)
        self.assertIn('"action":"agent.rate_limit"', audit.text)


if __name__ == "__main__":
    unittest.main()
