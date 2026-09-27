#!/usr/bin/env python3
"""Offline tests for scripts/release-notes.py (the tag -> Release derivation)."""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import subprocess
import unittest

HERE = Path(__file__).resolve().parent
_loader = SourceFileLoader('release_notes', str(HERE / 'release-notes.py'))
_spec = importlib.util.spec_from_loader('release_notes', _loader)
rn = importlib.util.module_from_spec(_spec)
_loader.exec_module(rn)

TAGS = ['v1.9.0', 'v1.10.0', 'v1.36.0', 'v1.37.0', 'v1.36.1']


class TitleTests(unittest.TestCase):
    def test_release_colon_style(self):
        self.assertEqual(rn.derive_title('v1.37.0', 'release: v1.37.0 — Claude subscription lanes'),
                         'v1.37.0 — Claude subscription lanes')

    def test_capital_release_style(self):
        self.assertEqual(rn.derive_title('v1.33.0', 'Release v1.33.0: ferry migrate promotes a client into a host'),
                         'v1.33.0 — ferry migrate promotes a client into a host')

    def test_bare_tag_colon_style(self):
        self.assertEqual(rn.derive_title('v1.20.0', 'v1.20.0: Claude Code runs on the ferry backend'),
                         'v1.20.0 — Claude Code runs on the ferry backend')

    def test_subject_for_another_tag_is_not_used(self):
        self.assertEqual(rn.derive_title('v1.37.1', 'release: v1.37.0 — old'), 'v1.37.1')

    def test_falls_back_to_release_doc_heading(self):
        notes = '# llm-ferry v1.35.0 — a dedicated schematron door\n\nbody\n'
        self.assertEqual(rn.derive_title('v1.35.0', 'feat(up): --schematron — the door', notes),
                         'v1.35.0 — a dedicated schematron door')

    def test_falls_back_to_tag_name(self):
        self.assertEqual(rn.derive_title('v1.36.0', 'feat(schematron): run the lane on the GPU'), 'v1.36.0')
        self.assertEqual(rn.derive_title('v1.36.0', '', '# Unrelated heading\n'), 'v1.36.0')


class NotesTests(unittest.TestCase):
    def test_release_doc_wins(self):
        self.assertEqual(rn.derive_notes('v1.37.0', '# heading\n\ntext\n\n', 'body', ['s']), '# heading\n\ntext\n')

    def test_commit_body_with_trailers_stripped(self):
        body = 'What changed.\n\nWhy.\n\nCo-Authored-By: X <x@y>\nClaude-Session-Id: abc-123\n'
        self.assertEqual(rn.derive_notes('v1.33.0', '', body, ['s']), 'What changed.\n\nWhy.\n')

    def test_prose_paragraph_is_not_mistaken_for_trailers(self):
        body = 'Tests: 5 new suites, all green.\nSecond line of prose here.'
        self.assertEqual(rn.strip_trailers(body), body)

    def test_git_log_fallback(self):
        notes = rn.derive_notes('v1.37.0', '', '\n', ['feat: a', 'fix: b'], 'v1.36.1')
        self.assertEqual(notes, 'Changes since v1.36.1:\n\n- feat: a\n- fix: b\n')

    def test_last_resort(self):
        self.assertEqual(rn.derive_notes('v1.0.0', '', '', []), 'Release v1.0.0.\n')


class SemverTests(unittest.TestCase):
    def test_latest_only_for_highest(self):
        self.assertTrue(rn.is_latest('v1.37.0', TAGS))
        self.assertFalse(rn.is_latest('v1.36.1', TAGS))
        self.assertFalse(rn.is_latest('v1.10.0', TAGS))  # numeric, not lexical: 1.10 < 1.37

    def test_prerelease_is_never_latest(self):
        self.assertFalse(rn.is_latest('v2.0.0-rc1', TAGS + ['v2.0.0-rc1']))

    def test_non_semver_is_never_latest(self):
        self.assertFalse(rn.is_latest('nightly', TAGS))

    def test_previous_tag_is_numeric(self):
        self.assertEqual(rn.previous_tag('v1.37.0', TAGS), 'v1.36.1')
        self.assertEqual(rn.previous_tag('v1.10.0', TAGS), 'v1.9.0')
        self.assertIsNone(rn.previous_tag('v1.9.0', TAGS))


class RealRepoTests(unittest.TestCase):
    """Runs the CLI against this checkout's real tags, when they are present."""

    def run_cli(self, *args):
        return subprocess.run(['python3', str(HERE / 'release-notes.py'), *args],
                              capture_output=True, text=True)

    def setUp(self):
        probe = subprocess.run(['git', 'rev-parse', '--verify', '--quiet', 'v1.37.0^{commit}'],
                               cwd=HERE, capture_output=True, text=True)
        if probe.returncode != 0:
            self.skipTest('tag v1.37.0 not in this checkout')

    def test_known_titles(self):
        self.assertEqual(self.run_cli('title', 'v1.37.0').stdout.strip(), 'v1.37.0 — Claude subscription lanes')
        self.assertEqual(self.run_cli('title', 'v1.33.0').stdout.strip(),
                         'v1.33.0 — ferry migrate promotes a client into a host')

    def test_usage_error(self):
        self.assertEqual(self.run_cli('bogus').returncode, 2)


if __name__ == '__main__':
    unittest.main()
