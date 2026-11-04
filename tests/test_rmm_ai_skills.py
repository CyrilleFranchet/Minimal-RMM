"""Unit coverage for server-side AI skill selection rules."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import rmm_ai_skills


class AiSkillsTests(unittest.TestCase):
    """Verify default and always-enabled server skills."""

    def test_split_skill_selection_and_disablements_survive_rename(self):
        directory = str(Path(__file__).resolve().parents[1] / 'ai-skills')
        selected = rmm_ai_skills.resolve_ai_skills(['ad-collection-analysis'], [directory])
        self.assertEqual({row['id'] for row in selected}, {
            'operator-boundaries', 'task-completion-report',
            'bloodhound-ce-python-collection', 'bloodhound-analysis'})
        flags = {'ad-collection-analysis': False, 'bloodhound-analysis': True}
        selected = rmm_ai_skills.resolve_ai_skills(['ad-collection-analysis'], [directory], flags)
        self.assertNotIn('bloodhound-ce-python-collection', {row['id'] for row in selected})
        self.assertIn('bloodhound-analysis', {row['id'] for row in selected})
        rows = {row['id']: row for row in rmm_ai_skills.list_ai_skills([directory], flags)}
        self.assertFalse(rows['bloodhound-ce-python-collection']['enabled'])
        self.assertTrue(rows['bloodhound-analysis']['enabled'])

    def test_adapted_skills_remain_discoverable_without_eager_bodies(self):
        from rmm_ai_skill_context import prepare_skill_context
        directory = str(Path(__file__).resolve().parents[1] / 'ai-skills')
        documents = rmm_ai_skills.resolve_ai_skills(None, [directory])
        ids = {row['id'] for row in documents}
        expected = {'offensive-netexec', 'offensive-windows-privesc', 'offensive-linux-privesc'} | {'web-' + name for name in (
            'sqli', 'xss', 'ssrf',
            'ssti', 'xxe', 'idor', 'file-upload', 'rce', 'deserialization',
            'race-condition', 'request-smuggling', 'open-redirect',
            'parameter-pollution', 'graphql', 'waf-bypass', 'business-logic')}
        self.assertTrue(expected <= ids)
        self.assertNotIn('ad-collection-analysis', ids)
        core, catalog, _, _ = prepare_skill_context(documents, [], AsyncMock())
        self.assertEqual({row['id'] for row in core}, {'operator-boundaries', 'task-completion-report'})
        for row in documents:
            if row['id'] in expected:
                self.assertFalse(row['always'])
                self.assertFalse(row['default'])
                self.assertIn(row['id'], catalog)
                self.assertNotIn(row['body'], catalog)

    def test_web_renames_preserve_selection_and_explicit_disablements(self):
        directory = str(Path(__file__).resolve().parents[1] / 'ai-skills')
        for topic in rmm_ai_skills.WEB_SKILL_TOPICS:
            old, new = 'offensive-' + topic, 'web-' + topic
            with self.subTest(skill=new):
                selected = rmm_ai_skills.resolve_ai_skills([old], [directory])
                self.assertIn(new, {row['id'] for row in selected})
                self.assertNotIn(old, {row['id'] for row in selected})
                disabled = rmm_ai_skills.resolve_ai_skills([old], [directory], {old: False})
                self.assertNotIn(new, {row['id'] for row in disabled})
                enabled = rmm_ai_skills.resolve_ai_skills([old], [directory], {old: False, new: True})
                self.assertIn(new, {row['id'] for row in enabled})

    def test_workspace_uses_same_legacy_selection_and_override_rules(self):
        from workspace_service import WorkspaceStore
        directory = str(Path(__file__).resolve().parents[1] / 'ai-skills')
        store = WorkspaceStore.__new__(WorkspaceStore)
        store.skill_directories = lambda: [directory]
        store.skill_overrides = lambda: {'ad-collection-analysis': False, 'bloodhound-analysis': True}
        selected = store.selected_skill_documents(['ad-collection-analysis'])
        self.assertEqual({row['id'] for row in selected}, {
            'operator-boundaries', 'task-completion-report', 'bloodhound-analysis'})

    def test_stale_overlay_does_not_restore_combined_instructions(self):
        directory = str(Path(__file__).resolve().parents[1] / 'ai-skills')
        with tempfile.TemporaryDirectory() as overlay:
            Path(overlay, 'ad-collection-analysis.md').write_text(
                '---\nid: ad-collection-analysis\nalways: true\n---\nSTALE_COMBINED_BODY')
            documents = rmm_ai_skills.resolve_ai_skills(None, [directory, overlay])
            metadata = rmm_ai_skills.list_ai_skills([directory, overlay])
        self.assertNotIn('ad-collection-analysis', {row['id'] for row in metadata})
        self.assertFalse(any('STALE_COMBINED_BODY' in row['body'] for row in documents))

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
