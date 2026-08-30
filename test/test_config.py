import os
import unittest
from unittest.mock import patch

from app.config import ConfigurationError, Settings


class SettingsTest(unittest.TestCase):
    @staticmethod
    def production_environment() -> dict[str, str]:
        return {
            "ANKE_ENVIRONMENT": "prod",
            "CLERK_JWKS_URL": "https://clerk-prod.example/.well-known/jwks.json",
            "CLERK_ISSUER": "https://clerk-prod.example",
            "CLERK_SECRET_KEY": "sk_live_synthetic",
            "ANKE_SESSION_SIGNING_SECRET": "s" * 48,
            "ANKE_COSMOS_ENDPOINT": "https://cosmos-anke-money-prod.documents.azure.com:443/",
            "ANKE_COSMOS_DATABASE": "anke_money_prod",
            "ANKE_COSMOS_ENTITIES_CONTAINER": "anke_entities",
            "ANKE_COSMOS_IDENTITIES_CONTAINER": "anke_identities",
            "ANKE_COSMOS_EXPECTED_ACCOUNT_NAME": "cosmos-anke-money-prod",
            "ANKE_COSMOS_ALLOW_SMOKE_WRITE": "false",
            "ANKE_APNS_TEAM_ID": "TEAMID1234",
            "ANKE_APNS_KEY_ID": "KEYID1234",
            "ANKE_APNS_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----synthetic-----END PRIVATE KEY-----",
            "ANKE_APNS_TOPIC": "app.ankemoney.ios",
            "ANKE_APPLE_APP_ID": "6800547254",
            "ANKE_APPLE_ROOT_CERTIFICATES_BASE64": "YQ==",
        }

    def test_defaults_are_local_and_do_not_enable_smoke(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(settings.environment, "local")
        self.assertFalse(settings.cosmos_allow_smoke_write)
        self.assertEqual(settings.session_ttl_seconds, 2_592_000)
        self.assertTrue(settings.docs_enabled)
        self.assertEqual(settings.agent_requests_per_minute, 120)
        self.assertEqual(settings.agent_failed_auth_threshold, 5)

    def test_production_keeps_auth_configuration_explicit(self):
        with patch.dict(os.environ, {"ANKE_ENVIRONMENT": "prod"}, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "CLERK_JWKS_URL"):
                Settings.from_environment()

    def test_production_accepts_complete_managed_identity_configuration(self):
        with patch.dict(os.environ, self.production_environment(), clear=True):
            settings = Settings.from_environment()

        self.assertEqual(settings.environment, "prod")
        self.assertFalse(settings.docs_enabled)
        self.assertFalse(settings.cosmos_key)
        self.assertTrue(settings.apns_configured)

    def test_production_allows_clerk_default_domain_with_live_key(self):
        environment = self.production_environment()
        environment["CLERK_ISSUER"] = "https://production.clerk.accounts.dev"
        environment["CLERK_JWKS_URL"] = (
            "https://production.clerk.accounts.dev/.well-known/jwks.json"
        )
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_environment()

        self.assertEqual(settings.clerk_issuer, "https://production.clerk.accounts.dev")

    def test_production_rejects_test_clerk_key(self):
        environment = self.production_environment()
        environment["CLERK_SECRET_KEY"] = "sk_test_synthetic"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "live Production"):
                Settings.from_environment()

    def test_production_rejects_cosmos_key_and_smoke_write(self):
        environment = self.production_environment()
        environment["ANKE_COSMOS_KEY"] = "should-not-be-used"
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "ANKE_COSMOS_KEY"):
                Settings.from_environment()

    def test_rejects_unknown_environment(self):
        with patch.dict(os.environ, {"ANKE_ENVIRONMENT": "preview"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_environment()

    def test_cosmos_requires_endpoint(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_environment()

        with self.assertRaisesRegex(ConfigurationError, "ANKE_COSMOS_ENDPOINT"):
            settings.require_cosmos()

    def test_agent_security_thresholds_are_bounded(self):
        with patch.dict(os.environ, {
            "ANKE_AGENT_REQUESTS_PER_MINUTE": "9",
            "ANKE_AGENT_FAILED_AUTH_THRESHOLD": "2",
        }, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_environment()

        with patch.dict(os.environ, {
            "ANKE_AGENT_REQUESTS_PER_MINUTE": "240",
            "ANKE_AGENT_FAILED_AUTH_THRESHOLD": "8",
        }, clear=True):
            settings = Settings.from_environment()
        self.assertEqual(settings.agent_requests_per_minute, 240)
        self.assertEqual(settings.agent_failed_auth_threshold, 8)


if __name__ == "__main__":
    unittest.main()
