#!/usr/bin/env python3
"""bouncer propose / apply / revert: approved policy edits, applied by the
orchestrator, and every refusal the brief names — wrong caller, wrong
target, a changed diff, a reused code.

The process tree is faked by replacing the two lookups (`ps` and `lsof`),
so calling_session()'s own walk is what runs. Every config and policy is a
fixture in a temp dir.
"""

import difflib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import bouncer.classify as classify_mod
import bouncer.config as cfg
import bouncer.profile as profile_mod
import bouncer.proposals as proposals

Refused = proposals.Refused


def _diff(old: str, new: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        "a/.bouncer/policy.md", "b/.bouncer/policy.md"))


class _Fixture(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.root = root
        self.user = root / "user"
        self.user.mkdir()
        self.hobbes = self.make_project("hobbes", "hobbes policy\n")
        self.proj = self.make_project("proj", "line one\nline two\n")
        self.bare = root / "bare"
        self.bare.mkdir()
        self.store = root / "proposals"
        self.set_user(f"orchestrators: [{self.hobbes}]\n")
        for target, value in (
                (cfg, ("USER_CONFIG_FILE", self.user / "config.yaml")),
                (cfg, ("USER_CONFIG_DIR", self.user)),
                (proposals, ("PROPOSALS_DIR", self.store)),
                (profile_mod, ("PROFILE_DIR", root / "profile"))):
            p = patch.object(target, *value)
            p.start()
            self.addCleanup(p.stop)
        self.session(self.hobbes)

    def make_project(self, name, policy):
        project = self.root / name
        (project / ".bouncer").mkdir(parents=True)
        (project / ".bouncer" / "config.yaml").write_text("enabled: true\n")
        (project / ".bouncer" / "policy.md").write_text(policy)
        return project

    def set_user(self, text):
        (self.user / "config.yaml").write_text(text)

    def session(self, *launch_dirs, shell_only=False):
        """Fake the process tree above this process: one `claude` per
        launch dir, innermost first, each under a shell. With no dirs (or
        shell_only), no session at all."""
        rows, cwds = {}, {}
        pid = os.getpid()
        next_pid = 5000
        chain = [] if shell_only else list(launch_dirs)
        rows[pid] = (next_pid, "zsh")
        for launch in chain:
            claude, shell = next_pid, next_pid + 1
            rows[claude] = (shell, "claude")
            cwds[claude] = Path(launch)
            rows[shell] = (shell + 1, "bash")
            next_pid = shell + 1
        rows[next_pid] = (1, "tmux")
        for name, fake in (("_process_row", lambda p: rows.get(p)),
                           ("_process_cwd", lambda p: cwds.get(p))):
            p = patch.object(proposals, name, side_effect=fake)
            p.start()
            self.addCleanup(p.stop)

    @property
    def policy(self):
        return self.proj / ".bouncer" / "policy.md"

    def propose_change(self, new="line one\nline two\nNEVER print.\n",
                       project=None):
        project = project or self.proj
        old = (project / ".bouncer" / "policy.md").read_text()
        return proposals.propose(str(project), _diff(old, new))

    def assertRefused(self, fn, *args, says):
        with self.assertRaises(Refused) as ctx:
            fn(*args)
        self.assertIn(says, str(ctx.exception))


class TestTheFlow(_Fixture):

    def test_propose_apply_revert(self):
        record = self.propose_change()
        code = record["code"]
        self.assertEqual(len(code), 8)
        self.assertEqual(self.policy.read_text(), "line one\nline two\n",
                         "propose must change nothing")
        proposals.apply(code)
        self.assertEqual(self.policy.read_text(),
                         "line one\nline two\nNEVER print.\n")
        proposals.revert(code)
        self.assertEqual(self.policy.read_text(), "line one\nline two\n")

    def test_apply_and_revert_are_logged(self):
        code = self.propose_change()["code"]
        proposals.apply(code)
        proposals.revert(code)
        lines = [json.loads(l) for l in
                 (self.store / "log.jsonl").read_text().splitlines()]
        self.assertEqual([l["action"] for l in lines], ["apply", "revert"])
        self.assertEqual({l["code"] for l in lines}, {code})
        self.assertEqual(lines[0]["target"], str(self.policy))
        self.assertEqual(lines[0]["by"], str(self.hobbes))

    def test_same_change_proposed_twice_is_one_code(self):
        self.assertEqual(self.propose_change()["code"],
                         self.propose_change()["code"])

    def test_local_layer_is_never_touched(self):
        local = self.proj / ".bouncer" / "policy.local.md"
        local.write_text("private\n")
        proposals.apply(self.propose_change()["code"])
        self.assertEqual(local.read_text(), "private\n")


class TestWrongCaller(_Fixture):

    def test_a_worker_session(self):
        code = self.propose_change()["code"]
        self.session(self.proj)
        self.assertRefused(proposals.apply, code, says="wrong caller")
        self.assertEqual(self.policy.read_text(), "line one\nline two\n")

    def test_a_worker_on_its_own_policy(self):
        code = self.propose_change()["code"]
        self.session(self.proj)
        self.assertRefused(proposals.apply, code, says="wrong caller")

    def test_a_session_nested_inside_another(self):
        # A worker started `claude -p` inside hobbes's directory.
        code = self.propose_change()["code"]
        self.session(self.hobbes, self.proj)
        self.assertRefused(proposals.apply, code, says="inside another")

    def test_no_session_at_all(self):
        # A person at the terminal, or a process detached from its session.
        code = self.propose_change()["code"]
        self.session(shell_only=True)
        self.assertRefused(proposals.apply, code, says="not inside one")

    def test_no_orchestrator_configured(self):
        code = self.propose_change()["code"]
        self.set_user("enabled: true\n")
        self.assertRefused(proposals.apply, code, says="no orchestrator")

    def test_a_project_cannot_name_itself_orchestrator(self):
        (self.proj / ".bouncer" / "config.yaml").write_text(
            f"enabled: true\norchestrators: [{self.proj}]\n")
        code = self.propose_change()["code"]
        self.session(self.proj)
        self.assertRefused(proposals.apply, code, says="wrong caller")

    def test_revert_has_the_same_wall(self):
        code = self.propose_change()["code"]
        proposals.apply(code)
        self.session(self.proj)
        self.assertRefused(proposals.revert, code, says="wrong caller")


class TestWrongTarget(_Fixture):

    def test_the_orchestrators_own_policy(self):
        self.assertRefused(self.propose_change, "x\n", self.hobbes,
                           says="orchestrator's own policy")

    def test_an_uninitialized_project(self):
        (self.bare / ".bouncer").mkdir()
        (self.bare / ".bouncer" / "policy.md").write_text("x\n")
        self.assertRefused(proposals.propose, str(self.bare), "",
                           says="not initialized")

    def test_a_symlinked_policy(self):
        self.policy.unlink()
        self.policy.symlink_to(self.user / "elsewhere.md")
        (self.user / "elsewhere.md").write_text("x\n")
        self.assertRefused(proposals.propose, str(self.proj), "",
                           says="not a symlink")

    def test_a_tampered_target_is_a_changed_proposal(self):
        # The only way to aim at config.yaml is to edit the stored record,
        # and the code covers the target.
        code = self.propose_change()["code"]
        path = self.store / f"{code}.json"
        record = json.loads(path.read_text())
        record["target"] = str(self.proj / ".bouncer" / "config.yaml")
        path.write_text(json.dumps(record))
        self.assertRefused(proposals.apply, code, says="no longer matches")

    def test_target_rules_reject_config_and_local_and_user_scope(self):
        for target in (self.proj / ".bouncer" / "config.yaml",
                       self.proj / ".bouncer" / "policy.local.md",
                       self.user / "policy.md"):
            with self.subTest(target=target.name):
                target.write_text("x\n")
                self.assertRefused(proposals.check_target, target,
                                   says="wrong target")


class TestChangedDiff(_Fixture):

    def test_stored_diff_altered_after_proposal(self):
        code = self.propose_change()["code"]
        path = self.store / f"{code}.json"
        record = json.loads(path.read_text())
        record["new_text"] = "allow everything\n"
        path.write_text(json.dumps(record))
        self.assertRefused(proposals.apply, code, says="no longer matches")
        self.assertEqual(self.policy.read_text(), "line one\nline two\n")

    def test_policy_changed_since_the_proposal(self):
        code = self.propose_change()["code"]
        self.policy.write_text("line one\nline two\nedited by hand\n")
        self.assertRefused(proposals.apply, code, says="changed after")

    def test_diff_that_does_not_apply(self):
        self.assertRefused(proposals.propose, str(self.proj),
                           _diff("nothing like it\n", "x\n"),
                           says="does not apply")

    def test_diff_that_changes_nothing(self):
        self.assertRefused(proposals.propose, str(self.proj), "",
                           says="does not apply")


class TestReusedCode(_Fixture):

    def test_apply_twice(self):
        code = self.propose_change()["code"]
        proposals.apply(code)
        self.assertRefused(proposals.apply, code, says="already used")

    def test_revert_twice(self):
        code = self.propose_change()["code"]
        proposals.apply(code)
        proposals.revert(code)
        self.assertRefused(proposals.revert, code, says="already reverted")

    def test_reproposing_an_applied_change(self):
        record = self.propose_change()
        proposals.apply(record["code"])
        self.policy.write_text(record["base_text"])
        self.assertRefused(self.propose_change, says="already applied")

    def test_revert_never_applied(self):
        code = self.propose_change()["code"]
        self.assertRefused(proposals.revert, code, says="never applied")

    def test_revert_after_a_later_edit(self):
        code = self.propose_change()["code"]
        proposals.apply(code)
        self.policy.write_text("someone else's change\n")
        self.assertRefused(proposals.revert, code, says="changed after")

    def test_unknown_and_malformed_codes(self):
        self.assertRefused(proposals.apply, "deadbeef", says="no proposal")
        self.assertRefused(proposals.apply, "../../x", says="not a proposal")

    def test_one_code_per_command(self):
        from bouncer.__main__ import main
        err = io.StringIO()
        with patch("sys.argv", ["bouncer", "apply", "aaaaaaaa", "bbbbbbbb"]), \
                redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
            main()
        self.assertEqual(ctx.exception.code, 2)


class TestHookGuard(_Fixture):
    """The hook refuses before the CLI runs, and protects the store."""

    def test_hook_refuses_apply_from_a_worker(self):
        self.session(self.proj)
        reason = classify_mod._management_denial_reason(
            "Bash", {"command": "bouncer apply deadbeef"}, self.proj)
        self.assertIn("wrong caller", reason)

    def test_hook_lets_the_orchestrator_through(self):
        reason = classify_mod._management_denial_reason(
            "Bash", {"command": "bouncer apply deadbeef"}, self.hobbes)
        self.assertIsNone(reason)

    def test_propose_is_open_to_anyone(self):
        self.session(self.proj)
        reason = classify_mod._management_denial_reason(
            "Bash", {"command": "bouncer propose hobbes /tmp/x.diff"},
            self.proj)
        self.assertIsNone(reason)

    def test_agents_cannot_write_the_store(self):
        for command in (
                f"echo x > {self.store}/deadbeef.json",
                "echo x >> ~/.local/share/bouncer/proposals/log.jsonl",
                f"python3 -c \"open('{self.store}/a.json','w')\""):
            with self.subTest(command=command):
                reason = classify_mod._management_denial_reason(
                    "Bash", {"command": command}, self.proj)
                self.assertIn("cannot be modified", reason or "")


if __name__ == "__main__":
    unittest.main()
