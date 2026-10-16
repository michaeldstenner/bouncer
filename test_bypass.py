#!/usr/bin/env python3
"""`enabled: bypass` — bouncer bypassed, as if the hook were not installed.

Not `enabled: false` (nor `off` or `no`, which YAML reads as false), all
of which turn off the classifier and keep the hard guard. Bypass is
nothing at all: no guard, no classifier, no log line, no state, no
output. Only a human sets it (agents can write no bouncer config), and it
is never silent: a SessionStart line, and `bypass` in `bouncer profile` /
`bouncer status`.

Every config here is a fixture in a temp dir.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import bouncer.classify as classify_mod
import bouncer.commands.classify as classify_cmd_mod
import bouncer.config as cfg
import bouncer.escalation_cache as escalation_mod
import bouncer.escalation_grant as grant_mod
import bouncer.profile as profile_mod
from bouncer.commands.log import cmd_log
from bouncer.commands.profile import cmd_profile
from bouncer.commands.session_start import cmd_session_start
from bouncer.commands.status import cmd_status

_GUARD = "cannot be modified"


class _Fixture(unittest.TestCase):
    """A user dir and two projects, A and B, all in a temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.user = root / "user"
        self.user.mkdir()
        self.a = root / "a"
        self.b = root / "b"
        for project in (self.a, self.b):
            (project / ".bouncer").mkdir(parents=True)
            (project / ".bouncer" / "config.yaml").write_text(
                "enabled: true\ntools: all\n")
            (project / ".bouncer" / "policy.md").write_text("old")
        self.patches = [
            patch.object(cfg, "USER_CONFIG_FILE", self.user / "config.yaml"),
            patch.object(cfg, "USER_POLICY_FILE", self.user / "policy.md"),
            patch.object(cfg, "USER_LOG_FILE", self.user / "log.jsonl"),
            patch.object(profile_mod, "PROFILE_DIR", root / "profile"),
            patch.object(grant_mod, "GRANT_DIR", root / "esc"),
            patch.object(escalation_mod, "ESCALATION_DIR", root / "esc"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.root = root

    def set_project(self, project, text, name="config.yaml"):
        (project / ".bouncer" / name).write_text(text)

    def set_user(self, text):
        (self.user / "config.yaml").write_text(text)

    def hook(self, command, cwd):
        """Run the real hook entry. Returns (stdout, stderr, exit code, llm
        calls, tool-catalog writes)."""
        calls, catalog = [], []
        payload = {"tool_name": "Bash", "tool_input": {"command": command},
                   "cwd": str(cwd), "session_id": "s1",
                   "hook_event_name": "PreToolUse"}
        out, err, code = io.StringIO(), io.StringIO(), 0

        class _Args:
            hook = True
            format = "json"

        with patch.object(classify_mod, "call_llm",
                          side_effect=lambda *a: calls.append(a) or
                          ("ALLOW", "ok", None, None)), \
                patch.object(classify_cmd_mod, "record_tool_observation",
                             side_effect=lambda *a: catalog.append(a)), \
                patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                classify_cmd_mod.cmd_classify(_Args())
            except SystemExit as exc:
                code = exc.code or 0
        return out.getvalue(), err.getvalue(), code, calls, catalog

    def protected_write(self, project):
        return f"echo 'allow all' > {project}/.bouncer/policy.md"

    def logs(self):
        return [p for p in (self.user / "log.jsonl",
                            self.a / ".bouncer" / "log.jsonl",
                            self.b / ".bouncer" / "log.jsonl") if p.exists()]

    def assertNothingRan(self, result):
        out, err, code, calls, catalog = result
        self.assertEqual((out, err, code), ("", "", 0))
        self.assertEqual(calls, [], "LLM called while bypassed")
        self.assertEqual(catalog, [], "state written while bypassed")
        self.assertEqual(self.logs(), [], "log written while bypassed")

    def assertGuarded(self, result):
        out, err, code, _, _ = result
        self.assertEqual(code, 2)
        self.assertIn(_GUARD, err)


class TestProjectBypass(_Fixture):

    def test_bypass_is_no_hook_at_all_not_even_the_guard(self):
        self.set_project(self.a, "enabled: bypass\n")
        self.assertNothingRan(self.hook(self.protected_write(self.a), self.a))
        self.assertNothingRan(self.hook("git push", self.a))

    def test_spellings_of_bypass(self):
        for text in ('enabled: "bypass"\n', "enabled: 'bypass'\n",
                     "enabled: BYPASS\n",
                     "enabled: bypass   # Michael, 2026-10-15\n",
                     "tools: all\nenabled: Bypass\n"):
            with self.subTest(text=text):
                self.set_project(self.a, text)
                self.assertNothingRan(self.hook("git push", self.a))

    def test_local_config_can_bypass_a_project(self):
        self.set_project(self.a, "enabled: bypass\n", name="config.local.yaml")
        self.assertNothingRan(self.hook("git push", self.a))

    def test_off_false_and_no_keep_the_guard(self):
        # Plain YAML: `off`, `no` and `false` are all false — the classifier
        # is off, the hard guard is on. No reading around the spec.
        for text in ("enabled: false\n", "enabled: no\n", "enabled: off\n",
                     "enabled: OFF\n", "enabled: Off\n"):
            with self.subTest(text=text):
                self.set_project(self.a, text)
                self.assertGuarded(self.hook(self.protected_write(self.a),
                                             self.a))

    def test_lookalikes_are_not_bypass(self):
        for text in ("# enabled: bypass\nenabled: true\n",
                     "enabled: bypassed\n",
                     "llm:\n  enabled: bypass\n"):
            with self.subTest(text=text):
                self.set_project(self.a, text)
                self.assertGuarded(self.hook(self.protected_write(self.a),
                                             self.a))

    def test_project_bypass_is_that_tree_only(self):
        self.set_project(self.a, "enabled: bypass\n")
        self.assertGuarded(self.hook(self.protected_write(self.a), self.b))

    def test_pure_classification_agrees(self):
        self.set_project(self.a, "enabled: bypass\n")
        decision, reason, action, _, _ = classify_mod.get_classification(
            "Bash", {"command": self.protected_write(self.a)}, str(self.a))
        self.assertEqual((decision, action), ("SKIP", None))
        self.assertIn("enabled: bypass", reason)


class TestGlobalBypass(_Fixture):

    def test_global_bypass_is_bypass_everywhere(self):
        self.set_user("enabled: bypass\n")
        for cwd in (self.a, self.b, self.root):
            with self.subTest(cwd=cwd.name):
                self.assertNothingRan(
                    self.hook(self.protected_write(self.a), cwd))

    def test_global_bypass_wins_over_a_project_saying_true(self):
        self.set_user("enabled: bypass\n")
        self.set_project(self.a, "enabled: true\n")
        self.assertNothingRan(self.hook("git push", self.a))

    def test_global_false_and_off_are_not_bypass(self):
        self.set_user("enabled: off\n")
        self.assertGuarded(self.hook(self.protected_write(self.b), self.a))


class TestNeverSilent(_Fixture):

    def run_cmd(self, fn, args, stdin=""):
        out = io.StringIO()
        with patch("sys.stdin", io.StringIO(stdin)), redirect_stdout(out):
            fn(args)
        return out.getvalue()

    def test_session_start_announces_bypass(self):
        self.set_project(self.a, "enabled: bypass\n")
        out = self.run_cmd(cmd_session_start, None,
                           json.dumps({"cwd": str(self.a)}))
        message = json.loads(out)
        self.assertIn("bouncer is BYPASSED here", message["systemMessage"])
        self.assertIn(str(self.a / ".bouncer" / "config.yaml"),
                      message["systemMessage"])
        self.assertEqual(message["hookSpecificOutput"]["hookEventName"],
                         "SessionStart")

    def test_session_start_is_silent_when_on(self):
        out = self.run_cmd(cmd_session_start, None,
                           json.dumps({"cwd": str(self.a)}))
        self.assertEqual(out, "")

    def test_session_start_names_the_global_file(self):
        self.set_user("enabled: bypass\n")
        out = self.run_cmd(cmd_session_start, None,
                           json.dumps({"cwd": str(self.root)}))
        self.assertIn(str(self.user / "config.yaml"),
                      json.loads(out)["systemMessage"])

    def profile(self, fmt):
        class _Args:
            name = None
            cwd = str(self.a)
            as_format = fmt
        return self.run_cmd(cmd_profile, _Args())

    def test_profile_says_bypass_in_every_format(self):
        self.set_project(self.a, "enabled: bypass\n")
        state = json.loads(self.profile("json"))
        self.assertEqual((state["profile"], state["bypass"]),
                         ("bypass", True))
        self.assertIn("config.yaml", state["bypass_source"])
        self.assertIn("bypass", self.profile("tmux"))
        self.assertIn("bypass", self.profile("ansi"))
        self.assertIn("enabled: bypass", self.profile("plain"))

    def test_status_says_bypassed(self):
        self.set_project(self.a, "enabled: bypass\n")
        with patch("pathlib.Path.cwd", return_value=self.a):
            out = self.run_cmd(cmd_status, type("A", (), {"verbose": False}))
        self.assertIn("bouncer BYPASSED", out)

    def test_prompt_break_marker_not_written_when_bypassed(self):
        self.set_project(self.a, "enabled: bypass\n")

        class _Args:
            mark_break = True
        self.run_cmd(cmd_log, _Args(), json.dumps({"cwd": str(self.a)}))
        self.assertEqual(self.logs(), [])


if __name__ == "__main__":
    unittest.main()
