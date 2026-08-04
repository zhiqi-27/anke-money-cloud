import unittest
from unittest.mock import patch

from firebase_admin import auth

from app.auth.firebase import (
    FirebaseTokenVerifier,
    InvalidTokenError,
    extract_bearer_token,
)
from app.config import Settings


def settings() -> Settings:
    return Settings(
        environment="test",
        firebase_project_id="anke-money-test",
        firebase_check_revoked=True,
        cosmos_endpoint="",
        cosmos_database="anke-money-dev",
        cosmos_entities_container="anke_entities",
        cosmos_identities_container="anke_identities",
        cosmos_key="",
        cosmos_expected_account_name="",
        cosmos_allow_smoke_write=False,
    )


class BearerTokenTest(unittest.TestCase):
    def test_extracts_case_insensitive_bearer_token(self):
        self.assertEqual(extract_bearer_token("bearer token-value"), "token-value")

    def test_rejects_missing_or_wrong_scheme(self):
        for authorization in ("", "token", "Basic token", "Bearer   "):
            with self.subTest(authorization=authorization):
                with self.assertRaises(InvalidTokenError):
                    extract_bearer_token(authorization)


class FirebaseTokenVerifierTest(unittest.TestCase):
    def test_returns_uid_from_verified_claims(self):
        verifier = FirebaseTokenVerifier(settings())
        firebase_app = object()
        with (
            patch.object(verifier, "_firebase_app", return_value=firebase_app),
            patch(
                "app.auth.firebase.auth.verify_id_token",
                return_value={"uid": "firebase-user-1"},
            ) as verify,
        ):
            identity = verifier.verify_bearer_token("Bearer signed-token")

        self.assertEqual(identity.uid, "firebase-user-1")
        verify.assert_called_once_with(
            "signed-token",
            app=firebase_app,
            check_revoked=True,
        )

    def test_redacts_underlying_verification_failure(self):
        verifier = FirebaseTokenVerifier(settings())
        with (
            patch.object(verifier, "_firebase_app", return_value=object()),
            patch(
                "app.auth.firebase.auth.verify_id_token",
                side_effect=ValueError("secret token detail"),
            ),
        ):
            with self.assertLogs("app.auth.firebase", level="WARNING") as logs:
                with self.assertRaisesRegex(InvalidTokenError, "Firebase token is invalid"):
                    verifier.verify_bearer_token("Bearer signed-token")

        self.assertNotIn("secret token detail", " ".join(logs.output))

    def test_rejects_expired_and_revoked_firebase_tokens(self):
        failures = (
            auth.ExpiredIdTokenError("expired token detail", ValueError("expired")),
            auth.RevokedIdTokenError("revoked token detail"),
        )
        for failure in failures:
            with self.subTest(error_type=type(failure).__name__):
                verifier = FirebaseTokenVerifier(settings())
                with (
                    patch.object(verifier, "_firebase_app", return_value=object()),
                    patch(
                        "app.auth.firebase.auth.verify_id_token",
                        side_effect=failure,
                    ),
                ):
                    with self.assertRaisesRegex(
                        InvalidTokenError,
                        "Firebase token is invalid",
                    ):
                        verifier.verify_bearer_token("Bearer signed-token")

    def test_rejects_verified_claims_without_uid(self):
        verifier = FirebaseTokenVerifier(settings())
        with (
            patch.object(verifier, "_firebase_app", return_value=object()),
            patch("app.auth.firebase.auth.verify_id_token", return_value={}),
        ):
            with self.assertRaises(InvalidTokenError):
                verifier.verify_bearer_token("Bearer signed-token")


if __name__ == "__main__":
    unittest.main()
