from datetime import UTC, datetime, timedelta
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.auth import AuthenticatedIdentity
from app.dependencies import admin_service, billing_service, current_admin, current_identity
from app.main import fastapi_app
from app.models import AdminGrantCreateRequest, AdminGrantRevokeRequest
from app.services.admin import AdminService, AdminIdempotencyConflictError
from app.services.billing import AppleBillingService, VerifiedAppleTransaction
from app.storage.in_memory import InMemoryHouseholdStorage


class FakeAdminClerkVerifier:
    def verify_bearer_token(self, authorization: str) -> AuthenticatedIdentity:
        if authorization != "Bearer admin-token":
            raise ValueError("invalid")
        return AuthenticatedIdentity(
            uid="clerk:admin-1",
            provider="clerk",
            provider_subject="admin-1",
        )


class FakeAppleVerifier:
    def __init__(self, transaction):
        self.transaction = transaction

    def verify_transaction(self, signed_transaction: str):
        return self.transaction

    def transaction_from_notification(self, signed_payload: str):
        return self.transaction


class AdminServiceTest(unittest.TestCase):
    def setUp(self):
        self.storage = InMemoryHouseholdStorage()
        self.user = AuthenticatedIdentity(
            uid="clerk:user-1",
            provider="clerk",
            provider_subject="user-1",
            email="person@example.com",
            display_name="Anke friend",
        )
        self.admin = AuthenticatedIdentity(
            uid="clerk:admin-1", provider="clerk", provider_subject="admin-1"
        )
        self.storage.ensure_identity(self.user)
        self.service = AdminService(self.storage)
        self.now = datetime.now(UTC)

    def test_manual_grant_is_effective_and_revoke_is_idempotent(self):
        key = str(uuid4())
        request = AdminGrantCreateRequest(
            starts_at=self.now,
            expires_at=self.now + timedelta(days=30),
            reason="Beta tester access",
        )
        created = self.service.create_manual_grant(
            self.admin, self.user.uid, request, key, "request-1", self.now
        )
        self.assertTrue(created.effective_entitlement.active)
        self.assertEqual(created.effective_entitlement.sources, ["manualGrant"])
        replay = self.service.create_manual_grant(
            self.admin, self.user.uid, request, key, "request-2", self.now
        )
        self.assertTrue(replay.replayed)

        revoke_key = str(uuid4())
        revoke = self.service.revoke_manual_grant(
            self.admin,
            self.user.uid,
            created.grant.id,
            AdminGrantRevokeRequest(reason="Support request completed"),
            revoke_key,
            "request-3",
            self.now,
        )
        self.assertFalse(revoke.effective_entitlement.active)
        replay_revoke = self.service.revoke_manual_grant(
            self.admin,
            self.user.uid,
            created.grant.id,
            AdminGrantRevokeRequest(reason="Support request completed"),
            revoke_key,
            "request-4",
            self.now,
        )
        self.assertTrue(replay_revoke.replayed)
        audits = self.service.audit(
            uid=self.user.uid,
            action=None,
            outcome=None,
            from_at=None,
            to_at=None,
            limit=25,
            cursor=None,
        )
        self.assertEqual(len(audits.items), 2)

    def test_changed_create_body_with_same_key_is_rejected(self):
        key = str(uuid4())
        request = AdminGrantCreateRequest(
            starts_at=self.now,
            expires_at=self.now + timedelta(days=30),
            reason="First reason",
        )
        self.service.create_manual_grant(
            self.admin, self.user.uid, request, key, "request-1", self.now
        )
        with self.assertRaises(AdminIdempotencyConflictError):
            self.service.create_manual_grant(
                self.admin,
                self.user.uid,
                request.model_copy(update={"reason": "Changed reason"}),
                key,
                "request-2",
                self.now,
            )

    def test_apple_and_manual_sources_are_union_not_overwritten(self):
        transaction = VerifiedAppleTransaction(
            original_transaction_id="1000000000001",
            transaction_id="2000000000001",
            product_id="app.ankemoney.ios.pro.yearly",
            expires_at=self.now + timedelta(days=365),
            revoked_at=None,
            environment="Sandbox",
        )
        AppleBillingService(self.storage, FakeAppleVerifier(transaction)).verify_and_bind(
            self.user, "signed"
        )
        request = AdminGrantCreateRequest(
            starts_at=self.now,
            expires_at=self.now + timedelta(days=7),
            reason="Support recovery",
        )
        response = self.service.create_manual_grant(
            self.admin, self.user.uid, request, str(uuid4()), "request-1", self.now
        )
        self.assertEqual(response.effective_entitlement.sources, ["apple", "manualGrant"])

    def test_account_deletion_removes_manual_grants_and_admin_audit(self):
        request = AdminGrantCreateRequest(
            starts_at=self.now,
            expires_at=self.now + timedelta(days=7),
            reason="Deletion cleanup test",
        )
        self.service.create_manual_grant(
            self.admin, self.user.uid, request, str(uuid4()), "request-1", self.now
        )

        self.storage.delete_account_data(self.user.uid)

        self.assertIsNone(self.storage.identity_membership(self.user.uid))
        self.assertEqual(self.storage.manual_pro_grants(self.user.uid), [])
        audits = self.service.audit(
            uid=self.user.uid,
            action=None,
            outcome=None,
            from_at=None,
            to_at=None,
            limit=25,
            cursor=None,
        )
        self.assertEqual(audits.items, [])


class AdminApiTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(fastapi_app, raise_server_exceptions=False)
        self.storage = InMemoryHouseholdStorage()
        self.user = AuthenticatedIdentity(
            uid="clerk:user-2",
            provider="clerk",
            provider_subject="user-2",
            email="user2@example.com",
            display_name="Second user",
        )
        self.admin = AuthenticatedIdentity(
            uid="clerk:admin-1", provider="clerk", provider_subject="admin-1"
        )
        self.storage.ensure_identity(self.user)
        fastapi_app.dependency_overrides[current_admin] = lambda: self.admin
        fastapi_app.dependency_overrides[admin_service] = lambda: AdminService(self.storage)

    def tearDown(self):
        fastapi_app.dependency_overrides.clear()

    def test_admin_routes_do_not_return_financial_data_and_round_trip_grant(self):
        response = self.client.get("/admin/v1/users", params={"q": "user2@example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["uid"], self.user.uid)
        self.assertNotIn("householdId", response.text)
        self.assertNotIn("ledger", response.text.lower())

        now = datetime.now(UTC)
        grant_key = str(uuid4())
        response = self.client.post(
            f"/admin/v1/users/{self.user.uid}/manual-pro-grants",
            headers={"Idempotency-Key": grant_key},
            json={
                "grantType": "fixedTerm",
                "startsAt": now.isoformat().replace("+00:00", "Z"),
                "expiresAt": (now + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
                "reason": "Development access",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["effectiveEntitlement"]["active"])

        entitlement = self.client.get(
            f"/admin/v1/users/{self.user.uid}/entitlement"
        )
        self.assertEqual(entitlement.status_code, 200)
        self.assertEqual(entitlement.json()["effective"]["sources"], ["manualGrant"])

    def test_admin_auth_empty_allowlist_fails_closed(self):
        fastapi_app.dependency_overrides.clear()
        with patch.dict(os.environ, {"ANKE_ADMIN_CLERK_SUBJECTS": ""}, clear=False):
            with patch("app.dependencies.get_clerk_verifier", return_value=FakeAdminClerkVerifier()):
                response = self.client.get(
                    "/admin/v1/overview", headers={"Authorization": "Bearer admin-token"}
                )
        self.assertEqual(response.status_code, 503)

    def test_billing_entitlement_reads_manual_grant_source(self):
        fastapi_app.dependency_overrides[current_identity] = lambda: self.user
        fastapi_app.dependency_overrides[billing_service] = lambda: AppleBillingService(
            self.storage,
            FakeAppleVerifier(None),
        )
        now = datetime.now(UTC)
        AdminService(self.storage).create_manual_grant(
            self.admin,
            self.user.uid,
            AdminGrantCreateRequest(
                starts_at=now,
                expires_at=now + timedelta(days=7),
                reason="Billing readback test",
            ),
            str(uuid4()),
            "request-1",
            now,
        )

        response = self.client.get("/api/v1/billing/entitlement")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sources"], ["manualGrant"])


if __name__ == "__main__":
    unittest.main()
