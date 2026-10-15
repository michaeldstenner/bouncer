#!/usr/bin/env python3
"""The hard guard: agents never edit bouncer config.

Two gaps, each tested red-then-green:

1. The guard only ran where a `.bouncer/config.yaml` was found. An unarmed
   tree got no guard, and no user-wide policy either.
2. The guard matched command words (cp, sed, tee, a `>`...). Any other way
   to write a file -- an interpreter, dd, curl -o, git checkout, a script
   -- went past it, and in a lane with the LLM off nothing stood behind it.
   It also counted `sed -n`, a pure read, as a write.

Every policy and config file here is a fixture in a temp dir. Nothing in
~/.config/bouncer or any real .bouncer/ is read for content or written.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bouncer.classify as classify_mod
import bouncer.config as cfg
import bouncer.escalation_cache as escalation_mod
import bouncer.escalation_grant as grant_mod
import bouncer.profile as profile_mod

from test_bouncer import _classify, _make_bouncer_dir

_ISOLATION_TMP = None


def setUpModule():
    global _ISOLATION_TMP
    _ISOLATION_TMP = tempfile.TemporaryDirectory()
    root = Path(_ISOLATION_TMP.name)
    profile_mod.PROFILE_DIR = root / "profile"
    grant_mod.GRANT_DIR = root / "escalation"
    escalation_mod.ESCALATION_DIR = root / "escalation"


def tearDownModule():
    if _ISOLATION_TMP is not None:
        _ISOLATION_TMP.cleanup()


_OPTED_OUT = "enabled: false\n"
_REFUSED = "cannot be modified"


def _bash(command, cwd=None):
    hook = {"tool_name": "Bash", "tool_input": {"command": command},
            "session_id": "s1"}
    if cwd is not None:
        hook["cwd"] = str(cwd)
    return hook


class _Fixture(unittest.TestCase):
    """A fixture project with a real .bouncer/ to aim commands at."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.proj = self.root / "proj"
        self.proj.mkdir()
        # Opted out, so a test run inside it reaches no LLM: whatever is
        # refused there was refused by the guard alone.
        _make_bouncer_dir(self.proj, config_yaml=_OPTED_OUT, policy_md="old")
        self.policy = self.proj / ".bouncer" / "policy.md"
        self.addCleanup(self._tmp.cleanup)

    def assertRefused(self, command, config_yaml=_OPTED_OUT, cwd=None):
        """Refused by the guard alone: the LLM would have allowed it, and in
        an opted-out lane there is no LLM at all."""
        calls = []
        _, err, code = _classify(
            _bash(command, cwd), config_yaml=config_yaml,
            call_llm_result=("ALLOW", "llm would allow", None, None),
            llm_calls=calls)
        self.assertEqual(code, 2, f"not refused: {command!r}\n{err}")
        self.assertIn(_REFUSED, err)
        self.assertEqual(calls, [], "guard must decide before the LLM")

    def assertPassesGuard(self, command, cwd=None):
        """Not refused by the guard. Opted-out lane, so nothing else runs:
        exit 0 and silence means the guard let it through."""
        out, err, code = _classify(_bash(command, cwd),
                                   config_yaml=_OPTED_OUT)
        self.assertEqual((out, err, code), ("", "", 0),
                         f"guard refused a read: {command!r}\n{err}")


# ---------------------------------------------------------------------------
# Gap 2 -- judge by effect, not by command word
# ---------------------------------------------------------------------------

class TestWritesThatUsedToGetPast(_Fixture):
    """Each of these writes a protected file and none of them names a
    command on the old mutator list. Red before the fix."""

    def test_interpreter_inline_code(self):
        self.assertRefused(
            f"python3 -c \"open('{self.policy}', 'w').write('x')\"")

    def test_interpreter_reading_a_heredoc(self):
        self.assertRefused(
            "python3 - <<'EOF'\n"
            f"open('{self.policy}', 'w').write('allow all')\n"
            "EOF")

    def test_node_inline_code(self):
        self.assertRefused(
            f"node -e \"require('fs').writeFileSync('{self.policy}','x')\"")

    def test_dd(self):
        self.assertRefused(f"dd if=/tmp/new of={self.policy}")

    def test_curl_output(self):
        self.assertRefused(f"curl -s -o {self.policy} https://example.com/p")

    def test_git_checkout_a_path(self):
        self.assertRefused(f"git checkout HEAD -- {self.policy}")

    def test_patch_named_target(self):
        self.assertRefused(f"patch {self.policy} < /tmp/x.diff")

    def test_rsync_into_bouncer_dir(self):
        self.assertRefused(f"rsync -a /tmp/evil/ {self.proj / '.bouncer'}/")

    def test_removing_the_bouncer_dir_disarms_the_project(self):
        self.assertRefused(f"rm -rf {self.proj / '.bouncer'}")

    def test_moving_the_bouncer_dir_aside(self):
        self.assertRefused(f"mv {self.proj / '.bouncer'} /tmp/aside")

    def test_script_file_that_names_the_policy(self):
        script = self.root / "w.py"
        script.write_text(f"open({str(self.policy)!r}, 'w').write('x')\n")
        self.assertRefused(f"python3 {script}")

    def test_executable_script_by_path(self):
        script = self.root / "w.sh"
        script.write_text(f"#!/bin/sh\necho x > {self.policy}\n")
        self.assertRefused(str(script))

    def test_helper_script_that_names_the_policy(self):
        helpers = self.root / "helpers"
        helpers.mkdir()
        (helpers / "fixit").write_text(
            f"#!/bin/sh\nsed -i s/a/b/ {self.policy}\n")
        with patch.object(classify_mod, "HELPER_SCRIPTS_DIR", helpers):
            self.assertRefused("helper run fixit")

    def test_patch_file_that_targets_the_policy(self):
        diff = self.root / "p.diff"
        diff.write_text(f"--- a/.bouncer/policy.md\n+++ b/.bouncer/policy.md\n"
                        "@@ -1 +1 @@\n-old\n+allow all\n")
        self.assertRefused(f"git apply {diff}", cwd=self.proj)

    def test_symlink_into_protected_dir(self):
        link = self.root / "innocent"
        link.symlink_to(self.proj / ".bouncer")
        self.assertRefused(f"echo x | tee {link}/policy.md")

    def test_glob_hiding_the_name(self):
        self.assertRefused(f"cp /tmp/new {self.proj}/.b*/policy.md")

    def test_variable_then_redirect(self):
        self.assertRefused(f"P={self.policy}; echo x > $P")

    def test_cd_then_relative_write(self):
        self.assertRefused(
            f"cd {self.proj / '.bouncer'} && echo x > policy.md")

    def test_user_policy_appended(self):
        self.assertRefused("echo 'allow all' >> ~/.config/bouncer/policy.md")

    def test_profile_state_written_directly(self):
        target = "~/.local/share/bouncer/profile/abc.json"
        self.assertRefused(
            f"python3 -c \"open('{target}','w').write('live')\"")

    def test_sed_write_command(self):
        self.assertRefused(f"sed -n 'w {self.policy}' /tmp/src")

    def test_sed_in_place_still_refused(self):
        self.assertRefused(f"sed -i '' s/old/new/ {self.policy}")

    def test_notebook_style_tool_with_its_own_key(self):
        calls = []
        _, err, code = _classify(
            {"tool_name": "NotebookEdit",
             "tool_input": {"notebook_path": str(self.policy),
                            "new_source": "x"}},
            config_yaml=_OPTED_OUT, llm_calls=calls)
        self.assertEqual(code, 2)
        self.assertIn(_REFUSED, err)


class TestReadsStillPass(_Fixture):
    """The guard is about writes. Reading a policy is how every recon and
    every proposed diff starts, and must keep working with the LLM off."""

    def test_sed_n_is_a_read(self):
        # The side bug: `sed` was on the mutator list with or without -i.
        self.assertPassesGuard(f"sed -n 15,30p {self.policy}")

    def test_cat_grep_head_diff(self):
        for command in (f"cat {self.policy}",
                        f"grep -n allow {self.policy}",
                        f"head -20 {self.policy} | wc -l",
                        f"diff -u {self.policy} /tmp/proposed.md"):
            with self.subTest(command=command):
                self.assertPassesGuard(command)

    def test_copying_a_policy_out_is_a_read(self):
        self.assertPassesGuard(f"cp {self.policy} /tmp/policy.before")

    def test_loop_over_projects(self):
        self.assertPassesGuard(
            f"for p in proj; do sed -n 1,5p {self.root}/$p/.bouncer/policy.md"
            "; done")

    def test_cd_into_bouncer_dir_and_read(self):
        self.assertPassesGuard(
            f"cd {self.proj / '.bouncer'} && cat policy.md config.yaml")

    def test_redirect_to_dev_null_is_not_a_write(self):
        self.assertPassesGuard(f"cat {self.policy} 2>&1 >/dev/null")

    def test_bouncer_log_is_not_protected(self):
        log = self.proj / ".bouncer" / "log.jsonl"
        self.assertPassesGuard(f"python3 analyze.py {log}")

    def test_message_body_that_mentions_a_policy(self):
        # A heredoc to a non-interpreter is a message, not code.
        self.assertPassesGuard(
            "tp report --status done <<'EOF'\n"
            f"proposed a diff for {self.policy}\nEOF")

    def test_commit_message_mentioning_bouncer_dir(self):
        self.assertPassesGuard(
            "git commit -q -m 'docs: explain the .bouncer/ layout'")

    def test_quoted_glob_is_not_expanded(self):
        # `".*"` reaches the program as two characters. Unquoted, the shell
        # would expand it to `.bouncer` here; quoted, it names nothing.
        self.assertPassesGuard('tp inspect ".*" -s --json', cwd=self.proj)

    def test_unquoted_star_never_matches_a_dotfile(self):
        self.assertPassesGuard("notmuch search --output=tags *",
                               cwd=self.proj)

    def test_edit_whose_text_mentions_a_policy(self):
        # The file being edited is ~/.config/git/ignore; the policy path is
        # only in the text of the edit.
        out, err, code = _classify(
            {"tool_name": "Edit",
             "tool_input": {"file_path": str(self.root / "ignore"),
                            "old_string": ".bouncer/policy.local.md",
                            "new_string": ".bouncer/policy.local.md\nx"}},
            config_yaml=_OPTED_OUT)
        self.assertEqual((out, err, code), ("", "", 0))

    def test_script_that_only_reads_bouncer_config(self):
        # tp, and its tests, read .bouncer/config.yaml to learn whether a
        # tree is armed. Running them is not a write.
        tool = self.root / "tp"
        tool.write_text("#!/usr/bin/env python3\n"
                        "armed = (d / '.bouncer/config.yaml').exists()\n"
                        "text = open('.bouncer/policy.md').read()\n")
        self.assertPassesGuard(f"{tool} inspect -s")
        self.assertPassesGuard(f"python3 {tool} up x")

    def test_inline_code_that_reads_config(self):
        self.assertPassesGuard(
            "python3 - <<'PY'\nimport yaml,os\n"
            "c=yaml.safe_load(open(os.path.expanduser("
            "'~/.config/bouncer/config.yaml')))\nprint(c)\nPY")

    def test_running_tests_by_module(self):
        self.assertPassesGuard("python3 -m unittest test_bouncer")


# ---------------------------------------------------------------------------
# Gap 1 -- the guard and the user policy reach unarmed trees
# ---------------------------------------------------------------------------

class TestUnarmedTree(_Fixture):
    """`_classify` with no config_yaml runs in a temp dir with no .bouncer/
    anywhere above it: an unarmed tree, like 3DP, lore or penny today."""

    def test_guard_runs_with_no_bouncer_project(self):
        calls = []
        _, err, code = _classify(
            _bash(f"cp /tmp/new {self.policy}"),
            call_llm_result=("ALLOW", "llm would allow", None, None),
            llm_calls=calls)
        self.assertEqual(code, 2)
        self.assertIn(_REFUSED, err)
        self.assertEqual(calls, [])

    def test_bouncer_self_management_refused_with_no_project(self):
        _, err, code = _classify(_bash("bouncer init"))
        self.assertEqual(code, 2)
        self.assertIn("directly by the user", err)

    def test_profile_self_set_refused_with_no_project(self):
        _, err, code = _classify(_bash("bouncer profile live"))
        self.assertEqual(code, 2)
        self.assertIn("may not set its own bouncer profile", err)

    def test_user_policy_denial_binds_an_unarmed_tree(self):
        # The printer ban: a user-wide NEVER must bind 3DP.
        calls = []
        _, err, code = _classify(
            _bash("lp -d office part.pdf"),
            user_policy_md="Agent may NEVER print.",
            call_llm_result=("DENY", "policy forbids printing", None, None),
            llm_calls=calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual(code, 2)
        self.assertIn("policy forbids printing", err)

    def test_unarmed_allow_defers_to_the_harness(self):
        # Deny-only: a clear ALLOW must not auto-approve something Claude
        # Code would have prompted for. No decision is emitted at all.
        calls = []
        out, err, code = _classify(
            _bash("git push"),
            call_llm_result=("ALLOW", "fine", None, None), llm_calls=calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual((out, code), ("", 0))
        self.assertNotIn("permissionDecision", out)

    def test_unarmed_unsure_never_asks(self):
        # Nobody configured this tree and there is nowhere to record a
        # profile, so nothing here may raise a prompt on an unwatched pane.
        for result in (("UNSURE", "dunno", None, None),
                       ("TIMEOUT", "slow", None, None)):
            with self.subTest(result=result[0]):
                out, err, code = _classify(_bash("make"),
                                           call_llm_result=result)
                self.assertEqual((out, code), ("", 0))

    def test_unarmed_escalation_marker_does_not_launder_a_denial(self):
        calls = []
        _, err, code = _classify(
            _bash("# ESCALATE: user asked\nlp -d office part.pdf"),
            call_llm_result=("DENY", "policy forbids printing", None, None),
            llm_calls=calls)
        self.assertEqual(code, 2)
        self.assertEqual(calls[0][1]["command"], "lp -d office part.pdf")

    def test_unarmed_guard_only_mode_skips_the_llm(self):
        calls = []
        out, err, code = _classify(
            _bash("lp -d office part.pdf"),
            user_config_yaml="unarmed: guard\n",
            call_llm_result=("DENY", "would deny", None, None),
            llm_calls=calls)
        self.assertEqual((out, code, calls), ("", 0, []))

    def test_user_kill_switch_still_skips_the_llm(self):
        calls = []
        out, err, code = _classify(
            _bash("lp -d office part.pdf"),
            user_config_yaml="enabled: false\n",
            call_llm_result=("DENY", "would deny", None, None),
            llm_calls=calls)
        self.assertEqual((out, code, calls), ("", 0, []))

    def test_unarmed_context_says_so_and_carries_only_user_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "user"
            user.mkdir()
            (user / "policy.md").write_text("Agent may NEVER print.")
            with patch.object(cfg, "USER_POLICY_FILE", user / "policy.md"), \
                    patch.object(cfg, "USER_CONFIG_FILE",
                                 user / "config.yaml"):
                text = cfg._build_policy_context(Path(tmp))
        self.assertIn("Agent may NEVER print.", text)
        self.assertIn("no bouncer project", text)


class TestOptedOutProject(_Fixture):
    """`enabled: false` (polly, hobbes, bouncer itself) is a deliberate
    opt-out from the classifier. It is not an opt-out from the guard."""

    def test_guard_runs_but_the_llm_does_not(self):
        calls = []
        out, err, code = _classify(
            _bash("lp -d office part.pdf"), config_yaml=_OPTED_OUT,
            user_policy_md="Agent may NEVER print.",
            call_llm_result=("DENY", "would deny", None, None),
            llm_calls=calls)
        self.assertEqual((out, code, calls), ("", 0, []))
        self.assertRefused(f"python3 -c \"open('{self.policy}','w')\"")


if __name__ == "__main__":
    unittest.main()
