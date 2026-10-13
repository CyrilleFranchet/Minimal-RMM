"""Unit coverage for rclone profile payload helpers."""

import unittest
from unittest.mock import patch

from rmm_rclone import (
    build_exfil_command_from_payload,
    build_plugin_exfil_job,
    profile_to_plugin_settings,
)


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

    def test_builds_plugin_job_from_workspace_profile(self):
        with patch.dict(
            "os.environ",
            {
                "RMM_RCLONE_PROFILES": '{"local":{"type":"s3","access_key_id":"a","folder":"loot"}}',
                "RMM_RCLONE_PROFILES_FILE": "",
            },
            clear=False,
        ):
            job = build_plugin_exfil_job("C:/Temp/report.txt", "local")

        self.assertEqual(job["source"], "C:/Temp/report.txt")
        self.assertEqual(job["target"], "loot/report.txt")
        self.assertEqual(job["settings"], {"access_key_id": "a"})

    def test_serializes_a_workspace_built_binary_payload(self):
        command = build_exfil_command_from_payload(
            {"local_path": "C:/Temp/report.txt", "env": {"RCLONE_CONFIG_RMM_TYPE": "s3"}}
        )
        self.assertTrue(command.startswith("__EXFIL__\n{"))
        self.assertIn('"local_path":"C:/Temp/report.txt"', command)


if __name__ == "__main__":
    unittest.main()
