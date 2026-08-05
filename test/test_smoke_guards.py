import unittest

from app.config import ConfigurationError, Settings
from app.storage.cosmos import CosmosHouseholdStorage
from scripts.cosmos_smoke import validate_smoke_target
from scripts.firebase_auth_smoke import validate_firebase_smoke_target
from scripts.firebase_e2e_smoke import (
    validate_remote_smoke_target,
    validate_synthetic_auth_target,
)


def settings(**overrides) -> Settings:
    values = {
        "environment": "dev",
        "firebase_project_id": "anke-money",
        "firebase_check_revoked": True,
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

    def test_firebase_smoke_rejects_production_and_missing_project(self):
        validate_firebase_smoke_target(settings())
        with self.assertRaises(ConfigurationError):
            validate_firebase_smoke_target(settings(environment="prod"))
        with self.assertRaises(ConfigurationError):
            validate_firebase_smoke_target(settings(firebase_project_id=""))

    def test_synthetic_firebase_smoke_requires_dev_key_and_opt_in(self):
        validate_synthetic_auth_target(
            settings(),
            web_api_key="client-api-key",
            allow_synthetic_user=True,
        )
        rejected = (
            (settings(environment="prod"), "client-api-key", True),
            (settings(), "", True),
            (settings(), "client-api-key", False),
        )
        for value, api_key, allow in rejected:
            with self.subTest(environment=value.environment, allow=allow):
                with self.assertRaises(ConfigurationError):
                    validate_synthetic_auth_target(
                        value,
                        web_api_key=api_key,
                        allow_synthetic_user=allow,
                    )

    def test_remote_firebase_smoke_requires_exact_https_host(self):
        host = "func-anke-money-dev.example.net"
        self.assertEqual(
            validate_remote_smoke_target(f"https://{host}/", host),
            f"https://{host}",
        )
        self.assertEqual(validate_remote_smoke_target("", ""), "")
        rejected = (
            (f"http://{host}", host),
            (f"https://{host}/api/v1/me", host),
            (f"https://{host}", "other.example.net"),
            (f"https://{host}", ""),
        )
        for base_url, expected_host in rejected:
            with self.subTest(base_url=base_url, expected_host=expected_host):
                with self.assertRaises(ConfigurationError):
                    validate_remote_smoke_target(base_url, expected_host)


if __name__ == "__main__":
    unittest.main()
