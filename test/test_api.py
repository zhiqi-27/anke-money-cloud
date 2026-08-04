import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.auth import AuthenticatedIdentity, InvalidTokenError
from app.config import ConfigurationError
from app.main import fastapi_app


class FakeTokenVerifier:
    def verify_bearer_token(self, authorization: str) -> AuthenticatedIdentity:
        if authorization != "Bearer valid-test-token":
            raise InvalidTokenError("invalid")
        return AuthenticatedIdentity(uid="firebase-user-1")


class MisconfiguredTokenVerifier:
    def verify_bearer_token(self, authorization: str) -> AuthenticatedIdentity:
        raise ConfigurationError("missing secret detail")


class ApiContractTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(fastapi_app, raise_server_exceptions=False)

    def test_ping_is_public_and_non_sensitive(self):
        response = self.client.get("/ping")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertNotIn("cosmos", response.text.lower())
        self.assertNotIn("firebase", response.text.lower())

    def test_openapi_includes_health_and_protected_identity_route(self):
        response = self.client.get("/openapi.json")

        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        self.assertIn("/ping", paths)
        self.assertIn("/api/v1/me", paths)
        identity_operation = paths["/api/v1/me"]["get"]
        self.assertEqual(identity_operation["security"], [{"HTTPBearer": []}])
        self.assertIn("HTTPBearer", response.json()["components"]["securitySchemes"])

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
        self.assertEqual(response.json(), {"uid": "firebase-user-1"})

    def test_azure_functions_entry_imports_without_credentials(self):
        import function_app

        self.assertIsNotNone(function_app.app)


if __name__ == "__main__":
    unittest.main()
