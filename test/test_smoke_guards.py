import unittest

from app.config import ConfigurationError, Settings
from app.storage.cosmos import CosmosHouseholdStorage
from scripts.cosmos_smoke import validate_smoke_target


def settings(**overrides) -> Settings:
    values = {
        "environment": "dev",
        "clerk_jwks_url": "https://clerk.example/.well-known/jwks.json",
        "clerk_issuer": "https://clerk.example",
        "clerk_audience": "",
        "clerk_secret_key": "sk_test_" + "s" * 32,
        "clerk_backend_api_url": "https://api.clerk.com",
        "session_signing_secret": "s" * 32,
        "session_ttl_seconds": 2_592_000,
        "cosmos_endpoint": "https://cosmos-anke-money-dev-zq01.documents.azure.com:443/",
        "cosmos_database": "anke_money_dev",
        "cosmos_entities_container": "anke_entities",
        "cosmos_identities_container": "anke_identities",
        "cosmos_key": "",
        "cosmos_expected_account_name": "cosmos-anke-money-dev-zq01",
        "cosmos_allow_smoke_write": True,
    }
    values.update(overrides)
    return Settings(**values)


class SmokeGuardTest(unittest.TestCase):
    def test_accepts_exact_development_target(self):
        value = settings()
        validate_smoke_target(value, CosmosHouseholdStorage(value, container=object()))

    def test_rejects_prod_disabled_and_account_mismatch(self):
        cases = [
            settings(environment="prod"),
            settings(cosmos_allow_smoke_write=False),
            settings(cosmos_expected_account_name="other-account"),
        ]
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    validate_smoke_target(
                        value,
                        CosmosHouseholdStorage(value, container=object()),
                    )

    def test_session_auth_rejects_short_signing_secret(self):
        with self.assertRaises(ConfigurationError):
            settings(session_signing_secret="too-short").require_session_auth()

    def test_clerk_auth_requires_https_jwks(self):
        with self.assertRaises(ConfigurationError):
            settings(clerk_jwks_url="http://example.test/keys").require_clerk_auth()


if __name__ == "__main__":
    unittest.main()
