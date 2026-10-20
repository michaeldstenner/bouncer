#!/usr/bin/env python3
"""The flip-flop fixes (recon 2026-10-20, scratch/recon-flipflop-3DP.md):

1. A tool called by full path is shown to the classifier by its bare
   name when that is the same file on PATH. A path into another
   project's folder read as "that project's code".
2. A shell sitting somewhere unarmed (the session scratchpad) falls back
   to the session's launch directory instead of running unjudged; what
   is still skipped is logged.
3. Every verdict records why: the policy line, the fact in the request,
   and the sentence linking them, as log fields.

Every project, PATH entry and log here is a fixture in a temp dir.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bouncer.classify as classify_mod
import bouncer.providers as providers_mod

from test_bouncer import _classify, _make_bouncer_dir

_ARMED = "enabled: true\n"


def _bash(command, cwd):
    return {"tool_name": "Bash", "tool_input": {"command": command},
            "session_id": "s1", "cwd": str(cwd)}


def _rows(project):
    log = project / ".bouncer" / "log.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()
            if line.strip() and json.loads(line).get("decision") != "PENDING"]


class _Fixture(unittest.TestCase):
    """root/
         tools/penny/bin/penny   a tool in another project's folder
         bin/penny -> that       on PATH
         other/bin/penny         same name, a different file
         proj/                   an armed project
         scratch/                unarmed, like a session scratchpad
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.tool = self.root / "tools" / "penny" / "bin" / "penny"
        self.other = self.root / "other" / "bin" / "penny"
        for exe in (self.tool, self.other):
            exe.parent.mkdir(parents=True)
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
        (self.root / "bin").mkdir()
        (self.root / "bin" / "penny").symlink_to(self.tool)
        p = patch.dict(os.environ, {"PATH": str(self.root / "bin"),
                                    "HOME": str(self.root)})
        p.start()
        self.addCleanup(p.stop)
        self.proj = self.root / "proj"
        self.proj.mkdir()
        _make_bouncer_dir(self.proj, config_yaml=_ARMED, policy_md="proj")
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()


class TestNormalize(_Fixture):

    def test_full_path_to_the_path_binary_becomes_its_name(self):
        self.assertEqual(
            classify_mod.normalize_tool_paths(f"{self.tool} audit X.FCStd"),
            "penny audit X.FCStd")

    def test_tilde_path(self):
        self.assertEqual(
            classify_mod.normalize_tool_paths(
                "~/tools/penny/bin/penny audit X | grep '^bodies'"),
            "penny audit X | grep '^bodies'")

    def test_inside_a_compound_command(self):
        self.assertEqual(
            classify_mod.normalize_tool_paths(
                f"sleep 3; cat s.txt; {self.tool} audit X 2>&1 | grep b"),
            "sleep 3; cat s.txt; penny audit X 2>&1 | grep b")

    def test_same_name_but_a_different_file_is_left(self):
        command = f"{self.other} audit X"
        self.assertEqual(classify_mod.normalize_tool_paths(command), command)

    def test_not_on_path_is_left(self):
        command = f"{self.root}/tools/penny/bin/nothere x"
        self.assertEqual(classify_mod.normalize_tool_paths(command), command)

    def test_an_argument_is_never_rewritten(self):
        command = f"cat {self.tool}; ls -l {self.tool}"
        self.assertEqual(classify_mod.normalize_tool_paths(command), command)

    def test_classifier_sees_the_name_and_the_log_keeps_both(self):
        calls = []
        _classify(_bash(f"{self.tool} audit X", self.proj), llm_calls=calls)
        self.assertEqual(calls[0][1]["command"], "penny audit X")
        row = _rows(self.proj)[-1]
        self.assertIn(str(self.tool), row["input_summary"])
        self.assertEqual(row["classified_command"], "penny audit X")

    def test_unchanged_command_logs_no_rewrite(self):
        _classify(_bash("penny audit X", self.proj))
        self.assertNotIn("classified_command", _rows(self.proj)[-1])


class TestExecShim(_Fixture):
    """A tool whose wrapper finds its repo from $0 cannot be symlinked onto
    PATH; it gets a shim that execs it. Exactly that shim — and nothing
    looser — counts as the same program."""

    def setUp(self):
        super().setUp()
        self.real = self.root / "tools" / "printwatch" / "bin" / "printwatch"
        self.real.parent.mkdir(parents=True)
        self.real.write_text("#!/bin/sh\n")
        self.real.chmod(0o755)

    def shim(self, text):
        path = self.root / "bin" / "printwatch"
        path.write_text(text)
        path.chmod(0o755)

    def assertRewritten(self, yes):
        command = "~/tools/printwatch/bin/printwatch preflight --plate satin"
        shown = classify_mod.normalize_tool_paths(command)
        self.assertEqual(shown, "printwatch preflight --plate satin"
                         if yes else command)

    def test_the_printwatch_shim(self):
        self.shim("#!/bin/sh\n"
                  "# Shim so printwatch is on PATH by bare name.\n"
                  "# Its own wrapper finds its repo from $0.\n"
                  'exec "$HOME/tools/printwatch/bin/printwatch" "$@"\n')
        self.assertRewritten(True)

    def test_braced_home_and_literal_path_and_env_shebang(self):
        for line in ('exec "${HOME}/tools/printwatch/bin/printwatch" "$@"',
                     f'exec "{self.real}" "$@"'):
            for bang in ("#!/bin/sh", "#!/usr/bin/env bash", "#!/bin/zsh"):
                with self.subTest(line=line, bang=bang):
                    self.shim(f"{bang}\n{line}\n")
                    self.assertRewritten(True)

    def test_anything_else_is_not_a_shim(self):
        for text in (
                # Another command alongside the exec.
                '#!/bin/sh\nexport X=1\nexec "$HOME/tools/printwatch/bin/'
                'printwatch" "$@"\n',
                # Arguments added or changed.
                '#!/bin/sh\nexec "$HOME/tools/printwatch/bin/printwatch" '
                '--dry-run "$@"\n',
                '#!/bin/sh\nexec "$HOME/tools/printwatch/bin/printwatch" "$1"\n',
                '#!/bin/sh\nexec "$HOME/tools/printwatch/bin/printwatch" $@\n',
                # Another variable, or a path built at run time.
                '#!/bin/sh\nexec "$PW_ROOT/bin/printwatch" "$@"\n',
                '#!/bin/sh\nexec "$HOME/tools/$TOOL/bin/printwatch" "$@"\n',
                '#!/bin/sh\nexec "$(dirname $0)/printwatch" "$@"\n',
                # Not exec, no shebang, not a plain shell.
                '#!/bin/sh\n"$HOME/tools/printwatch/bin/printwatch" "$@"\n',
                'exec "$HOME/tools/printwatch/bin/printwatch" "$@"\n',
                '#!/usr/bin/env python3\nexec "$HOME/tools/printwatch/bin/'
                'printwatch" "$@"\n',
                # A tilde does not expand inside quotes.
                '#!/bin/sh\nexec "~/tools/printwatch/bin/printwatch" "$@"\n'):
            with self.subTest(text=text):
                self.shim(text)
                self.assertRewritten(False)

    def test_a_shim_for_a_different_file(self):
        self.shim('#!/bin/sh\nexec "$HOME/other/bin/penny" "$@"\n')
        self.assertRewritten(False)

    def test_a_large_file_is_not_read_as_a_shim(self):
        self.shim('#!/bin/sh\nexec "$HOME/tools/printwatch/bin/printwatch" '
                  '"$@"\n' + "#" * 5000 + "\n")
        self.assertRewritten(False)


class TestCwdFallback(_Fixture):

    def test_unarmed_shell_falls_back_to_the_launch_project(self):
        calls = []
        _, _, code = _classify(
            _bash("ls", self.scratch), launch_dir=self.proj,
            call_llm_result=("DENY", "no", None, None), llm_calls=calls)
        self.assertEqual(code, 2, "the launch project's verdict applies")
        self.assertEqual(Path(calls[0][2]), self.proj)
        row = _rows(self.proj)[-1]
        self.assertEqual(row["decision"], "DENY")
        self.assertEqual(row["cwd"], str(self.scratch))
        self.assertEqual(row["cwd_fallback"], str(self.proj))

    def test_armed_cwd_never_falls_back(self):
        other = self.root / "other_proj"
        other.mkdir()
        _make_bouncer_dir(other, config_yaml=_ARMED, policy_md="other")
        calls = []
        _classify(_bash("ls", other), launch_dir=self.proj, llm_calls=calls)
        self.assertEqual(Path(calls[0][2]), other)
        self.assertNotIn("cwd_fallback", _rows(other)[-1])
        self.assertEqual(_rows(self.proj), [])

    def test_unarmed_launch_dir_is_still_a_skip_and_is_logged(self):
        logged = []
        real = classify_mod.log_decision

        def spy(*args, **kwargs):
            logged.append(args[3:5])
            return real(*args, **kwargs)

        calls = []
        with patch.object(classify_mod, "log_decision", side_effect=spy):
            out, err, code = _classify(_bash("ls", self.scratch),
                                       launch_dir=self.root / "bin",
                                       llm_calls=calls)
        self.assertEqual((out, code, calls), ("", 0, []))
        self.assertEqual(logged,
                         [("SKIP", classify_mod._NO_PROJECT_REASON)])

    def test_guard_still_reads_paths_from_the_real_cwd(self):
        # Relative to scratch/, not to the launch project.
        _, err, code = _classify(
            _bash("echo x > ../proj/.bouncer/policy.md", self.scratch),
            launch_dir=self.proj)
        self.assertEqual(code, 2)
        self.assertIn("cannot be modified", err)

    def test_not_intercepted_skip_is_logged(self):
        _make_bouncer_dir(self.proj, config_yaml="enabled: true\n"
                          "tools: [bash]\n", policy_md="proj")
        _classify({"tool_name": "Read", "tool_input": {"file_path": "x"},
                   "session_id": "s1", "cwd": str(self.proj)})
        row = _rows(self.proj)[-1]
        self.assertEqual(row["decision"], "SKIP")
        self.assertIn("not intercepted", row["reason"])


class TestExplanation(_Fixture):

    def test_reply_fields_are_parsed(self):
        reply = ("DECISION: DENY\n"
                 "REASON: Running printwatch from its folder reads as its code.\n"
                 "POLICY: \"Modify other projects' code, including printwatch's.\"\n"
                 "FACT: ~/Documents/Code/printwatch/bin/printwatch preflight")
        self.assertEqual(providers_mod._parse_llm_text(reply),
                         ("DENY", "Running printwatch from its folder reads "
                                  "as its code."))
        self.assertEqual(providers_mod._parse_explanation(reply), {
            "policy_quote": "\"Modify other projects' code, including "
                            "printwatch's.\"",
            "request_fact": "~/Documents/Code/printwatch/bin/printwatch "
                            "preflight"})

    def test_a_cut_off_reply_keeps_its_verdict(self):
        reply = "DECISION: ALLOW\nREASON: A read.\nPOLICY: \"The agent ma"
        self.assertEqual(providers_mod._parse_llm_text(reply),
                         ("ALLOW", "A read."))
        self.assertEqual(providers_mod._parse_explanation(reply),
                         {"policy_quote": "\"The agent ma"})

    def test_old_two_line_reply_still_works(self):
        reply = "DECISION: ALLOW\nREASON: A read."
        self.assertEqual(providers_mod._parse_llm_text(reply),
                         ("ALLOW", "A read."))
        self.assertEqual(providers_mod._parse_explanation(reply), {})

    def test_allow_and_deny_both_log_the_why(self):
        for verdict in ("ALLOW", "DENY"):
            with self.subTest(verdict=verdict):
                explain = {"policy_quote": "\"Run FreeCAD.\"",
                           "request_fact": "freecadcmd x.py",
                           "tokens": {"prompt": 2100, "response": 60}}
                _classify(_bash("freecadcmd x.py", self.proj),
                          call_llm_result=(verdict, "linked.", None, None,
                                           explain))
                row = _rows(self.proj)[-1]
                self.assertEqual(row["decision"], verdict)
                self.assertEqual(row["reason"], "linked.")
                for key, value in explain.items():
                    self.assertEqual(row[key], value)

    def test_system_prompt_asks_for_the_fields_after_the_verdict(self):
        tail = providers_mod.DEFAULT_SYSTEM_PROMPT.rsplit(
            "nothing else:", 1)[1]
        order = [tail.index(k) for k in
                 ("DECISION:", "REASON:", "POLICY:", "FACT:")]
        self.assertEqual(order, sorted(order),
                         "verdict first, so a cut-off reply keeps it")


if __name__ == "__main__":
    unittest.main()
