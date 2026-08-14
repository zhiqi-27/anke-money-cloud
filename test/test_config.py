import os
import unittest
from unittest.mock import patch

from app.config import ConfigurationError, Settings


class SettingsTest(unittest.TestCase):
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
            settings = Settings.from_environment()

        with self.assertRaises(ConfigurationError):
            settings.require_session_auth()
        self.assertFalse(settings.docs_enabled)

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
