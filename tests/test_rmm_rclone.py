"""Unit coverage for rclone profile payload helpers."""

import unittest

from rmm_rclone import profile_to_plugin_settings


class PluginSettingsTests(unittest.TestCase):
    """Verify plugin jobs use rclone backend option names."""

    def test_maps_profile_aliases_to_backend_names(self):
        profile = {
            "type": "mega",
            "user": "lab@example.com",
            "password": "plain-password",
            "folder": "RMM",
            "pass_obscured": True,
        }
        self.assertEqual(
            profile_to_plugin_settings(profile),
            {"user": "lab@example.com", "pass": "plain-password"},
        )

    def test_preserves_backend_option_names(self):
        profile = {
            "type": "s3",
            "access_key_id": "access",
            "secret_access_key": "secret",
            "region": "us-east-1",
        }
        self.assertEqual(
            profile_to_plugin_settings(profile),
            {
                "access_key_id": "access",
                "secret_access_key": "secret",
                "region": "us-east-1",
            },
        )


if __name__ == "__main__":
    unittest.main()
