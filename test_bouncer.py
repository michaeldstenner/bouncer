#!/usr/bin/env python3
"""Tests for the bouncer package."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

import bouncer.config as cfg
import bouncer.classify as classify_mod
from bouncer.yaml import MicroYAML
from bouncer.config import (
    _deep_merge,
    load_yaml_config,
    load_policy,
    _find_bouncer_dir,
    _merged_config,
    _build_policy_context,
    project_has_bouncer,
    CONFIG_YAML_TEMPLATE,
    USER_CONFIG_YAML_TEMPLATE,
)
from bouncer.log import _should_log, _maybe_prune_log, log_decision
from bouncer.commands.lint import cmd_lint
from bouncer.commands.classify import cmd_classify


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bouncer_dir(tmp_path, config_yaml=None, policy_md=None):
    bd = tmp_path / ".bouncer"
    bd.mkdir(exist_ok=True)
    if config_yaml is not None:
        (bd / "config.yaml").write_text(config_yaml, encoding="utf-8")
    if policy_md is not None:
        (bd / "policy.md").write_text(policy_md, encoding="utf-8")
    return bd


def _classify(hook_input, *, call_llm_result=("ALLOW", "ok"),
              config_yaml=None, policy_md=None):
    """
    Run cmd_classify with patched stdin/paths/LLM.
    Returns (stdout_str, stderr_str, exit_code).
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        user_dir = tmp_path / "user"
        user_dir.mkdir()

        if config_yaml is not None or policy_md is not None:
            _make_bouncer_dir(tmp_path, config_yaml, policy_md)

        full_input = dict(hook_input, cwd=hook_input.get("cwd", str(tmp_path)))
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        exit_code  = 0

        with (
            patch.object(cfg, "USER_CONFIG_FILE", user_dir / "config.yaml"),
            patch.object(cfg, "USER_POLICY_FILE", user_dir / "policy.md"),
            patch.object(cfg, "USER_LOG_FILE",    user_dir / "log.jsonl"),
            patch.object(classify_mod, "call_llm", return_value=call_llm_result),
            patch("sys.stdin", io.StringIO(json.dumps(full_input))),
            redirect_stdout(stdout_buf),
            redirect_stderr(stderr_buf),
        ):
            class _A:
                hook = True
                format = "json"
            try:
                cmd_classify(_A())
            except SystemExit as e:
                exit_code = e.code

    return stdout_buf.getvalue(), stderr_buf.getvalue(), exit_code


def _lint(yaml_text):
    """Run cmd_lint on a temp file. Returns (stdout_str, exit_code)."""
    with tempfile.NamedTemporaryFile(
        suffix=".yaml", mode="w", delete=False, encoding="utf-8"
    ) as f:
        f.write(yaml_text)
        path = Path(f.name)

    stdout_buf = io.StringIO()
    exit_code  = 0

    class _A:
        file = str(path)

    with redirect_stdout(stdout_buf):
        try:
            cmd_lint(_A())
        except SystemExit as e:
            exit_code = e.code

    path.unlink()
    return stdout_buf.getvalue(), exit_code


# ---------------------------------------------------------------------------
# MicroYAML
# ---------------------------------------------------------------------------

class TestMicroYAML(unittest.TestCase):
    def setUp(self):
        self.p = MicroYAML()

    def test_flat_map(self):
        self.assertEqual(
            self.p.load("enabled: true\npolicy_mode: append"),
            {"enabled": True, "policy_mode": "append"},
        )

    def test_nested_map(self):
        yaml = "llm:\n  provider: ollama\n  model: qwen2.5:14b\n  timeout: 25"
        self.assertEqual(
            self.p.load(yaml),
            {"llm": {"provider": "ollama", "model": "qwen2.5:14b", "timeout": 25}},
        )

    def test_list_value(self):
        self.assertEqual(self.p.load("tools:\n  - Bash\n  - Write"),
                         {"tools": ["Bash", "Write"]})

    def test_boolean_variants(self):
        self.assertEqual(
            self.p.load("a: true\nb: false\nc: yes\nd: no"),
            {"a": True, "b": False, "c": True, "d": False},
        )

    def test_comments_stripped(self):
        yaml = "# top\nenabled: true  # inline\ntools:\n  - Bash"
        self.assertEqual(self.p.load(yaml), {"enabled": True, "tools": ["Bash"]})

    def test_empty_returns_none(self):
        self.assertIsNone(self.p.load(""))
        self.assertIsNone(self.p.load("   \n# only comments\n"))

    def test_tools_all_is_string(self):
        self.assertEqual(self.p.load("tools: all"), {"tools": "all"})

    def test_tools_empty_flow_list(self):
        self.assertEqual(self.p.load("tools: []"), {"tools": []})

    def test_config_template_parses_cleanly(self):
        result = MicroYAML().load(CONFIG_YAML_TEMPLATE)
        self.assertIsInstance(result, dict)
        self.assertTrue(result.get("enabled"))

    def test_user_config_template_parses_cleanly(self):
        result = MicroYAML().load(USER_CONFIG_YAML_TEMPLATE)
        self.assertIsInstance(result, dict)
        self.assertIn("llm", result)
        self.assertEqual(result["llm"]["provider"], "ollama")


# ---------------------------------------------------------------------------
# _deep_merge
# ---------------------------------------------------------------------------

class TestDeepMerge(unittest.TestCase):
    def test_simple_override(self):
        self.assertEqual(_deep_merge({"a": 1, "b": 2}, {"b": 99}),
                         {"a": 1, "b": 99})

    def test_nested_dict_merged(self):
        base     = {"llm": {"model": "old", "timeout": 25}}
        override = {"llm": {"model": "new"}}
        self.assertEqual(_deep_merge(base, override),
                         {"llm": {"model": "new", "timeout": 25}})

    def test_list_replaced_not_merged(self):
        self.assertEqual(
            _deep_merge({"tools": ["Bash", "Write"]}, {"tools": ["Edit"]}),
            {"tools": ["Edit"]},
        )

    def test_base_key_untouched(self):
        result = _deep_merge({"a": 1, "b": 2}, {"b": 3})
        self.assertEqual(result["a"], 1)

    def test_override_wins_on_scalar(self):
        self.assertEqual(_deep_merge({"enabled": True}, {"enabled": False}),
                         {"enabled": False})


# ---------------------------------------------------------------------------
# Config loading and merging
# ---------------------------------------------------------------------------

class TestConfigLoading(unittest.TestCase):
    def test_load_yaml_config_valid(self):
        with tempfile.NamedTemporaryFile(
            suffix=".yaml", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write("enabled: false\npolicy_mode: replace\n")
            path = Path(f.name)
        try:
            self.assertEqual(load_yaml_config(path),
                             {"enabled": False, "policy_mode": "replace"})
        finally:
            path.unlink()

    def test_load_yaml_config_missing_returns_empty(self):
        self.assertEqual(load_yaml_config(Path("/nonexistent/config.yaml")), {})

    def test_load_policy_existing(self):
        with tempfile.NamedTemporaryFile(
            suffix=".md", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write("# Policy\n- Be careful\n")
            path = Path(f.name)
        try:
            self.assertIn("Be careful", load_policy(path))
        finally:
            path.unlink()

    def test_load_policy_missing_returns_empty(self):
        self.assertEqual(load_policy(Path("/nonexistent/policy.md")), "")

    def test_merged_config_all_defaults_when_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch.object(cfg, "USER_CONFIG_FILE", tmp_path / "config.yaml"):
                config = _merged_config(tmp_path)
        self.assertTrue(config["enabled"])
        self.assertEqual(config["on_unsure"], "ask")
        self.assertEqual(config["on_unavailable"], "ask")
        self.assertEqual(config["tools"], ["Bash"])
        self.assertEqual(config["llm"]["model"], "qwen2.5:14b")

    def test_merged_config_project_overrides_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _make_bouncer_dir(tmp_path, config_yaml="on_unsure: allow\n")
            with patch.object(cfg, "USER_CONFIG_FILE", tmp_path / "no_user.yaml"):
                config = _merged_config(tmp_path)
        self.assertEqual(config["on_unsure"], "allow")
        self.assertEqual(config["on_unavailable"], "ask")  # default untouched

    def test_merged_config_user_then_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user_cfg = tmp_path / "user_config.yaml"
            user_cfg.write_text("on_unsure: deny\non_unavailable: deny\n", encoding="utf-8")
            _make_bouncer_dir(tmp_path, config_yaml="on_unsure: allow\n")
            with patch.object(cfg, "USER_CONFIG_FILE", user_cfg):
                config = _merged_config(tmp_path)
        self.assertEqual(config["on_unsure"], "allow")     # project wins
        self.assertEqual(config["on_unavailable"], "deny") # user survives

    def test_merged_config_local_overrides_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bd = _make_bouncer_dir(tmp_path, config_yaml="on_unsure: ask\n")
            (bd / "config.local.yaml").write_text("on_unsure: allow\n", encoding="utf-8")
            with patch.object(cfg, "USER_CONFIG_FILE", tmp_path / "no_user.yaml"):
                config = _merged_config(tmp_path)
        self.assertEqual(config["on_unsure"], "allow")

    def test_merged_config_llm_partial_override_preserves_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _make_bouncer_dir(tmp_path, config_yaml="llm:\n  model: llama3\n")
            with patch.object(cfg, "USER_CONFIG_FILE", tmp_path / "no_user.yaml"):
                config = _merged_config(tmp_path)
        self.assertEqual(config["llm"]["model"], "llama3")
        self.assertEqual(config["llm"]["provider"], "ollama")  # default preserved
        self.assertEqual(config["llm"]["timeout"], 25)


# ---------------------------------------------------------------------------
# Policy context
# ---------------------------------------------------------------------------

class TestPolicyContext(unittest.TestCase):
    def test_append_combines_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user_pol = tmp_path / "user_policy.md"
            user_pol.write_text("User context", encoding="utf-8")
            _make_bouncer_dir(tmp_path, policy_md="Project context")
            with patch.object(cfg, "USER_POLICY_FILE", user_pol):
                result = _build_policy_context(tmp_path, {"policy_mode": "append"})
        self.assertIn("User context", result)
        self.assertIn("Project context", result)

    def test_replace_uses_project_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user_pol = tmp_path / "user_policy.md"
            user_pol.write_text("User context", encoding="utf-8")
            _make_bouncer_dir(tmp_path, policy_md="Project context")
            with patch.object(cfg, "USER_POLICY_FILE", user_pol):
                result = _build_policy_context(tmp_path, {"policy_mode": "replace"})
        self.assertNotIn("User context", result)
        self.assertIn("Project context", result)

    def test_replace_falls_back_when_no_project_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user_pol = tmp_path / "user_policy.md"
            user_pol.write_text("User context", encoding="utf-8")
            _make_bouncer_dir(tmp_path)  # no policy.md
            with patch.object(cfg, "USER_POLICY_FILE", user_pol):
                result = _build_policy_context(tmp_path, {"policy_mode": "replace"})
        self.assertIn("User context", result)

    def test_no_policies_returns_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch.object(cfg, "USER_POLICY_FILE", tmp_path / "no_policy.md"):
                result = _build_policy_context(tmp_path, {"policy_mode": "append"})
        self.assertEqual(result, "(no policy configured)")


# ---------------------------------------------------------------------------
# Project discovery
# ---------------------------------------------------------------------------

class TestProjectDiscovery(unittest.TestCase):
    def test_finds_bouncer_dir_in_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            bd = (tmp_path / ".bouncer")
            bd.mkdir()
            self.assertEqual(_find_bouncer_dir(tmp_path), bd)

    def test_finds_bouncer_dir_walking_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            bd = (tmp_path / ".bouncer")
            bd.mkdir()
            nested = tmp_path / "a" / "b" / "c"
            nested.mkdir(parents=True)
            self.assertEqual(_find_bouncer_dir(nested), bd)

    def test_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_find_bouncer_dir(Path(tmp)))

    def test_project_has_bouncer_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _make_bouncer_dir(tmp_path, config_yaml="enabled: true\n")
            self.assertTrue(project_has_bouncer(tmp_path))

    def test_project_has_bouncer_false_policy_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _make_bouncer_dir(tmp_path, policy_md="some policy")
            self.assertFalse(project_has_bouncer(tmp_path))

    def test_project_has_bouncer_false_no_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(project_has_bouncer(Path(tmp)))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class TestLogging(unittest.TestCase):
    def test_should_log_all_logs_everything(self):
        config = {"log": {"verbosity": "all"}}
        for decision in ("ALLOW", "DENY", "UNSURE", "ESCALATE"):
            self.assertTrue(_should_log(decision, config), decision)

    def test_should_log_deny_only(self):
        config = {"log": {"verbosity": "deny_only"}}
        self.assertTrue(_should_log("DENY", config))
        self.assertTrue(_should_log("BLOCK", config))
        self.assertFalse(_should_log("ALLOW", config))
        self.assertFalse(_should_log("UNSURE", config))

    def test_should_log_off_logs_nothing(self):
        config = {"log": {"verbosity": "off"}}
        for decision in ("ALLOW", "DENY", "UNSURE"):
            self.assertFalse(_should_log(decision, config), decision)

    def test_prune_removes_oldest_entries(self):
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as f:
            path = Path(f.name)
            # Use larger entries to exceed the size guard (max_entries * 300 * 1.2)
            # 100 * 300 * 1.2 = 36,000 bytes. 
            # 200 entries * 400 bytes = 80,000 bytes.
            padding = "x" * 400
            for i in range(200):
                f.write(f'{{"n":{i}, "pad":"{padding}"}}\n'.encode())
        try:
            _maybe_prune_log(path, 100)
            lines = [ln for ln in path.read_bytes().split(b"\n") if ln]
            self.assertEqual(len(lines), 100)
            self.assertEqual(json.loads(lines[0])["n"], 100)
            self.assertEqual(json.loads(lines[-1])["n"], 199)
        finally:
            path.unlink()

    def test_prune_no_op_under_limit(self):
        with tempfile.NamedTemporaryFile(mode="wb", delete=False) as f:
            path = Path(f.name)
            for i in range(50):
                f.write(f'{{"n":{i}}}\n'.encode())
        try:
            _maybe_prune_log(path, 100)
            lines = [ln for ln in path.read_bytes().split(b"\n") if ln]
            self.assertEqual(len(lines), 50)
        finally:
            path.unlink()

    def test_prune_no_op_on_missing_file(self):
        _maybe_prune_log(Path("/nonexistent/log.jsonl"), 100)

    def test_log_decision_writes_correct_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            with patch.object(cfg, "USER_LOG_FILE", log_path):
                log_decision("Bash", {"command": "ls"}, "/tmp",
                             "ALLOW", "test reason", cfg=None)
            entry = json.loads(log_path.read_text())
        self.assertEqual(entry["tool"], "Bash")
        self.assertEqual(entry["decision"], "ALLOW")
        self.assertEqual(entry["reason"], "test reason")
        self.assertIn("command", entry["input_summary"])
        self.assertIn("timestamp", entry)

    def test_log_decision_verbosity_off_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            config = {"log": {"verbosity": "off"}}
            with patch.object(cfg, "USER_LOG_FILE", log_path):
                log_decision("Bash", {}, "/tmp", "ALLOW", "fine", cfg=config)
            self.assertFalse(log_path.exists())

    def test_log_decision_deny_only_skips_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            config = {"log": {"verbosity": "deny_only"}}
            with patch.object(cfg, "USER_LOG_FILE", log_path):
                log_decision("Bash", {}, "/tmp", "ALLOW", "fine", cfg=config)
                log_decision("Bash", {}, "/tmp", "DENY",  "bad",  cfg=config)
            lines = [ln for ln in log_path.read_text().splitlines() if ln]
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["decision"], "DENY")

    def test_log_decision_always_writes_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            config = {"log": {"verbosity": "off"}}
            with patch.object(cfg, "USER_LOG_FILE", log_path):
                log_decision("Bash", {}, "/tmp", "PENDING", "calling LLM",
                             cfg=config)
            self.assertTrue(log_path.exists())

    def test_log_decision_writes_to_proj_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user_log = tmp_path / "user.jsonl"
            proj_log = tmp_path / "proj.jsonl"
            with patch.object(cfg, "USER_LOG_FILE", user_log):
                log_decision("Bash", {}, "/tmp", "ALLOW", "ok",
                             cfg=None, proj_log=proj_log)
            self.assertTrue(user_log.exists())
            self.assertTrue(proj_log.exists())


# ---------------------------------------------------------------------------
# cmd_classify
# ---------------------------------------------------------------------------

_BASIC_CONFIG = "enabled: true\ntools:\n  - Bash\n"


class TestClassify(unittest.TestCase):
    def _hook(self, tool="Bash", command="ls /tmp"):
        return {"tool_name": tool, "tool_input": {"command": command}}

    def test_no_project_config_passes_through(self):
        _, _, code = _classify(self._hook(), call_llm_result=("DENY", "bad"))
        self.assertEqual(code, 0)

    def test_disabled_passes_through(self):
        _, _, code = _classify(
            self._hook(),
            config_yaml="enabled: false\n",
            call_llm_result=("DENY", "bad"),
        )
        self.assertEqual(code, 0)

    def test_tool_not_in_list_passes_through(self):
        _, _, code = _classify(
            {"tool_name": "Write", "tool_input": {"path": "/f", "content": "x"}},
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("DENY", "bad"),
        )
        self.assertEqual(code, 0)

    def test_tools_all_intercepts_non_bash(self):
        _, _, code = _classify(
            {"tool_name": "Write", "tool_input": {}},
            config_yaml="enabled: true\ntools: all\n",
            call_llm_result=("ALLOW", "fine"),
        )
        self.assertEqual(code, 0)

    def test_tools_empty_list_passes_through(self):
        _, _, code = _classify(
            self._hook(),
            config_yaml="enabled: true\ntools: []\n",
            call_llm_result=("DENY", "bad"),
        )
        self.assertEqual(code, 0)

    def test_allow_exits_0_with_permission_grant(self):
        out, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("ALLOW", "looks fine"),
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(
            data["hookSpecificOutput"]["permissionDecision"], "allow"
        )

    def test_deny_exits_2_with_reason_in_stderr(self):
        _, err, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("DENY", "dangerous command"),
        )
        self.assertEqual(code, 2)
        self.assertIn("dangerous command", err)

    def test_deny_stderr_includes_escalate_hint(self):
        _, err, _ = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("DENY", "bad"),
        )
        self.assertIn("ESCALATE", err)

    def test_unsure_default_asks_human(self):
        out, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("UNSURE", "unclear"),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_unsure_on_unsure_allow(self):
        _, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG + "on_unsure: allow\n",
            call_llm_result=("UNSURE", "unclear"),
        )
        self.assertEqual(code, 0)

    def test_unsure_on_unsure_deny(self):
        _, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG + "on_unsure: deny\n",
            call_llm_result=("UNSURE", "unclear"),
        )
        self.assertEqual(code, 2)

    def test_unavailable_default_asks_human(self):
        out, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=(None, "Ollama unreachable"),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_unavailable_on_unavailable_allow(self):
        _, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG + "on_unavailable: allow\n",
            call_llm_result=(None, "Ollama unreachable"),
        )
        self.assertEqual(code, 0)

    def test_unavailable_on_unavailable_deny(self):
        _, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG + "on_unavailable: deny\n",
            call_llm_result=(None, "Ollama unreachable"),
        )
        self.assertEqual(code, 2)

    def test_escalate_prefix_produces_ask(self):
        out, _, code = _classify(
            self._hook(command="# ESCALATE: needed for deploy\nrm -rf dist/"),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("DENY", "should not be called"),
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        reason = data["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("escalat", reason.lower())

    def test_escalate_does_not_call_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _make_bouncer_dir(tmp_path, config_yaml=_BASIC_CONFIG)
            user_dir = tmp_path / "user"
            user_dir.mkdir()
            hook_input = dict(
                self._hook(command="# ESCALATE: test\nls"),
                cwd=str(tmp_path),
            )
            with (
                patch.object(cfg, "USER_CONFIG_FILE", user_dir / "config.yaml"),
                patch.object(cfg, "USER_POLICY_FILE", user_dir / "policy.md"),
                patch.object(cfg, "USER_LOG_FILE",    user_dir / "log.jsonl"),
                patch.object(classify_mod, "call_llm") as mock_llm,
                patch("sys.stdin", io.StringIO(json.dumps(hook_input))),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                class _A:
                    hook = True
                    format = "json"
                try:
                    cmd_classify(_A())
                except SystemExit:
                    pass
                mock_llm.assert_not_called()

    def test_escalate_reason_in_ask_message(self):
        out, _, _ = _classify(
            self._hook(command="# ESCALATE: deploy step\nrm -rf dist/"),
            config_yaml=_BASIC_CONFIG,
        )
        reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("deploy step", reason)

    def test_invalid_stdin_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user_dir = tmp_path / "user"
            user_dir.mkdir()
            with (
                patch.object(cfg, "USER_CONFIG_FILE", user_dir / "config.yaml"),
                patch.object(cfg, "USER_POLICY_FILE", user_dir / "policy.md"),
                patch.object(cfg, "USER_LOG_FILE",    user_dir / "log.jsonl"),
                patch("sys.stdin", io.StringIO("not valid json")),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                class _A:
                    hook = True
                    format = "json"
                with self.assertRaises(SystemExit) as cm:
                    cmd_classify(_A())
                self.assertEqual(cm.exception.code, 0)


# ---------------------------------------------------------------------------
# cmd_lint
# ---------------------------------------------------------------------------

class TestLint(unittest.TestCase):
    def test_valid_minimal(self):
        _, code = _lint("enabled: true\n")
        self.assertEqual(code, 0)

    def test_valid_full_template(self):
        _, code = _lint(CONFIG_YAML_TEMPLATE)
        self.assertEqual(code, 0)

    def test_unknown_key_warns_no_error(self):
        out, code = _lint("enabled: true\nfoo_unknown: bar\n")
        self.assertEqual(code, 0)
        self.assertIn("Unknown key", out)

    def test_invalid_policy_mode(self):
        _, code = _lint("policy_mode: badvalue\n")
        self.assertEqual(code, 1)

    def test_invalid_on_unsure(self):
        _, code = _lint("on_unsure: maybe\n")
        self.assertEqual(code, 1)

    def test_invalid_on_unavailable(self):
        _, code = _lint("on_unavailable: shrug\n")
        self.assertEqual(code, 1)

    def test_tools_all_is_valid(self):
        _, code = _lint("tools: all\n")
        self.assertEqual(code, 0)

    def test_tools_empty_list_is_valid(self):
        _, code = _lint("tools: []\n")
        self.assertEqual(code, 0)

    def test_tools_list_is_valid(self):
        _, code = _lint("tools:\n  - Bash\n  - Write\n")
        self.assertEqual(code, 0)

    def test_invalid_log_verbosity(self):
        _, code = _lint("log:\n  verbosity: verbose\n")
        self.assertEqual(code, 1)

    def test_valid_log_settings(self):
        _, code = _lint("log:\n  verbosity: deny_only\n  max_entries: 5000\n")
        self.assertEqual(code, 0)

    def test_valid_llm_block(self):
        yaml = "llm:\n  provider: ollama\n  model: llama3\n  timeout: 30\n"
        _, code = _lint(yaml)
        self.assertEqual(code, 0)

    def test_disabled_project_is_valid(self):
        _, code = _lint("enabled: false\n")
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
