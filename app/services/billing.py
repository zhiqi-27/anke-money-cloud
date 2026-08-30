from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import (
    SignedDataVerifier,
    VerificationException,
)

from app.auth import AuthenticatedIdentity
from app.config import ConfigurationError, Settings
from app.models import ProEntitlementView
from app.services.entitlements import EntitlementResolver
from app.storage.protocols import HouseholdStorage


class InvalidAppleTransactionError(RuntimeError):
    pass


class AppleTransactionAlreadyLinkedError(RuntimeError):
    pass


class ProEntitlementRequiredError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedAppleTransaction:
    original_transaction_id: str
    transaction_id: str
    product_id: str
    expires_at: datetime | None
    revoked_at: datetime | None
    environment: str

    @property
    def active(self) -> bool:
        now = datetime.now(UTC)
        return self.revoked_at is None and (
            self.expires_at is None or self.expires_at > now
        )


class AppleTransactionVerifier(Protocol):
    def verify_transaction(self, signed_transaction: str) -> VerifiedAppleTransaction: ...

    def transaction_from_notification(self, signed_payload: str) -> VerifiedAppleTransaction | None: ...


class AppleSignedDataVerifier:
    def __init__(self, settings: Settings):
        if not settings.apple_root_certificates_base64:
            raise ConfigurationError("ANKE_APPLE_ROOT_CERTIFICATES_BASE64 is required")
        try:
            roots = [
                base64.b64decode(value, validate=True)
                for value in settings.apple_root_certificates_base64
            ]
        except ValueError as exc:
            raise ConfigurationError("Apple root certificates must be base64 DER") from exc
        if not roots or any(not root for root in roots):
            raise ConfigurationError("Apple root certificates must not be empty")

        self._settings = settings
        self._verifiers: list[SignedDataVerifier] = []
        if settings.environment != "prod":
            self._verifiers.append(SignedDataVerifier(
                roots, True, Environment.SANDBOX, settings.apple_bundle_id
            ))
        self._verifiers.append(SignedDataVerifier(
            roots,
            True,
            Environment.PRODUCTION,
            settings.apple_bundle_id,
            settings.apple_app_id or None,
        ))

    def verify_transaction(self, signed_transaction: str) -> VerifiedAppleTransaction:
        last_error: Exception | None = None
        for verifier in self._verifiers:
            try:
                return self._convert(verifier.verify_and_decode_signed_transaction(signed_transaction))
            except VerificationException as exc:
                last_error = exc
        raise InvalidAppleTransactionError("Apple transaction verification failed") from last_error

    def transaction_from_notification(self, signed_payload: str) -> VerifiedAppleTransaction | None:
        last_error: Exception | None = None
        for verifier in self._verifiers:
            try:
                payload = verifier.verify_and_decode_notification(signed_payload)
                signed_transaction = payload.data.signedTransactionInfo if payload.data else None
                if not signed_transaction:
                    return None
                return self._convert(
                    verifier.verify_and_decode_signed_transaction(signed_transaction)
                )
            except VerificationException as exc:
                last_error = exc
        raise InvalidAppleTransactionError("Apple notification verification failed") from last_error

    def _convert(self, payload) -> VerifiedAppleTransaction:
        required = (
            payload.originalTransactionId,
            payload.transactionId,
            payload.productId,
        )
        if not all(isinstance(value, str) and value for value in required):
            raise InvalidAppleTransactionError("Apple transaction is missing required fields")
        if payload.productId not in self._settings.apple_product_ids:
            raise InvalidAppleTransactionError("Apple product is not an Anke Pro product")
        return VerifiedAppleTransaction(
            original_transaction_id=payload.originalTransactionId,
            transaction_id=payload.transactionId,
            product_id=payload.productId,
            expires_at=self._milliseconds(payload.expiresDate),
            revoked_at=self._milliseconds(payload.revocationDate),
            environment=(payload.environment.value if payload.environment else "Unknown"),
        )

    @staticmethod
    def _milliseconds(value: int | None) -> datetime | None:
        return datetime.fromtimestamp(value / 1000, UTC) if value is not None else None


class AppleBillingService:
    def __init__(self, storage: HouseholdStorage, verifier: AppleTransactionVerifier):
        self._storage = storage
        self._verifier = verifier
        self._entitlements = EntitlementResolver(storage)

    def verify_and_bind(
        self,
        identity: AuthenticatedIdentity,
        signed_transaction: str,
    ) -> ProEntitlementView:
        household_id = self._storage.household_for_uid(identity.uid)
        if household_id is None:
            raise ProEntitlementRequiredError("Anke identity is not ready")
        transaction = self._verifier.verify_transaction(signed_transaction)
        existing = self._storage.subscription_by_original_transaction_id(
            transaction.original_transaction_id
        )
        if existing is not None and existing.get("uid") != identity.uid:
            raise AppleTransactionAlreadyLinkedError(
                "This Apple subscription is already linked to another Anke account"
            )
        document = self._document(transaction, identity.uid, household_id, existing)
        self._storage.upsert_subscription_entitlement(document)
        return self.entitlement(identity)

    def entitlement(self, identity: AuthenticatedIdentity) -> ProEntitlementView:
        effective = self._entitlements.resolve(identity.uid)
        apple = max(
            effective.apple_documents,
            key=lambda value: value.get("updatedAt", ""),
            default=None,
        )
        return ProEntitlementView(
            active=effective.active,
            sources=list(effective.sources),
            product_id=apple.get("productId") if apple else None,
            original_transaction_id=apple.get("originalTransactionId") if apple else None,
            expires_at=effective.expires_at,
            environment=apple.get("environment") if apple else None,
        )

    def process_notification(self, signed_payload: str) -> None:
        transaction = self._verifier.transaction_from_notification(signed_payload)
        if transaction is None:
            return
        existing = self._storage.subscription_by_original_transaction_id(
            transaction.original_transaction_id
        )
        if existing is None:
            return
        document = self._document(
            transaction,
            existing["uid"],
            existing["householdId"],
            existing,
        )
        self._storage.upsert_subscription_entitlement(document)

    @staticmethod
    def _document(transaction, uid: str, household_id: str, existing: dict | None) -> dict:
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return {
            "id": f"apple-subscription:{transaction.original_transaction_id}",
            "entityType": "appleSubscription",
            "uid": uid,
            "householdId": household_id,
            "originalTransactionId": transaction.original_transaction_id,
            "transactionId": transaction.transaction_id,
            "productId": transaction.product_id,
            "active": transaction.active,
            "expiresAt": transaction.expires_at.isoformat().replace("+00:00", "Z") if transaction.expires_at else None,
            "revokedAt": transaction.revoked_at.isoformat().replace("+00:00", "Z") if transaction.revoked_at else None,
            "environment": transaction.environment,
            "createdAt": (existing or {}).get("createdAt", now),
            "updatedAt": now,
        }
