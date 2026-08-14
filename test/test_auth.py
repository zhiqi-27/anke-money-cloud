import json
import unittest
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.auth import (
    AnkeSessionTokenIssuer,
    AnkeSessionTokenVerifier,
    AuthenticatedIdentity,
    ClerkTokenVerifier,
    InvalidClerkCredentialError,
    InvalidTokenError,
    extract_bearer_token,
)
from app.config import Settings


def settings(**overrides) -> Settings:
    values = {
        "environment": "test",
        "clerk_jwks_url": "https://clerk.example/.well-known/jwks.json",
        "clerk_issuer": "https://clerk.example",
        "clerk_audience": "",
        "clerk_secret_key": "sk_test_" + "s" * 32,
        "clerk_backend_api_url": "https://api.clerk.com",
        "session_signing_secret": "s" * 32,
        "session_ttl_seconds": 3600,
        "cosmos_endpoint": "",
        "cosmos_database": "anke-money-dev",
        "cosmos_entities_container": "anke_entities",
        "cosmos_identities_container": "anke_identities",
        "cosmos_key": "",
        "cosmos_expected_account_name": "",
        "cosmos_allow_smoke_write": False,
    }
    values.update(overrides)
    return Settings(**values)


class BearerTokenTest(unittest.TestCase):
    def test_extracts_case_insensitive_bearer_token(self):
        self.assertEqual(extract_bearer_token("bearer token-value"), "token-value")

    def test_rejects_missing_or_wrong_scheme(self):
        for authorization in ("", "token", "Basic token", "Bearer   "):
            with self.subTest(authorization=authorization):
                with self.assertRaises(InvalidTokenError):
                    extract_bearer_token(authorization)


class AnkeSessionTokenTest(unittest.TestCase):
    def test_round_trip_preserves_anke_identity(self):
        now = datetime.now(UTC)
        identity = AuthenticatedIdentity(
            uid="apple:subject-1",
            provider="apple",
            provider_subject="subject-1",
            display_name="Anke User",
            email="relay@example.appleid.com",
        )
        issuer = AnkeSessionTokenIssuer(settings())
        token, expires_at = issuer.issue(identity, now=now)
        verified = AnkeSessionTokenVerifier(settings()).verify_bearer_token(
            f"Bearer {token}"
        )

        self.assertEqual(verified, identity)
        self.assertEqual(expires_at, now + timedelta(seconds=3600))

    def test_rejects_expired_session(self):
        now = datetime(2026, 8, 13, tzinfo=UTC)
        issuer = AnkeSessionTokenIssuer(settings(session_ttl_seconds=300))
        token, _ = issuer.issue(
            AuthenticatedIdentity(
                uid="apple:subject-1",
                provider_subject="subject-1",
            ),
            now=now - timedelta(hours=1),
        )

        with self.assertRaises(InvalidTokenError):
            AnkeSessionTokenVerifier(settings(session_ttl_seconds=300)).verify_bearer_token(
                f"Bearer {token}"
            )


class ClerkTokenVerifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(RSAAlgorithm.to_jwk(cls.private_key.public_key()))
        cls.jwk["kid"] = "test-key"

    def _token(self, subject="clerk-subject"):
        now = datetime.now(UTC)
        return jwt.encode(
            {
                "iss": "https://clerk.example",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
                "sub": subject,
                "email_address": "user@example.com",
                "first_name": "Anke",
                "last_name": "User",
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )

    def test_verifies_clerk_claims_and_namespaces_subject(self):
        verifier = ClerkTokenVerifier(
            settings(),
            jwks_loader=lambda: {"keys": [self.jwk]},
        )
        identity = verifier.verify(self._token())

        self.assertEqual(identity.uid, "clerk:clerk-subject")
        self.assertEqual(identity.provider, "clerk")
        self.assertEqual(identity.email, "user@example.com")
        self.assertEqual(identity.display_name, "Anke User")

    def test_rejects_wrong_issuer(self):
        verifier = ClerkTokenVerifier(
            settings(),
            jwks_loader=lambda: {"keys": [self.jwk]},
        )
        token = jwt.encode(
            {
                "iss": "https://other.example",
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
                "sub": "clerk-subject",
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        with self.assertRaises(InvalidClerkCredentialError):
            verifier.verify(token)


if __name__ == "__main__":
    unittest.main()
