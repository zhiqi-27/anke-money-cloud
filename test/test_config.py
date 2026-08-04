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
        self.assertFalse(settings.firebase_check_revoked)
        self.assertTrue(settings.docs_enabled)

    def test_production_checks_revocation_by_default(self):
        with patch.dict(os.environ, {"ANKE_ENVIRONMENT": "prod"}, clear=True):
            settings = Settings.from_environment()

        self.assertTrue(settings.firebase_check_revoked)
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


if __name__ == "__main__":
    unittest.main()
