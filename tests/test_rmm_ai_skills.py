"""Unit coverage for server-side AI skill selection rules."""

import os
import tempfile
import unittest
from unittest.mock import patch

import rmm_ai_skills


class AiSkillsTests(unittest.TestCase):
    """Verify default and always-enabled server skills."""

    def test_always_skill_is_injected_when_no_skills_are_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "always.md"), "w", encoding="utf-8") as handle:
                handle.write("---\nid: always\nalways: true\n---\nAlways instructions.")
            with open(os.path.join(directory, "default.md"), "w", encoding="utf-8") as handle:
                handle.write("---\nid: default\ndefault: true\n---\nDefault instructions.")
            with patch.dict(os.environ, {"RMM_AI_SKILLS_DIR": directory}):
                selected = rmm_ai_skills.resolve_ai_skills([])
                defaults = rmm_ai_skills.resolve_ai_skills(None)
                listed = rmm_ai_skills.list_ai_skills()

        self.assertEqual([skill["id"] for skill in selected], ["always"])
        self.assertEqual({skill["id"] for skill in defaults}, {"always", "default"})
        self.assertTrue(next(skill for skill in listed if skill["id"] == "always")["always"])
