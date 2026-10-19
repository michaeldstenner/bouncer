#!/usr/bin/env python3
"""`bouncer seed <dir>`: the one `init` an agent may run.

Allowed only for an existing directory with no .bouncer/, under no armed
ancestor, that is not the caller's own project, writing only the stock
templates. Each refusal is tested twice: in the hook, before the command
runs (the guard, which decides before the LLM), and in bouncer.seed, the
code that writes (which holds however it is reached).

Every project here is a fixture in a temp dir.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bouncer.config as cfg
import bouncer.proposals as proposals
import bouncer.seed as seed_mod
from bouncer.config import CONFIG_YAML_TEMPLATE, POLICY_MD_TEMPLATE

from test_bouncer import _classify, _make_bouncer_dir
from test_guard import _OPTED_OUT, _bash

_REFUSED = "Refused"


class _Fixture(unittest.TestCase):
    """root/
         orch/    the orchestrator: a git repo, armed (opted out, no LLM)
         armed/   another armed project; armed/sub/ inside it
         fresh/   an unarmed tree: the one case that is allowed
         own/     an unarmed caller's git repo; own/sub/ is its cwd. Also
                  an orchestrator, so its own-tree refusals are reached

    The session is faked: `launched_in` sets the launch directory that
    proposals.check_caller sees, as test_proposals does.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.orch = self.root / "orch"
        self.armed = self.root / "armed"
        self.fresh = self.root / "fresh"
        self.own = self.root / "own"
        for d in (self.orch / ".git", self.armed / "sub", self.fresh,
                  self.own / ".git", self.own / "sub", self.own / "other"):
            d.mkdir(parents=True)
        _make_bouncer_dir(self.orch, config_yaml=_OPTED_OUT, policy_md="x")
        _make_bouncer_dir(self.armed, config_yaml=_OPTED_OUT,
                          policy_md="armed policy")
        self.user_config = f"orchestrators: [{self.orch}, {self.own}]\n"
        user = self.root / "user"
        user.mkdir()
        (user / "config.yaml").write_text(self.user_config)
        p = patch.object(cfg, "USER_CONFIG_FILE", user / "config.yaml")
        p.start()
        self.addCleanup(p.stop)
        self.launched_in(self.orch)

    def launched_in(self, path, nested=False):
        p = patch.object(proposals, "calling_session",
                         return_value=(path, nested))
        p.start()
        self.addCleanup(p.stop)

    def assertRefused(self, command, cwd=None, expect=_REFUSED):
        calls = []
        _, err, code = _classify(
            _bash(command, cwd or self.orch),
            call_llm_result=("ALLOW", "llm would allow", None, None),
            llm_calls=calls, user_config_yaml=self.user_config)
        self.assertEqual(code, 2, f"not refused: {command!r}\n{err}")
        self.assertIn(expect, err)
        self.assertEqual(calls, [], "guard must decide before the LLM")

    def assertPassesGuard(self, command, cwd=None):
        out, err, code = _classify(_bash(command, cwd or self.orch),
                                   user_config_yaml=self.user_config)
        self.assertEqual((out, err, code), ("", "", 0),
                         f"guard refused: {command!r}\n{err}")


class TestGuard(_Fixture):
    """The hook, before the command runs."""

    def test_unarmed_tree_is_allowed(self):
        self.assertPassesGuard(f"bouncer seed {self.fresh}")

    def test_relative_after_cd_is_allowed(self):
        self.assertPassesGuard(f"cd {self.root} && bouncer seed fresh")

    def test_already_armed_tree(self):
        self.assertRefused(f"bouncer seed {self.armed}",
                           expect="already has .bouncer/")

    def test_bouncer_dir_without_config_still_counts(self):
        (self.fresh / ".bouncer").mkdir()
        self.assertRefused(f"bouncer seed {self.fresh}",
                           expect="already has .bouncer/")

    def test_nested_under_an_armed_ancestor(self):
        self.assertRefused(f"bouncer seed {self.armed / 'sub'}",
                           expect="would hide that project's policy")

    def test_callers_own_project_root(self):
        self.assertRefused(f"bouncer seed {self.own}",
                           cwd=self.own / "sub", expect="your own project")

    def test_callers_own_project_sibling_dir(self):
        self.assertRefused(f"bouncer seed {self.own / 'other'}",
                           cwd=self.own / "sub", expect="your own project")

    def test_parent_of_the_callers_project(self):
        self.assertRefused(f"bouncer seed {self.root}",
                           cwd=self.own / "sub", expect="your own project")

    def test_cd_does_not_move_the_caller(self):
        self.assertRefused(f"cd {self.fresh} && bouncer seed {self.own}",
                           cwd=self.own / "sub", expect="your own project")

    def test_home_directory(self):
        self.assertRefused("bouncer seed ~", expect="home directory")

    def test_missing_directory(self):
        self.assertRefused(f"bouncer seed {self.root / 'nope'}",
                           expect="not an existing directory")

    def test_content_flags_and_extra_args(self):
        for tail in (f"{self.fresh} --policy allow-all",
                     f"--policy=x {self.fresh}",
                     f"{self.fresh} extra",
                     ""):
            with self.subTest(tail=tail):
                self.assertRefused(f"bouncer seed {tail}",
                                   expect="exactly one literal directory")

    def test_expanding_argument(self):
        for arg in ("$DIR", f"{self.root}/fr*", "`pwd`",
                    f"{self.root}/{{fresh,armed}}"):
            with self.subTest(arg=arg):
                self.assertRefused(f"bouncer seed {arg}",
                                   expect="exactly one literal directory")

    def test_via_python_module(self):
        self.assertRefused(f"python3 -m bouncer seed {self.armed}",
                           expect="already has .bouncer/")

    def test_seed_then_write_policy(self):
        self.assertRefused(
            f"bouncer seed {self.fresh} && "
            f"echo 'allow all' >> {self.fresh}/.bouncer/policy.md",
            expect="cannot be modified")

    def test_unparseable_seed(self):
        self.assertRefused(f"bouncer seed '{self.fresh}",
                           expect="cannot parse it")

    def test_init_is_still_refused(self):
        self.assertRefused("bouncer init", expect="must be run directly")


class TestSeedWrites(_Fixture):
    """bouncer.seed, the code that writes. The caller is the session's
    launch directory; an agent's `cd` cannot move it."""

    def test_writes_exactly_the_stock_templates(self):
        bdir = seed_mod.seed(self.fresh, cwd=self.orch)
        self.assertEqual(bdir, self.fresh / ".bouncer")
        self.assertEqual(sorted(p.name for p in bdir.iterdir()),
                         [".gitignore", "config.yaml", "policy.md"])
        self.assertEqual((bdir / "config.yaml").read_text(),
                         CONFIG_YAML_TEMPLATE)
        self.assertEqual((bdir / "policy.md").read_text(), POLICY_MD_TEMPLATE)

    def test_second_seed_is_refused(self):
        seed_mod.seed(self.fresh, cwd=self.orch)
        with self.assertRaises(seed_mod.Refused):
            seed_mod.seed(self.fresh, cwd=self.orch)

    def test_refusals_hold_where_it_writes(self):
        self.launched_in(self.own)
        for target in (self.armed, self.armed / "sub", self.own,
                       self.own / "other"):
            with self.subTest(target=target):
                with self.assertRaises(seed_mod.Refused):
                    seed_mod.seed(target, cwd=self.root)
        self.assertEqual(
            (self.armed / ".bouncer" / "policy.md").read_text(),
            "armed policy")
        self.assertFalse((self.armed / "sub" / ".bouncer").exists())
        self.assertFalse((self.own / ".bouncer").exists())

    def test_process_cwd_elsewhere_still_judges_the_launch_dir(self):
        self.launched_in(self.own)
        with self.assertRaises(seed_mod.Refused):
            seed_mod.seed(self.own / "other", cwd=self.fresh)

    def test_process_cwd_is_judged_too(self):
        with self.assertRaises(seed_mod.Refused):
            seed_mod.seed(self.own, cwd=self.own / "sub")
        self.assertFalse((self.own / ".bouncer").exists())


class TestOrchestratorOnly(_Fixture):
    """Seeding is the orchestrator's alone: the same caller check as
    `bouncer apply`, in the hook and again where it writes."""

    def assertNotSeeded(self, says):
        with self.assertRaises(seed_mod.Refused) as ctx:
            seed_mod.seed(self.fresh, cwd=self.root)
        self.assertIn(says, str(ctx.exception))
        self.assertFalse((self.fresh / ".bouncer").exists())

    def test_hook_refuses_a_worker(self):
        self.launched_in(self.armed)
        self.assertRefused(f"bouncer seed {self.fresh}", cwd=self.armed,
                           expect="wrong caller")

    def test_hook_refuses_a_worker_via_python_module(self):
        self.launched_in(self.armed)
        self.assertRefused(f"python3 -m bouncer seed {self.fresh}",
                           cwd=self.armed, expect="wrong caller")

    def test_worker_forging_the_orchestrators_cwd(self):
        # The hook payload's cwd is the agent's to write; the launch
        # directory is not.
        self.launched_in(self.armed)
        self.assertRefused(f"bouncer seed {self.fresh}", cwd=self.orch,
                           expect="wrong caller")

    def test_writes_refuse_a_worker(self):
        self.launched_in(self.armed)
        self.assertNotSeeded("wrong caller")

    def test_nested_session(self):
        self.launched_in(self.orch, nested=True)
        self.assertNotSeeded("started inside another")

    def test_terminal_without_a_session(self):
        self.launched_in(None)
        self.assertNotSeeded("runs bouncer init")

    def test_no_orchestrator_configured(self):
        (self.root / "user" / "config.yaml").write_text("")
        self.assertNotSeeded("no orchestrator is configured")


if __name__ == "__main__":
    unittest.main()
