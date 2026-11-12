#!/usr/bin/env python3
"""Classifier misreads (docket 1124, 2026-11-12): the classifier was not
shown where a call runs, and its verdict came before its reasoning."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bouncer.classify as classify_mod
import bouncer.providers as providers_mod
from bouncer.providers import _build_prompt, _parse_llm_text


class TestWhereTheCallRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "printwatch"
        (self.root / ".bouncer").mkdir(parents=True)
        (self.root / "tests").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _user(self, cwd, shell_cwd=None, tool_input=None):
        _, user = _build_prompt(
            "Bash", tool_input or {"command": "grep -n main parkify.py"},
            cwd, {}, policy_context="p", shell_cwd=shell_cwd)
        return user

    def test_bash_call_shows_working_directory_and_project_root(self):
        user = self._user(self.root)
        self.assertIn(f"Working directory: {self.root}", user)
        self.assertIn(f"Project root: {self.root}", user)
        self.assertIn("Command: grep -n main parkify.py", user)

    def test_subdirectory_shell_shows_both(self):
        sub = self.root / "tests"
        user = self._user(self.root, shell_cwd=sub)
        self.assertIn(f"Working directory: {sub}", user)
        self.assertIn(f"Project root: {self.root}", user)

    def test_file_tools_get_the_header_too(self):
        user = self._user(self.root, tool_input={"file_path": "x.py"})
        self.assertIn("Working directory:", user)
        self.assertIn("file_path: x.py", user)

    def test_no_project_means_no_root_line(self):
        with tempfile.TemporaryDirectory() as bare:
            with patch("bouncer.config.HOME", Path(bare)):
                user = self._user(Path(bare))
        self.assertIn("Working directory:", user)
        self.assertNotIn("Project root:", user)

    def test_home_paths_also_shown_in_tilde_form(self):
        shown = providers_mod._show_path(Path.home() / "Code" / "x")
        self.assertTrue(shown.endswith("(~/Code/x)"), shown)

    def test_system_prompt_says_relative_paths_are_the_projects(self):
        self.assertIn("resolves against\n  the working directory",
                      providers_mod.DEFAULT_SYSTEM_PROMPT)

    def test_classify_passes_the_shell_cwd(self):
        sub = self.root / "tests"
        seen = {}

        def fake(tool, inp, cwd, config, **kw):
            seen.update(cwd=cwd, **kw)
            return ("ALLOW", "ok", 1, None)

        with patch.object(classify_mod, "project_has_bouncer",
                          return_value=True), \
             patch.object(classify_mod, "_merged_config", return_value={}), \
             patch.object(classify_mod, "call_llm", side_effect=fake):
            classify_mod.classify_call(
                "Bash", {"command": "ls"}, str(sub))
        self.assertEqual(seen["shell_cwd"], sub)


class TestVerdictLast(unittest.TestCase):
    def test_reason_first_reply_parses(self):
        reply = ("FACT: `grep job1307/parkwatch.jsonl`\n"
                 "POLICY: \"reading any job, any file, is always fine\"\n"
                 "REASON: A read of job1307, not job1206.\n"
                 "DECISION: ALLOW")
        self.assertEqual(_parse_llm_text(reply),
                         ("ALLOW", "A read of job1307, not job1206."))

    def test_reply_cut_before_the_verdict_is_unsure(self):
        # The 2026-11-12 shape, reason-first: cut mid-REASON.
        reply = ("FACT: `grep job1307`\nPOLICY: general rules\n"
                 "REASON: The command reads job1307; re-reading the pol")
        self.assertEqual(_parse_llm_text(reply)[0], "UNSURE")

    def test_old_verdict_first_reply_still_parses(self):
        # A custom system_prompt.txt may still ask for the old order.
        self.assertEqual(_parse_llm_text("DECISION: DENY\nREASON: no"),
                         ("DENY", "no"))

    def test_reply_cap_leaves_room(self):
        self.assertGreaterEqual(providers_mod._REPLY_TOKENS, 512)


if __name__ == "__main__":
    unittest.main()
