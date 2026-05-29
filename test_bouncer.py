#!/usr/bin/env python3
"""Tests for the bouncer package."""

import builtins
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
import bouncer.commands.init as init_mod
import bouncer.providers as providers_mod
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
from bouncer.log import _should_log, _maybe_prune_log, log_decision, log_llm_debug
from bouncer.activity import _render_activity
from bouncer.commands.lint import cmd_lint
from bouncer.commands.activity import cmd_activity
from bouncer.commands.classify import cmd_classify
from bouncer.commands.log import _extract_command
from bouncer.commands.check import cmd_check
from bouncer.providers import _parse_llm_text
from bouncer.llmclient.providers.openai import _extract_text as _extract_response_text
from bouncer.llmclient.providers.ollama import _get_loaded_ctx, call_ollama
from bouncer.llmclient import LLMConfig


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


def _classify(hook_input, *, call_llm_result=("ALLOW", "ok", None, None),
              config_yaml=None, policy_md=None, fmt="json"):
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
                format = fmt
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
        self.assertIsInstance(result, (dict, type(None)))

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
        self.assertNotIn("model", config["llm"])  # no default; must be set in user config

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
        self.assertEqual(config["llm"]["timeout"], 30)


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

    def test_log_decision_continues_if_user_log_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user_log = tmp_path / "user.jsonl"
            proj_log = tmp_path / "proj.jsonl"
            real_open = builtins.open

            def fake_open(path, *args, **kwargs):
                if Path(path) == user_log:
                    raise PermissionError("sandbox")
                return real_open(path, *args, **kwargs)

            with (
                patch.object(cfg, "USER_LOG_FILE", user_log),
                patch("builtins.open", fake_open),
            ):
                log_decision("Bash", {}, "/tmp", "ALLOW", "ok",
                             cfg=None, proj_log=proj_log)

            self.assertFalse(user_log.exists())
            self.assertTrue(proj_log.exists())

    def test_log_llm_debug_redacts_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bouncer_dir = tmp_path / ".bouncer"
            bouncer_dir.mkdir()
            debug_log = bouncer_dir / "llm_debug.jsonl"
            with patch.object(cfg, "_find_bouncer_dir", return_value=bouncer_dir):
                log_llm_debug(
                    str(tmp_path),
                    {"log": {"llm_debug": True}},
                    "openai_compatible",
                    "gpt-oss-120b",
                    {
                        "url": "https://example.test/v1/chat/completions",
                        "headers": {"Authorization": "Bearer secret", "Content-Type": "application/json"},
                        "body": {"model": "gpt-oss-120b"},
                    },
                    response_body={"choices": []},
                    response_text="DECISION: ALLOW\nREASON: ok",
                )
            entry = json.loads(debug_log.read_text())
            self.assertEqual(entry["request"]["headers"]["Authorization"], "Bearer ***REDACTED***")
            self.assertEqual(entry["response_text"], "DECISION: ALLOW\nREASON: ok")


# ---------------------------------------------------------------------------
# activity
# ---------------------------------------------------------------------------

class TestActivity(unittest.TestCase):
    def test_render_activity_tmux_format(self):
        out = _render_activity(
            [
                {"d": "ALLOW", "t": "Bash"},
                {"d": "UNSURE", "t": "Bash"},
                {"d": "DENY", "t": "Bash"},
                {"d": "ESCALATE", "t": "Bash"},
            ],
            as_format="tmux",
        )
        self.assertIn("#[fg=green]B#[default]", out)
        self.assertIn("#[fg=yellow]B#[default]", out)
        self.assertIn("#[bg=red,fg=black,bold]B#[default]", out)
        self.assertIn("#[fg=blue]B#[default]", out)

    def test_project_activity_reads_project_log_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bouncer_dir = _make_bouncer_dir(
                tmp_path,
                config_yaml="enabled: true\ntools:\n  - Bash\n",
            )
            log_path = bouncer_dir / "log.jsonl"
            rows = [
                {"tool": "Bash", "decision": "ALLOW"},
                {"tool": "Bash", "decision": "PENDING"},
                {"tool": "Bash", "decision": "DENY"},
                {"tool": "Bash", "decision": "UNSURE"},
            ]
            log_path.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )

            class _A:
                width = 2
                session = None
                cwd = str(tmp_path)
                as_format = "plain"
                project = True

            stdout_buf = io.StringIO()
            with redirect_stdout(stdout_buf):
                cmd_activity(_A())

        self.assertEqual(stdout_buf.getvalue(), "BB")

    def test_project_activity_tmux_marker_for_active_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _make_bouncer_dir(tmp_path, config_yaml="enabled: true\n")

            class _A:
                width = 2
                session = None
                cwd = str(tmp_path)
                as_format = "tmux"
                project = True

            stdout_buf = io.StringIO()
            with redirect_stdout(stdout_buf):
                cmd_activity(_A())

        self.assertEqual(stdout_buf.getvalue(), "#[dim]○#[default]")


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

class TestInit(unittest.TestCase):
    def test_codex_install_uses_permission_request_not_pretool_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            codex_dir = home / ".codex"
            hooks_dir = codex_dir / "hooks"
            hooks_dir.mkdir(parents=True)
            hooks_json = codex_dir / "hooks.json"
            installed_hook = hooks_dir / "bouncer_hook.py"
            hooks_json.write_text(
                json.dumps({
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": str(installed_hook),
                                        "timeout": 30,
                                    }
                                ],
                            }
                        ]
                    }
                }),
                encoding="utf-8",
            )

            with (
                patch.object(init_mod.Path, "home", return_value=home),
                redirect_stdout(io.StringIO()),
            ):
                init_mod._install_codex()

            cfg_data = json.loads(hooks_json.read_text(encoding="utf-8"))
            hooks = cfg_data["hooks"]
            self.assertNotIn("PreToolUse", hooks)
            permission = hooks["PermissionRequest"]
            self.assertEqual(permission[0]["matcher"], "Bash")
            command = permission[0]["hooks"][0]["command"]
            self.assertEqual(command, str(installed_hook))
            self.assertIn(
                "codex-permission",
                installed_hook.read_text(encoding="utf-8"),
            )


# ---------------------------------------------------------------------------
# _extract_command (commands/log.py)
# ---------------------------------------------------------------------------

class TestExtractCommand(unittest.TestCase):
    def test_plain_command(self):
        summary = json.dumps({"command": "ls -la"})
        self.assertEqual(_extract_command(summary), "ls -la")

    def test_command_with_single_quotes(self):
        summary = json.dumps({"command": "echo \"fix user's bug\""})
        self.assertEqual(_extract_command(summary), "echo \"fix user's bug\"")

    def test_command_with_double_quotes(self):
        summary = json.dumps({"command": 'grep "needle" file.txt'})
        self.assertEqual(_extract_command(summary), 'grep "needle" file.txt')

    def test_no_command_key(self):
        summary = json.dumps({"path": "/tmp/foo", "content": "x"})
        self.assertEqual(_extract_command(summary), summary)

    def test_non_json_fallback(self):
        legacy = "{'command': 'ls'}"
        self.assertEqual(_extract_command(legacy), legacy)


# ---------------------------------------------------------------------------
# cmd_classify
# ---------------------------------------------------------------------------

_BASIC_CONFIG = "enabled: true\ntools:\n  - Bash\n"


class TestClassify(unittest.TestCase):
    def _hook(self, tool="Bash", command="ls /tmp"):
        return {"tool_name": tool, "tool_input": {"command": command}}

    def test_no_project_config_passes_through(self):
        _, _, code = _classify(self._hook(), call_llm_result=("DENY", "bad", None, None))
        self.assertEqual(code, 0)

    def test_disabled_passes_through(self):
        _, _, code = _classify(
            self._hook(),
            config_yaml="enabled: false\n",
            call_llm_result=("DENY", "bad", None, None),
        )
        self.assertEqual(code, 0)

    def test_tool_not_in_list_passes_through(self):
        _, _, code = _classify(
            {"tool_name": "Write", "tool_input": {"path": "/f", "content": "x"}},
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("DENY", "bad", None, None),
        )
        self.assertEqual(code, 0)

    def test_tools_all_intercepts_non_bash(self):
        _, _, code = _classify(
            {"tool_name": "Write", "tool_input": {}},
            config_yaml="enabled: true\ntools: all\n",
            call_llm_result=("ALLOW", "fine", None, None),
        )
        self.assertEqual(code, 0)

    def test_tools_empty_list_passes_through(self):
        _, _, code = _classify(
            self._hook(),
            config_yaml="enabled: true\ntools: []\n",
            call_llm_result=("DENY", "bad", None, None),
        )
        self.assertEqual(code, 0)

    def test_allow_exits_0_with_permission_grant(self):
        out, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("ALLOW", "looks fine", None, None),
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
            call_llm_result=("DENY", "dangerous command", None, None),
        )
        self.assertEqual(code, 2)
        self.assertIn("dangerous command", err)

    def test_deny_stderr_includes_escalate_hint(self):
        _, err, _ = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("DENY", "bad", None, None),
        )
        self.assertIn("ESCALATE", err)

    def test_unsure_default_asks_human(self):
        out, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("UNSURE", "unclear", None, None),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_unsure_plain_denies_when_ask_not_available(self):
        out, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("UNSURE", "unclear", None, None),
            fmt="plain",
        )
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("deny\tLLM unsure: unclear"))
        self.assertIn("does not have ASK available", out)
        self.assertNotIn("To escalate to the user", out)

    def test_codex_pretool_allow_is_silent_exit_0(self):
        out, err, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("ALLOW", "looks fine", None, None),
            fmt="codex-pretool",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_codex_pretool_unsure_passes_through(self):
        out, err, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("UNSURE", "unclear", None, None),
            fmt="codex-pretool",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_codex_permission_allow_auto_approves(self):
        out, err, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("ALLOW", "looks fine", None, None),
            fmt="codex-permission",
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        data = json.loads(out)
        output = data["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PermissionRequest")
        self.assertEqual(output["decision"]["behavior"], "allow")

    def test_codex_permission_deny_blocks_approval_request(self):
        out, err, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("DENY", "not in policy", None, None),
            fmt="codex-permission",
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        data = json.loads(out)
        output = data["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PermissionRequest")
        self.assertEqual(output["decision"]["behavior"], "deny")
        self.assertEqual(output["decision"]["message"], "not in policy")

    def test_codex_permission_unsure_abstains_so_codex_asks(self):
        out, err, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("UNSURE", "unclear", None, None),
            fmt="codex-permission",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_unsure_on_unsure_allow(self):
        _, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG + "on_unsure: allow\n",
            call_llm_result=("UNSURE", "unclear", None, None),
        )
        self.assertEqual(code, 0)

    def test_unsure_on_unsure_deny(self):
        _, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG + "on_unsure: deny\n",
            call_llm_result=("UNSURE", "unclear", None, None),
        )
        self.assertEqual(code, 2)

    def test_unavailable_default_asks_human(self):
        out, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=(None, "Ollama unreachable", None, None),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_unavailable_on_unavailable_allow(self):
        _, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG + "on_unavailable: allow\n",
            call_llm_result=(None, "Ollama unreachable", None, None),
        )
        self.assertEqual(code, 0)

    def test_unavailable_on_unavailable_deny(self):
        _, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG + "on_unavailable: deny\n",
            call_llm_result=(None, "Ollama unreachable", None, None),
        )
        self.assertEqual(code, 2)

    def test_bouncer_help_commands_skip_without_llm(self):
        for cmd in ("bouncer --agent-help", "bouncer --help", "bouncer -h"):
            with self.subTest(cmd=cmd):
                _, _, code = _classify(
                    self._hook(command=cmd),
                    config_yaml=_BASIC_CONFIG,
                    call_llm_result=("DENY", "should not be called", None, None),
                )
                self.assertEqual(code, 0)

    def test_escalate_prefix_produces_ask(self):
        out, _, code = _classify(
            self._hook(command="# ESCALATE: needed for deploy\nrm -rf dist/"),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("DENY", "should not be called", None, None),
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

    def test_escalate_plain_denies_when_ask_not_available(self):
        out, _, code = _classify(
            self._hook(command="# ESCALATE: deploy step\nrm -rf dist/"),
            config_yaml=_BASIC_CONFIG,
            fmt="plain",
        )
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("deny\tagent escalation requested: deploy step"))
        self.assertIn("does not have ASK available", out)

    def test_escalate_codex_permission_abstains_so_codex_asks(self):
        out, err, code = _classify(
            self._hook(command="# ESCALATE: deploy step\nrm -rf dist/"),
            config_yaml=_BASIC_CONFIG,
            fmt="codex-permission",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_log_decision_records_elapsed_and_prompt_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            with patch.object(cfg, "USER_LOG_FILE", log_path):
                log_decision("Bash", {"command": "ls"}, "/tmp",
                             "ALLOW", "ok", cfg={"log": {}},
                             elapsed_s=3.142, prompt_chars=1234)
            entry = json.loads(log_path.read_text())
        self.assertAlmostEqual(entry["elapsed_s"], 3.142, places=3)
        self.assertEqual(entry["prompt_chars"], 1234)

    def test_log_decision_omits_fields_when_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            with patch.object(cfg, "USER_LOG_FILE", log_path):
                log_decision("Bash", {"command": "ls"}, "/tmp",
                             "ALLOW", "ok", cfg=None)
            entry = json.loads(log_path.read_text())
        self.assertNotIn("elapsed_s", entry)
        self.assertNotIn("prompt_chars", entry)

    def test_classify_writes_elapsed_to_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user_dir = tmp_path / "user"
            user_dir.mkdir()
            _make_bouncer_dir(tmp_path, config_yaml=_BASIC_CONFIG)
            hook_input = {"tool_name": "Bash", "tool_input": {"command": "ls"},
                          "cwd": str(tmp_path)}
            log_path = user_dir / "log.jsonl"
            with (
                patch.object(cfg, "USER_CONFIG_FILE", user_dir / "config.yaml"),
                patch.object(cfg, "USER_POLICY_FILE", user_dir / "policy.md"),
                patch.object(cfg, "USER_LOG_FILE", log_path),
                patch.object(classify_mod, "call_llm",
                             return_value=("ALLOW", "fine", 500, None)),
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
            lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
            final = next(e for e in lines if e["decision"] != "PENDING")
        self.assertIn("elapsed_s", final)
        self.assertEqual(final["prompt_chars"], 500)

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


class TestParseLlmText(unittest.TestCase):
    def test_strict_format_parses(self):
        decision, reason = _parse_llm_text("DECISION: ALLOW\nREASON: harmless read")
        self.assertEqual((decision, reason), ("ALLOW", "harmless read"))

    def test_malformed_output_falls_back_to_unsure(self):
        decision, reason = _parse_llm_text("pwd is allowed because it is read-only")
        self.assertEqual(decision, "UNSURE")
        self.assertIn("does not match expected format", reason)


class TestCallLlm(unittest.TestCase):
    def test_llm_extra_params_override_classifier_defaults(self):
        captured = {}

        class FakeClient:
            def __init__(self, llm_cfg, abort_event=None):
                captured["cfg"] = llm_cfg

            def call(self, user, system=""):
                class Result:
                    text = "DECISION: ALLOW\nREASON: ok"
                    outcome = "success"
                    prompt_chars = len(user) + len(system)
                    prompt_tokens = None
                    call_s = 0.1
                    queue_snapshot = None
                return Result()

        config = {
            "llm": {
                "provider": "openai_compatible",
                "model": "reasoning-test-model",
                "extra_params": {
                    "max_tokens": 4096,
                    "temperature": 0.2,
                },
            }
        }

        with patch("bouncer.llmclient.LLMClient", FakeClient):
            decision, reason, _, _snap = providers_mod.call_llm(
                "Bash", {"command": "pwd"}, Path("/tmp/project"), config,
            )

        self.assertEqual((decision, reason), ("ALLOW", "ok"))
        self.assertEqual(captured["cfg"].extra_params["max_tokens"], 4096)
        self.assertEqual(captured["cfg"].extra_params["num_predict"], 80)
        self.assertEqual(captured["cfg"].extra_params["temperature"], 0.2)

    def test_openai_compatible_classifier_uses_larger_default_token_budget(self):
        captured = {}

        class FakeClient:
            def __init__(self, llm_cfg, abort_event=None):
                captured["cfg"] = llm_cfg

            def call(self, user, system=""):
                class Result:
                    text = "DECISION: ALLOW\nREASON: ok"
                    outcome = "success"
                    prompt_chars = len(user) + len(system)
                    prompt_tokens = None
                    call_s = 0.1
                    queue_snapshot = None
                return Result()

        config = {
            "llm": {
                "provider": "openai_compatible",
                "model": "reasoning-test-model",
            }
        }

        with patch("bouncer.llmclient.LLMClient", FakeClient):
            decision, reason, _, _snap = providers_mod.call_llm(
                "Bash", {"command": "pwd"}, Path("/tmp/project"), config,
            )

        self.assertEqual((decision, reason), ("ALLOW", "ok"))
        self.assertEqual(captured["cfg"].extra_params["max_tokens"], 1024)
        self.assertEqual(captured["cfg"].extra_params["num_predict"], 80)


class TestCheckCommand(unittest.TestCase):
    def test_check_llm_accepts_current_call_llm_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _make_bouncer_dir(tmp_path, config_yaml="llm:\n  model: test-model\n")

            class _A:
                cmd = "pwd"
                llm = True

            stdout_buf = io.StringIO()
            with (
                patch.object(cfg, "USER_CONFIG_FILE", tmp_path / "no_user.yaml"),
                patch("bouncer.commands.check.call_llm",
                      return_value=("ALLOW", "ok", 12, None)),
                patch("pathlib.Path.cwd", return_value=tmp_path),
                redirect_stdout(stdout_buf),
            ):
                cmd_check(_A())

        output = stdout_buf.getvalue()
        self.assertIn("ALLOW", output)
        self.assertIn("ok", output)


class TestOllamaProvider(unittest.TestCase):
    def _make_ps_response(self, models):
        body = json.dumps({"models": models}).encode()
        return unittest.mock.MagicMock(
            read=lambda: body,
            __enter__=lambda s: s,
            __exit__=lambda s, *a: False,
        )

    def test_get_loaded_ctx_returns_context_length(self):
        resp = self._make_ps_response([
            {"model": "qwen3:32b", "context_length": 8192}
        ])
        with patch("urllib.request.urlopen", return_value=resp):
            result = _get_loaded_ctx("http://localhost:11434", "qwen3:32b")
        self.assertEqual(result, 8192)

    def test_get_loaded_ctx_matches_by_prefix(self):
        resp = self._make_ps_response([
            {"model": "qwen3:32b-instruct", "context_length": 4096}
        ])
        with patch("urllib.request.urlopen", return_value=resp):
            result = _get_loaded_ctx("http://localhost:11434", "qwen3:32b")
        self.assertEqual(result, 4096)

    def test_get_loaded_ctx_returns_none_on_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = _get_loaded_ctx("http://localhost:11434", "qwen3:32b")
        self.assertIsNone(result)

    def test_num_ctx_ratchet_keeps_larger_loaded_value(self):
        cfg = LLMConfig(
            provider="ollama", model="qwen3:32b",
            extra_params={"num_ctx": 4096, "num_predict": 80},
        )
        captured = {}

        def fake_urlopen(req, timeout=None):
            if req.full_url.endswith("/api/ps"):
                body = json.dumps({
                    "models": [{"model": "qwen3:32b", "context_length": 16384}]
                }).encode()
                return unittest.mock.MagicMock(
                    read=lambda: body,
                    __enter__=lambda s: s,
                    __exit__=lambda s, *a: False,
                )
            # /api/generate
            captured["payload"] = json.loads(req.data)
            body = json.dumps({"response": "DECISION: ALLOW\nREASON: ok",
                               "eval_count": 10, "prompt_eval_count": 50,
                               "load_duration": 0, "eval_duration": 1e8}).encode()
            return unittest.mock.MagicMock(
                read=lambda: body,
                __enter__=lambda s: s,
                __exit__=lambda s, *a: False,
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            call_ollama("system", "user", cfg, "http://localhost:11434", None)

        sent_ctx = captured["payload"]["options"]["num_ctx"]
        self.assertEqual(sent_ctx, 16384)

    def test_num_ctx_ratchet_keeps_requested_when_larger(self):
        cfg = LLMConfig(
            provider="ollama", model="qwen3:32b",
            extra_params={"num_ctx": 32768, "num_predict": 80},
        )
        captured = {}

        def fake_urlopen(req, timeout=None):
            if req.full_url.endswith("/api/ps"):
                body = json.dumps({
                    "models": [{"model": "qwen3:32b", "context_length": 8192}]
                }).encode()
                return unittest.mock.MagicMock(
                    read=lambda: body,
                    __enter__=lambda s: s,
                    __exit__=lambda s, *a: False,
                )
            captured["payload"] = json.loads(req.data)
            body = json.dumps({"response": "DECISION: ALLOW\nREASON: ok",
                               "eval_count": 10, "prompt_eval_count": 50,
                               "load_duration": 0, "eval_duration": 1e8}).encode()
            return unittest.mock.MagicMock(
                read=lambda: body,
                __enter__=lambda s: s,
                __exit__=lambda s, *a: False,
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            call_ollama("system", "user", cfg, "http://localhost:11434", None)

        sent_ctx = captured["payload"]["options"]["num_ctx"]
        self.assertEqual(sent_ctx, 32768)


class TestOpenAIProvider(unittest.TestCase):
    def test_extract_response_text_uses_message_content(self):
        body = {
            "choices": [{
                "message": {
                    "content": "DECISION: ALLOW\nREASON: harmless read",
                    "reasoning_content": "internal reasoning",
                }
            }]
        }
        self.assertEqual(
            _extract_response_text(body),
            "DECISION: ALLOW\nREASON: harmless read",
        )

    def test_extract_response_text_ignores_reasoning_content(self):
        body = {
            "choices": [{
                "message": {
                    "content": None,
                    "reasoning_content": "DECISION: ALLOW\nREASON: should not be used",
                    "reasoning": "same here",
                }
            }]
        }
        with self.assertRaisesRegex(ValueError, "missing textual content"):
            _extract_response_text(body)




if __name__ == "__main__":
    unittest.main(verbosity=2)
