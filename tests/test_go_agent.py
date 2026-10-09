import unittest

import server_rmm


class GoAgentTests(unittest.TestCase):
    def test_source_manifest_is_repository_scoped(self):
        files = server_rmm.go_agent_source_files()
        names = {item["filename"] for item in files}
        self.assertEqual(
            names,
            {
                "go.mod",
                "main.go",
                "pe_loader_windows.go",
                "platform_other.go",
                "platform_windows.go",
            },
        )
        self.assertTrue(all(".." not in item["filename"] for item in files))

    def test_build_rejects_unallowlisted_target(self):
        with self.assertRaises(ValueError):
            server_rmm.build_go_agent("darwin", "amd64")


if __name__ == "__main__":
    unittest.main()
