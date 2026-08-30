from datetime import UTC, datetime, timedelta
import unittest

from app.auth import AuthenticatedIdentity
from app.services.billing import (
    AppleBillingService,
    AppleTransactionAlreadyLinkedError,
    VerifiedAppleTransaction,
)
from app.storage.in_memory import InMemoryHouseholdStorage


class FakeAppleVerifier:
    def __init__(self, transaction):
        self.transaction = transaction

    def verify_transaction(self, signed_transaction: str):
        if signed_transaction != "signed-transaction":
            raise AssertionError("unexpected transaction")
        return self.transaction

    def transaction_from_notification(self, signed_payload: str):
        if signed_payload != "signed-notification":
            raise AssertionError("unexpected notification")
        return self.transaction


class AppleBillingServiceTest(unittest.TestCase):
    def setUp(self):
        self.storage = InMemoryHouseholdStorage()
        self.identity = AuthenticatedIdentity(uid="clerk:owner-1")
        self.storage.ensure_identity(self.identity)
        self.transaction = VerifiedAppleTransaction(
            original_transaction_id="1000000000001",
            transaction_id="2000000000001",
            product_id="app.ankemoney.ios.pro.yearly",
            expires_at=datetime.now(UTC) + timedelta(days=365),
            revoked_at=None,
            environment="Sandbox",
        )
        self.service = AppleBillingService(
            self.storage, FakeAppleVerifier(self.transaction)
        )

    def test_verified_transaction_binds_and_activates_entitlement(self):
        entitlement = self.service.verify_and_bind(
            self.identity, "signed-transaction"
        )

        self.assertTrue(entitlement.active)
        self.assertEqual(entitlement.product_id, self.transaction.product_id)
        household_id = self.storage.household_for_uid(self.identity.uid)
        self.assertTrue(self.storage.has_active_pro_entitlement(household_id))

    def test_subscription_cannot_be_linked_to_another_account(self):
        self.service.verify_and_bind(self.identity, "signed-transaction")
        another = AuthenticatedIdentity(uid="clerk:owner-2")
        self.storage.ensure_identity(another)

        with self.assertRaises(AppleTransactionAlreadyLinkedError):
            self.service.verify_and_bind(another, "signed-transaction")

    def test_notification_updates_an_existing_subscription(self):
        self.service.verify_and_bind(self.identity, "signed-transaction")
        expired = VerifiedAppleTransaction(
            original_transaction_id=self.transaction.original_transaction_id,
            transaction_id="2000000000002",
            product_id=self.transaction.product_id,
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            revoked_at=None,
            environment=self.transaction.environment,
        )
        service = AppleBillingService(self.storage, FakeAppleVerifier(expired))

        service.process_notification("signed-notification")

        self.assertFalse(service.entitlement(self.identity).active)


if __name__ == "__main__":
    unittest.main()
