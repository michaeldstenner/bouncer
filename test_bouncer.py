#!/usr/bin/env python3
"""Tests for the bouncer package."""

import builtins
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

import bouncer.config as cfg
import bouncer.classify as classify_mod
import bouncer.escalation_cache as escalation_mod
import bouncer.escalation_grant as grant_mod
import bouncer.profile as profile_mod
import bouncer.hook as hook_mod
import bouncer.commands.lint as lint_mod
import bouncer.commands.profile as profile_cmd
from bouncer.commands.profile import effective_state
import bouncer.commands.init as init_mod
import bouncer.notify as notify_mod
import bouncer.providers as providers_mod
import bouncer.tool_catalog as tool_catalog_mod
from bouncer.yaml import MicroYAML
from bouncer.config import (
    _deep_merge,
    load_yaml_config,
    load_policy,
    _find_bouncer_dir,
    _merged_config,
    _build_policy_context,
    project_has_bouncer,
    CONFIG_DEFAULTS,
    CONFIG_YAML_TEMPLATE,
    USER_CONFIG_YAML_TEMPLATE,
)
from bouncer.log import (
    _log_mode,
    _maybe_prune_log,
    log_decision,
    log_break,
    log_llm_debug,
)
from bouncer.notify import notify_decision
from bouncer.activity import _render_activity
from bouncer.commands.lint import cmd_lint
from bouncer.commands.activity import cmd_activity
from bouncer.commands.status import cmd_status
from bouncer.commands.classify import cmd_classify, _infer_harness
from bouncer.commands.log import _extract_command, cmd_log
from bouncer.commands.check import cmd_check
from bouncer.commands.tools import cmd_tools
from bouncer.providers import _parse_llm_text
from bouncer.llmclient.providers.openai import (
    _extract_text as _extract_response_text,
    call_openai,
)
from bouncer.llmclient.providers.ollama import _get_loaded_ctx, call_ollama
from bouncer.llmclient import LLMConfig, configure as llmclient_configure
from bouncer.llmclient._keys import resolve_api_key, resolve_url


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

# Bouncer keeps per-project file state under ~/.local/share/bouncer/. Tests
# that drive the hook directly (rather than through the _classify helper)
# would otherwise write into the developer's real state dir — harmless-looking
# but real, and the profile state is written on every classified call. Redirect
# the whole module at those dirs' temp equivalents; individual tests still
# patch over this when they need a shared dir of their own.
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
              call_llm_exc=None, config_yaml=None, policy_md=None, fmt="json",
              escalation_dir=None, profile_dir=None):
    """
    Run cmd_classify with patched stdin/paths/LLM.
    Returns (stdout_str, stderr_str, exit_code).

    escalation_dir: where the escalation-attempt cache lives. Defaults to a
    fresh dir under the per-call temp (so calls are isolated and real $HOME is
    never touched); pass a shared path to exercise attempt-then-escalate flows.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        esc_dir = Path(escalation_dir) if escalation_dir else tmp_path / "escalation"
        prof_dir = Path(profile_dir) if profile_dir else tmp_path / "profile"

        if config_yaml is not None or policy_md is not None:
            _make_bouncer_dir(tmp_path, config_yaml, policy_md)

        full_input = dict(hook_input, cwd=hook_input.get("cwd", str(tmp_path)))
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        exit_code  = 0

        call_llm_patch = (
            patch.object(classify_mod, "call_llm", side_effect=call_llm_exc)
            if call_llm_exc is not None
            else patch.object(classify_mod, "call_llm", return_value=call_llm_result)
        )

        with (
            patch.object(cfg, "USER_CONFIG_FILE", user_dir / "config.yaml"),
            patch.object(cfg, "USER_POLICY_FILE", user_dir / "policy.md"),
            patch.object(cfg, "USER_LOG_FILE",    user_dir / "log.jsonl"),
            patch.object(escalation_mod, "ESCALATION_DIR", esc_dir),
            patch.object(grant_mod, "GRANT_DIR", esc_dir),
            patch.object(profile_mod, "PROFILE_DIR", prof_dir),
            call_llm_patch,
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

    def test_patient_gate_defaults(self):
        # Under circuit_mode=futility, deadline_s owns the call end to end
        # (queue wait + active call), so the timeouts and count-mode knobs it
        # supersedes must not be set — they'd only re-introduce early bails or
        # are simply ignored.
        llm = CONFIG_DEFAULTS["llm"]
        self.assertEqual(llm["deadline_s"], 180)
        self.assertEqual(llm["caller_max"], 4)
        self.assertEqual(llm["circuit_mode"], "futility")
        for ignored in ("first_token_timeout", "generation_timeout",
                        "queue_timeout", "circuit_n"):
            self.assertNotIn(ignored, llm)


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
        self.assertEqual(config["tools"], "all")
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
    def test_log_mode_all_is_full(self):
        config = {"log": {"verbosity": "all"}}
        for decision in ("ALLOW", "DENY", "UNSURE", "ESCALATE", "BREAK"):
            self.assertEqual(_log_mode(decision, config), "full", decision)

    def test_log_mode_deny_only_compacts_non_denials(self):
        config = {"log": {"verbosity": "deny_only"}}
        self.assertEqual(_log_mode("DENY", config), "full")
        self.assertEqual(_log_mode("BLOCK", config), "full")
        # Non-denials (and breaks) stay in the log as compact markers so the
        # activity strip survives a filtered verbosity.
        self.assertEqual(_log_mode("ALLOW", config), "compact")
        self.assertEqual(_log_mode("UNSURE", config), "compact")
        self.assertEqual(_log_mode("BREAK", config), "compact")

    def test_log_mode_off_skips_everything(self):
        config = {"log": {"verbosity": "off"}}
        for decision in ("ALLOW", "DENY", "UNSURE", "BREAK"):
            self.assertEqual(_log_mode(decision, config), "skip", decision)

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

    def test_log_decision_deny_only_writes_compact_allow(self):
        # deny_only keeps full detail for denials but records a compact marker
        # for allows, so the activity strip stays complete without the noise.
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            config = {"log": {"verbosity": "deny_only"}}
            with patch.object(cfg, "USER_LOG_FILE", log_path):
                log_decision("Bash", {"command": "ls"}, "/tmp",
                             "ALLOW", "fine", cfg=config)
                log_decision("Bash", {"command": "rm"}, "/tmp",
                             "DENY", "bad", cfg=config)
            lines = [ln for ln in log_path.read_text().splitlines() if ln]
            self.assertEqual(len(lines), 2)
            allow, deny = json.loads(lines[0]), json.loads(lines[1])
            # Compact allow: enough for the strip, none of the heavy fields.
            self.assertEqual(allow["decision"], "ALLOW")
            self.assertEqual(allow["tool"], "Bash")
            self.assertIn("timestamp", allow)
            self.assertNotIn("input_summary", allow)
            self.assertNotIn("reason", allow)
            # Denials keep full detail.
            self.assertEqual(deny["decision"], "DENY")
            self.assertIn("command", deny["input_summary"])
            self.assertEqual(deny["reason"], "bad")

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

    def test_log_break_writes_break_row_to_project_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bd = _make_bouncer_dir(tmp_path, config_yaml="enabled: true\n")
            user_log = tmp_path / "user.jsonl"
            with patch.object(cfg, "USER_LOG_FILE", user_log):
                log_break(str(tmp_path), {"log": {"verbosity": "all"}})
            proj_log = bd / "log.jsonl"
            self.assertTrue(proj_log.exists())
            row = json.loads(proj_log.read_text().splitlines()[0])
            self.assertEqual(row["decision"], "BREAK")
            self.assertIn("timestamp", row)
            # Breaks never pollute the cross-project user log.
            self.assertFalse(user_log.exists())

    def test_log_break_suppressed_when_logging_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bd = _make_bouncer_dir(tmp_path, config_yaml="enabled: true\n")
            log_break(str(tmp_path), {"log": {"verbosity": "off"}})
            self.assertFalse((bd / "log.jsonl").exists())

    def test_cmd_log_break_routes_by_payload_cwd_not_process_cwd(self):
        # E: the --break hook must log the turn boundary to the SAME project
        # the decisions land in — the one named by the hook payload's cwd —
        # not the bouncer process's own working directory. Two projects: the
        # payload-cwd one gets the break; the process-cwd one must stay clean.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            payload_proj = tmp_path / "payload"
            process_proj = tmp_path / "process"
            payload_proj.mkdir()
            process_proj.mkdir()
            bd_payload = _make_bouncer_dir(payload_proj, config_yaml="enabled: true\n")
            bd_process = _make_bouncer_dir(process_proj, config_yaml="enabled: true\n")

            class _A:
                mark_break = True
                user = False

            hook_input = {"cwd": str(payload_proj), "session_id": "s1"}
            with patch("sys.stdin", io.StringIO(json.dumps(hook_input))), \
                 patch("pathlib.Path.cwd", return_value=process_proj):
                cmd_log(_A())

            payload_log = bd_payload / "log.jsonl"
            self.assertTrue(payload_log.exists())
            row = json.loads(payload_log.read_text().splitlines()[0])
            self.assertEqual(row["decision"], "BREAK")
            # The break must not have leaked into the process-cwd project.
            self.assertFalse((bd_process / "log.jsonl").exists())

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


class TestNotify(unittest.TestCase):
    def test_notify_decision_sends_json_to_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            proc = MagicMock()
            proc.stdin = MagicMock()
            with patch.object(notify_mod.subprocess, "Popen", return_value=proc) as popen:
                notify_decision(
                    cfg={"notify": {"command": ["notify-bin"], "decisions": "all"}},
                    tool_name="Bash",
                    tool_input={"command": "printf hi"},
                    cwd=str(tmp_path),
                    session_id="s1",
                    decision="DENY",
                    action="DENY",
                    reason="bad",
                    request_id=123,
                    elapsed_s=1.2345,
                    prompt_chars=99,
                    proj_log=tmp_path / ".bouncer" / "log.jsonl",
                )

            popen.assert_called_once()
            self.assertEqual(popen.call_args.args[0], ["notify-bin"])
            self.assertEqual(popen.call_args.kwargs["stdin"], notify_mod.subprocess.PIPE)
            if notify_mod.os.name == "nt":
                self.assertEqual(
                    popen.call_args.kwargs["creationflags"],
                    notify_mod.subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            else:
                self.assertTrue(popen.call_args.kwargs["start_new_session"])
            payload = json.loads(proc.stdin.write.call_args.args[0])
            proc.stdin.close.assert_called_once()
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["tool"], "Bash")
            self.assertEqual(payload["command"], "printf hi")
            self.assertEqual(payload["decision"], "DENY")
            self.assertEqual(payload["action"], "DENY")
            self.assertEqual(payload["reason"], "bad")
            self.assertEqual(payload["request_id"], 123)
            self.assertEqual(payload["elapsed_s"], 1.234)
            self.assertEqual(payload["prompt_chars"], 99)

    def test_notify_decision_filters_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(notify_mod.subprocess, "Popen") as popen:
                notify_decision(
                    cfg={"notify": {"command": "notify-bin", "decisions": ["DENY"]}},
                    tool_name="Bash",
                    tool_input={"command": "ls"},
                    cwd=tmp,
                    session_id="s1",
                    decision="ALLOW",
                    action="ALLOW",
                    reason="ok",
                )

            popen.assert_not_called()

    def test_notify_decision_ignores_spawn_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(notify_mod.subprocess, "Popen",
                              side_effect=TimeoutError("slow")):
                notify_decision(
                    cfg={"notify": "notify-bin"},
                    tool_name="Bash",
                    tool_input={"command": "ls"},
                    cwd=tmp,
                    session_id="s1",
                    decision="ALLOW",
                    action="ALLOW",
                    reason="ok",
                )


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
        self.assertIn("#[fg=magenta]B#[default]", out)
        self.assertIn("#[fg=red]B#[default]", out)
        self.assertIn("#[fg=cyan]B#[default]", out)

    def test_render_activity_color_overrides_apply_to_ansi_and_tmux(self):
        cfg = {
            "activity": {
                "colors": {
                    "ALLOW": "yellow",
                    "UNSURE": "cyan",
                }
            }
        }
        tmux_out = _render_activity(
            [
                {"d": "ALLOW", "t": "Bash"},
                {"d": "UNSURE", "t": "Bash"},
            ],
            as_format="tmux",
            cfg=cfg,
        )
        ansi_out = _render_activity(
            [
                {"d": "ALLOW", "t": "Bash"},
                {"d": "UNSURE", "t": "Bash"},
            ],
            as_format="ansi",
            cfg=cfg,
        )
        self.assertIn("#[fg=yellow]B#[default]", tmux_out)
        self.assertIn("#[fg=cyan]B#[default]", tmux_out)
        self.assertIn("\033[33mB\033[0m", ansi_out)
        self.assertIn("\033[36mB\033[0m", ansi_out)

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

    def test_project_activity_uses_configured_tmux_colors(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bouncer_dir = _make_bouncer_dir(
                tmp_path,
                config_yaml=(
                    "enabled: true\n"
                    "activity:\n"
                    "  colors:\n"
                    "    DENY: yellow\n"
                ),
            )
            log_path = bouncer_dir / "log.jsonl"
            log_path.write_text(
                json.dumps({"tool": "Bash", "decision": "DENY"}) + "\n",
                encoding="utf-8",
            )

            class _A:
                width = 2
                session = None
                cwd = str(tmp_path)
                as_format = "tmux"
                project = True

            stdout_buf = io.StringIO()
            with redirect_stdout(stdout_buf):
                cmd_activity(_A())

        self.assertEqual(stdout_buf.getvalue(), "#[fg=yellow]B#[default]")

    def test_project_activity_renders_break_dot(self):
        # A BREAK row in the project log becomes a dim separator dot in the
        # strip, the same marker the statusline shows — now unified on one
        # source so both harness strips get it from the log.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bouncer_dir = _make_bouncer_dir(
                tmp_path, config_yaml="enabled: true\n")
            rows = [
                {"tool": "Bash", "decision": "ALLOW"},
                {"decision": "BREAK"},
                {"tool": "Bash", "decision": "DENY"},
            ]
            (bouncer_dir / "log.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

            class _A:
                width = 5
                session = None
                cwd = str(tmp_path)
                as_format = "plain"
                project = True

            stdout_buf = io.StringIO()
            with redirect_stdout(stdout_buf):
                cmd_activity(_A())

        # Newest-first: DENY(B), break(·), ALLOW(B).
        self.assertEqual(stdout_buf.getvalue(), "B·B")

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
    def test_format_installed_harnesses_lists_installed_integrations(self):
        with patch.object(
            init_mod,
            "_INSTALLERS",
            {"codex": object(), "opencode": object(), "shim": object()},
        ), patch.object(
            init_mod,
            "_IS_INSTALLED",
            {
                "codex": lambda: True,
                "opencode": lambda: False,
                "shim": lambda: True,
            },
        ):
            self.assertEqual(init_mod.format_installed_harnesses(), "codex, shim")

    def test_format_installed_harnesses_reports_none(self):
        with patch.object(init_mod, "_INSTALLERS", {"codex": object()}), patch.object(
            init_mod,
            "_IS_INSTALLED",
            {"codex": lambda: False},
        ):
            self.assertEqual(init_mod.format_installed_harnesses(), "(none)")

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

    def test_codex_pretool_install_adds_hard_guard_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            codex_dir = home / ".codex"
            codex_dir.mkdir(parents=True)
            hooks_json = codex_dir / "hooks.json"
            hooks_json.write_text(json.dumps({"hooks": {}}), encoding="utf-8")

            with (
                patch.object(init_mod.Path, "home", return_value=home),
                redirect_stdout(io.StringIO()),
            ):
                init_mod._install_codex_pretool()

            installed_hook = codex_dir / "hooks" / "bouncer_pre_tool_use.py"
            cfg_data = json.loads(hooks_json.read_text(encoding="utf-8"))
            pre = cfg_data["hooks"]["PreToolUse"]
            self.assertEqual(pre[0]["matcher"], "Bash")
            command = pre[0]["hooks"][0]["command"]
            self.assertEqual(command, str(installed_hook))
            self.assertIn(
                "codex-pretool",
                installed_hook.read_text(encoding="utf-8"),
            )

    def test_opencode_install_patches_existing_jsonc_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            opencode_dir = home / ".config" / "opencode"
            opencode_dir.mkdir(parents=True)
            config_path = opencode_dir / "opencode.jsonc"
            config_path.write_text(
                '{\n'
                '  "$schema": "https://opencode.ai/config.json",\n'
                '  // existing comments are accepted while reading\n'
                '  "snapshot": false\n'
                '}\n',
                encoding="utf-8",
            )

            with (
                patch.object(init_mod.Path, "home", return_value=home),
                redirect_stdout(io.StringIO()),
            ):
                init_mod._install_opencode()

            self.assertFalse((opencode_dir / "opencode.json").exists())
            data = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(data["plugin"], ["bouncer"])
            self.assertTrue((opencode_dir / "plugin" / "bouncer.ts").exists())

    def test_opencode_install_preserves_jsonc_when_plugin_already_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            opencode_dir = home / ".config" / "opencode"
            opencode_dir.mkdir(parents=True)
            config_path = opencode_dir / "opencode.jsonc"
            original = (
                '{\n'
                '  // keep this comment\n'
                '  "plugin": ["bouncer"],\n'
                '  "snapshot": false\n'
                '}\n'
            )
            config_path.write_text(original, encoding="utf-8")

            with (
                patch.object(init_mod.Path, "home", return_value=home),
                redirect_stdout(io.StringIO()),
            ):
                init_mod._install_opencode()

            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            self.assertTrue((opencode_dir / "plugin" / "bouncer.ts").exists())

    def test_opencode_install_creates_json_config_when_none_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            opencode_dir = home / ".config" / "opencode"

            with (
                patch.object(init_mod.Path, "home", return_value=home),
                redirect_stdout(io.StringIO()),
            ):
                init_mod._install_opencode()

            config_path = opencode_dir / "opencode.json"
            data = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(data["plugin"], ["bouncer"])
            self.assertTrue((opencode_dir / "plugin" / "bouncer.ts").exists())

    def test_opencode_install_bails_when_json_and_jsonc_both_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            opencode_dir = home / ".config" / "opencode"
            opencode_dir.mkdir(parents=True)
            json_path = opencode_dir / "opencode.json"
            jsonc_path = opencode_dir / "opencode.jsonc"
            json_path.write_text("{}\n", encoding="utf-8")
            jsonc_path.write_text("{}\n", encoding="utf-8")
            stdout_buf = io.StringIO()

            with (
                patch.object(init_mod.Path, "home", return_value=home),
                redirect_stdout(stdout_buf),
            ):
                init_mod._install_opencode()

            self.assertIn("both", stdout_buf.getvalue())
            self.assertNotIn("plugin", json.loads(json_path.read_text(encoding="utf-8")))
            self.assertNotIn("plugin", json.loads(jsonc_path.read_text(encoding="utf-8")))
            self.assertFalse((opencode_dir / "plugin" / "bouncer.ts").exists())

    def test_project_init_prints_installed_integrations(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            class _A:
                harness = None

            stdout_buf = io.StringIO()
            with (
                patch.object(init_mod.Path, "cwd", return_value=tmp_path),
                patch.object(init_mod, "format_installed_harnesses", return_value="codex"),
                patch.object(init_mod, "_has_installed_harness", return_value=True),
                redirect_stdout(stdout_buf),
            ):
                init_mod.cmd_init(_A())

        self.assertIn("Installed integrations:", stdout_buf.getvalue())
        self.assertIn("codex", stdout_buf.getvalue())

    def test_install_targets_skips_installed_by_default(self):
        installer = MagicMock()
        stdout_buf = io.StringIO()

        with (
            patch.object(init_mod, "_INSTALLERS", {"opencode": installer}),
            patch.object(init_mod, "_IS_INSTALLED", {"opencode": lambda: True}),
            redirect_stdout(stdout_buf),
        ):
            init_mod._install_targets(["opencode"])

        self.assertEqual(installer.call_count, 0)
        self.assertIn("already installed", stdout_buf.getvalue())

    def test_install_targets_refreshes_installed_when_requested(self):
        installer = MagicMock()
        stdout_buf = io.StringIO()

        with (
            patch.object(init_mod, "_INSTALLERS", {"opencode": installer}),
            patch.object(init_mod, "_IS_INSTALLED", {"opencode": lambda: True}),
            redirect_stdout(stdout_buf),
        ):
            init_mod._install_targets(["opencode"], refresh=True)

        self.assertEqual(installer.call_count, 1)
        self.assertIn("Refreshing opencode", stdout_buf.getvalue())

    def test_global_init_with_explicit_harness_refreshes_installed_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp) / ".config" / "bouncer"
            installer = MagicMock()
            stdout_buf = io.StringIO()

            class _A:
                harness = "opencode"

            with (
                patch.object(init_mod, "USER_CONFIG_DIR", user_dir),
                patch.object(init_mod, "USER_CONFIG_FILE", user_dir / "config.yaml"),
                patch.object(init_mod, "USER_POLICY_FILE", user_dir / "policy.md"),
                patch.object(init_mod, "_INSTALLERS", {"opencode": installer}),
                patch.object(init_mod, "_IS_INSTALLED", {"opencode": lambda: True}),
                patch.object(init_mod, "_resolve_harness_targets", return_value=["opencode"]),
                redirect_stdout(stdout_buf),
            ):
                init_mod.cmd_global_init(_A())

        self.assertEqual(installer.call_count, 1)
        self.assertIn("Refreshing opencode", stdout_buf.getvalue())


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

class TestStatus(unittest.TestCase):
    def test_status_prints_installed_integrations(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _make_bouncer_dir(tmp_path, config_yaml="enabled: true\n")

            class _A:
                verbose = False

            stdout_buf = io.StringIO()
            with (
                patch("bouncer.commands.status.Path.cwd", return_value=tmp_path),
                patch("bouncer.commands.status.format_installed_harnesses", return_value="codex, opencode"),
                redirect_stdout(stdout_buf),
            ):
                cmd_status(_A())

        out = stdout_buf.getvalue()
        self.assertIn("integrations:", out)
        self.assertIn("codex, opencode", out)


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

class TestToolsCommand(unittest.TestCase):
    def test_tools_lists_observed_harness_tools(self):
        class _A:
            harness = None
            as_format = "plain"

        stdout_buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            with patch.object(cfg, "USER_LOG_FILE", log_path):
                with redirect_stdout(stdout_buf):
                    cmd_tools(_A())

        out = stdout_buf.getvalue()
        self.assertIn("claude_code:", out)
        self.assertIn("documented: Bash", out)
        self.assertIn("observed: (none)", out)
        self.assertIn("mcp__server__tool", out)

    def test_tools_json_can_filter_harness(self):
        class _A:
            harness = "codex"
            as_format = "json"

        stdout_buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            with patch.object(cfg, "USER_LOG_FILE", log_path):
                with redirect_stdout(stdout_buf):
                    cmd_tools(_A())

        data = json.loads(stdout_buf.getvalue())
        self.assertEqual(list(data), ["codex"])
        self.assertEqual(data["codex"]["documented"], ["Bash"])
        self.assertEqual(data["codex"]["observed"], [])
        self.assertEqual(data["codex"]["tools"], ["Bash"])

    def test_tools_includes_global_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            with patch.object(cfg, "USER_LOG_FILE", log_path):
                tool_catalog_mod.record_tool_observation("opencode", "mcp_example")

                class _A:
                    harness = "opencode"
                    as_format = "json"

                stdout_buf = io.StringIO()
                with redirect_stdout(stdout_buf):
                    cmd_tools(_A())

        data = json.loads(stdout_buf.getvalue())
        self.assertIn("mcp_example", data["opencode"]["observed"])
        self.assertIn("mcp_example", data["opencode"]["tools"])


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
_NO_GATE_CONFIG = _BASIC_CONFIG + "escalation_requires_attempt: false\n"


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

    def test_deny_stderr_identifies_bouncer_source(self):
        _, err, _ = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("DENY", "bad", None, None),
        )
        self.assertIn("Source: bouncer policy denial", err)
        self.assertIn("not a direct user denial", err)
        self.assertIn("bouncer --agent-help", err)

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
        self.assertIn("Source: bouncer policy denial", out)
        self.assertNotIn("To send this to the user", out)

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
        self.assertEqual(data["systemMessage"], "bouncer: ALLOW - looks fine")
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
        self.assertEqual(data["systemMessage"], "bouncer: DENY - not in policy")
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
        data = json.loads(out)
        self.assertEqual(data["systemMessage"], "bouncer: ASK - LLM unsure: unclear")
        self.assertNotIn("hookSpecificOutput", data)
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

    def test_unsure_on_unsure_abstain_emits_nothing(self):
        # abstain → no permissionDecision at all, so Claude Code falls back to
        # its own permission flow (auto-mode / normal prompt) as if bouncer had
        # not run. Distinct from allow, which would carry a permissionDecision.
        out, err, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG + "on_unsure: abstain\n",
            call_llm_result=("UNSURE", "unclear", None, None),
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_abstain_codex_permission_omits_decision(self):
        out, err, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG + "on_unsure: abstain\n",
            call_llm_result=("UNSURE", "unclear", None, None),
            fmt="codex-permission",
        )
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        data = json.loads(out)
        self.assertEqual(data["systemMessage"], "bouncer: ABSTAIN - LLM unsure: unclear")
        self.assertNotIn("hookSpecificOutput", data)

    def test_abstain_codex_pretool_is_silent(self):
        out, err, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG + "on_unsure: abstain\n",
            call_llm_result=("UNSURE", "unclear", None, None),
            fmt="codex-pretool",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_abstain_plain_allows_no_gate_to_defer_to(self):
        out, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG + "on_unsure: abstain\n",
            call_llm_result=("UNSURE", "unclear", None, None),
            fmt="plain",
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "allow\n")

    def test_unavailable_default_asks_human(self):
        out, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=(None, "Ollama unreachable", None, None),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_unavailable_exception_asks_human(self):
        out, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_exc=RuntimeError("queue unavailable"),
        )
        self.assertEqual(code, 0)
        hook_output = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(hook_output["permissionDecision"], "ask")
        self.assertIn("queue unavailable", hook_output["permissionDecisionReason"])

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

    def test_unavailable_on_unavailable_abstain_emits_nothing(self):
        # The motivating case: when the LLM backend is down, defer to the
        # harness instead of nagging the user — keeps auto-mode runs unattended.
        out, err, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG + "on_unavailable: abstain\n",
            call_llm_result=(None, "Ollama unreachable", None, None),
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_llm_error_default_asks_human(self):
        out, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("LLM_ERROR", "Ollama unavailable", None, None),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_llm_error_on_unavailable_deny(self):
        _, _, code = _classify(
            self._hook(),
            config_yaml=_BASIC_CONFIG + "on_unavailable: deny\n",
            call_llm_result=("LLM_ERROR", "auth failed", None, None),
        )
        self.assertEqual(code, 2)

    def test_llm_error_display_decision_distinct_from_timeout(self):
        # The display decision must surface *what* went wrong, not collapse to
        # UNSURE — LLM_ERROR for unreachable/auth/etc, TIMEOUT for slow.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user_dir = tmp_path / "user"
            user_dir.mkdir()
            _make_bouncer_dir(tmp_path, config_yaml=_BASIC_CONFIG)
            with (
                patch.object(cfg, "USER_CONFIG_FILE", user_dir / "config.yaml"),
                patch.object(cfg, "USER_POLICY_FILE", user_dir / "policy.md"),
                patch.object(classify_mod, "call_llm",
                             return_value=("LLM_ERROR", "Anthropic 401", None, None)),
            ):
                display, _reason, action, _pc, _snap = classify_mod.get_classification(
                    "Bash", {"command": "ls"}, str(tmp_path)
                )
            self.assertEqual(display, "LLM_ERROR")
            self.assertEqual(action, "ASK")  # on_unavailable default

    def test_bouncer_help_commands_skip_without_llm(self):
        for cmd in ("bouncer --agent-help", "bouncer --help", "bouncer -h"):
            with self.subTest(cmd=cmd):
                _, _, code = _classify(
                    self._hook(command=cmd),
                    config_yaml=_BASIC_CONFIG,
                    call_llm_result=("DENY", "should not be called", None, None),
                )
                self.assertEqual(code, 0)

    def test_bouncer_diagnostic_commands_skip_without_llm(self):
        commands = (
            "bouncer status",
            "bouncer status -v",
            "bouncer check --llm 'pwd'",
            "/Users/example/.local/bin/bouncer check --llm 'pwd'",
            "bouncer log --tail",
            "bouncer activity --project",
            "bouncer -g log",
        )
        for cmd in commands:
            with self.subTest(cmd=cmd):
                _, _, code = _classify(
                    self._hook(command=cmd),
                    config_yaml=_BASIC_CONFIG,
                    call_llm_result=("DENY", "should not be called", None, None),
                )
                self.assertEqual(code, 0)

    def test_bouncer_mutating_commands_do_not_skip(self):
        out, _, code = _classify(
            self._hook(command="bouncer init"),
            config_yaml=_BASIC_CONFIG,
            call_llm_result=("ALLOW", "ok", None, None),
        )
        self.assertEqual(code, 0)
        self.assertIn("permissionDecision", out)

    def test_escalate_prefix_produces_ask(self):
        out, _, code = _classify(
            self._hook(command="# ESCALATE: needed for deploy\nrm -rf dist/"),
            config_yaml=_NO_GATE_CONFIG,
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
            config_yaml=_NO_GATE_CONFIG,
        )
        reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("deploy step", reason)

    def test_escalate_plain_denies_when_ask_not_available(self):
        out, _, code = _classify(
            self._hook(command="# ESCALATE: deploy step\nrm -rf dist/"),
            config_yaml=_NO_GATE_CONFIG,
            fmt="plain",
        )
        self.assertEqual(code, 2)
        self.assertTrue(out.startswith("deny\tagent escalation requested: deploy step"))
        self.assertIn("does not have ASK available", out)

    def test_escalate_codex_permission_abstains_so_codex_asks(self):
        out, err, code = _classify(
            self._hook(command="# ESCALATE: deploy step\nrm -rf dist/"),
            config_yaml=_NO_GATE_CONFIG,
            fmt="codex-permission",
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(
            data["systemMessage"],
            "bouncer: ASK - agent escalation requested: deploy step",
        )
        self.assertNotIn("hookSpecificOutput", data)
        self.assertEqual(err, "")

    def test_escalate_denied_when_not_attempted(self):
        # Default config gates escalation: a command never tried first is
        # rejected rather than escalated.
        out, err, code = _classify(
            self._hook(command="# ESCALATE: deploy\nrm -rf dist/"),
            config_yaml=_BASIC_CONFIG,
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("doesn't match", err)

    def test_escalate_denied_when_only_other_command_attempted(self):
        with tempfile.TemporaryDirectory() as shared:
            _classify(
                self._hook(command="ls"),
                config_yaml=_BASIC_CONFIG,
                call_llm_result=("ALLOW", "ok", None, None),
                escalation_dir=shared,
            )
            out, err, code = _classify(
                self._hook(command="# ESCALATE: deploy\nrm -rf dist/"),
                config_yaml=_BASIC_CONFIG,
                escalation_dir=shared,
            )
            self.assertEqual(code, 2)
            self.assertIn("doesn't match", err)

    def test_escalate_honored_after_bare_attempt(self):
        with tempfile.TemporaryDirectory() as shared:
            # 1) agent runs the bare command (denied by policy)
            _classify(
                self._hook(command="rm -rf dist/"),
                config_yaml=_BASIC_CONFIG,
                call_llm_result=("DENY", "destructive", None, None),
                escalation_dir=shared,
            )
            # 2) agent escalates the same command -> now honored as ASK
            out, _, code = _classify(
                self._hook(command="# ESCALATE: needed for deploy\nrm -rf dist/"),
                config_yaml=_BASIC_CONFIG,
                escalation_dir=shared,
            )
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(
                data["hookSpecificOutput"]["permissionDecision"], "ask"
            )

    def test_escalate_honored_despite_whitespace_difference(self):
        with tempfile.TemporaryDirectory() as shared:
            _classify(
                self._hook(command="rm    -rf\tdist/"),
                config_yaml=_BASIC_CONFIG,
                call_llm_result=("DENY", "destructive", None, None),
                escalation_dir=shared,
            )
            out, _, code = _classify(
                self._hook(command="# ESCALATE: deploy\nrm -rf dist/"),
                config_yaml=_BASIC_CONFIG,
                escalation_dir=shared,
            )
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(
                data["hookSpecificOutput"]["permissionDecision"], "ask"
            )

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

    def test_classify_runs_configured_notifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user_dir = tmp_path / "user"
            user_dir.mkdir()
            _make_bouncer_dir(
                tmp_path,
                config_yaml=(
                    "enabled: true\n"
                    "notify:\n"
                    "  command: notify-test\n"
                    "  decisions: [ALLOW]\n"
                ),
            )
            hook_input = {"tool_name": "Bash", "tool_input": {"command": "ls"},
                          "cwd": str(tmp_path), "session_id": "sess-1"}
            log_path = user_dir / "log.jsonl"
            with (
                patch.object(cfg, "USER_CONFIG_FILE", user_dir / "config.yaml"),
                patch.object(cfg, "USER_POLICY_FILE", user_dir / "policy.md"),
                patch.object(cfg, "USER_LOG_FILE", log_path),
                patch.object(classify_mod, "call_llm",
                             return_value=("ALLOW", "fine", 500, None)),
                patch.object(notify_mod.subprocess, "Popen") as popen,
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

            self.assertEqual(popen.call_count, 1)
            payload = json.loads(popen.return_value.stdin.write.call_args.args[0])
            self.assertEqual(payload["decision"], "ALLOW")
            self.assertEqual(payload["action"], "ALLOW")
            self.assertEqual(payload["command"], "ls")
            self.assertEqual(payload["session_id"], "sess-1")
            self.assertEqual(payload["project"]["cwd"], str(tmp_path))

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

    def test_provider_section_not_flagged_unknown(self):
        # Provider-keyed sections are consumed by the vendored llmclient for
        # key/URL resolution; they must lint clean, not warn as unknown keys.
        out, code = _lint("openai:\n  api_" "key: dummy-openai-key\nollama:\n  url: http://x\n")
        self.assertEqual(code, 0)
        self.assertNotIn("Unknown key", out)

    def test_global_flag_lints_user_config(self):
        # `bouncer -g lint` validates the user config (no file arg, no project).
        with tempfile.TemporaryDirectory() as tmp:
            user_cfg = Path(tmp) / "config.yaml"
            user_cfg.write_text("enabled: true\ntools: all\n", encoding="utf-8")
            stdout_buf = io.StringIO()

            class _A:
                file = None
                user = True

            with patch("bouncer.commands.lint.USER_CONFIG_FILE", user_cfg), \
                 redirect_stdout(stdout_buf):
                try:
                    cmd_lint(_A())
                    code = 0
                except SystemExit as e:
                    code = e.code
        self.assertEqual(code, 0)
        self.assertIn(str(user_cfg), stdout_buf.getvalue())

    def test_global_flag_missing_user_config_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "config.yaml"  # never created

            class _A:
                file = None
                user = True

            with patch("bouncer.commands.lint.USER_CONFIG_FILE", missing), \
                 redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    cmd_lint(_A())
        self.assertEqual(ctx.exception.code, 1)

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


# ---------------------------------------------------------------------------
# llmclient configure() wiring
# ---------------------------------------------------------------------------

class TestLLMClientConfigure(unittest.TestCase):
    def tearDown(self):
        llmclient_configure(config_dir=None)  # reset to global-only

    def test_config_dir_overlays_key_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp)
            (cfg_dir / "config.yaml").write_text(
                "openai:\n  api_" "key: dummy-openai-key\nollama:\n  url: http://app:11434\n",
                encoding="utf-8",
            )
            llmclient_configure(config_dir=cfg_dir)
            self.assertEqual(resolve_api_key("openai", ""), "dummy-openai-key")
            self.assertEqual(resolve_url("ollama", ""), "http://app:11434")

    def test_explicit_value_beats_config_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp)
            (cfg_dir / "config.yaml").write_text(
                "openai:\n  api_" "key: dummy-openai-key\n", encoding="utf-8")
            llmclient_configure(config_dir=cfg_dir)
            self.assertEqual(resolve_api_key("openai", "sk-explicit"), "sk-explicit")

    def test_main_points_llmclient_at_user_config_dir(self):
        # bouncer's entry point must call configure() with its user config dir
        # so keys resolve from ~/.config/bouncer before the global llmclient files.
        import bouncer.__main__ as main_mod
        from bouncer.llmclient._config import get_config_files
        with (
            patch("sys.argv", ["bouncer"]),
            patch.object(cfg, "USER_CONFIG_DIR", Path("/tmp/bouncer-cfgdir-test")),
            redirect_stdout(io.StringIO()),
        ):
            try:
                main_mod.main()
            except SystemExit:
                pass
        self.assertEqual(get_config_files()[0],
                         Path("/tmp/bouncer-cfgdir-test") / "config.yaml")
        llmclient_configure(config_dir=None)


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
        self.assertEqual(
            captured["cfg"].circuit_key,
            "bouncer|openai_compatible|reasoning-test-model|",
        )

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

    def test_default_temperature_is_integer_zero(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured.update(json.loads(req.data))
            response = MagicMock()
            response.read.return_value = json.dumps({
                "choices": [{"message": {"content": "DECISION: ALLOW\nREASON: ok"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 4},
            }).encode()
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            return response

        cfg = LLMConfig(provider="openai_compatible", model="test-model")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = call_openai("", "user", cfg, "https://example.test", "k")

        self.assertEqual(result.outcome, "success")
        self.assertEqual(captured["temperature"], 0)
        self.assertIs(type(captured["temperature"]), int)


class TestSkipReason(unittest.TestCase):
    def test_skip_reason_cases(self):
        from bouncer.classify import _skip_reason
        # classify: enabled, tool intercepted, not a diagnostic
        self.assertIsNone(_skip_reason("Bash", {"command": "ls"},
                                       {"enabled": True, "tools": "all"}))
        self.assertIsNone(_skip_reason("Write", {},
                                       {"enabled": True, "tools": "all"}))
        # tool not in an explicit list
        self.assertEqual(
            _skip_reason("Write", {}, {"enabled": True, "tools": ["Bash"]}),
            "tool 'Write' not intercepted (tools/groups config)",
        )
        # harness plumbing (@internal) is skipped under the default `all`
        self.assertIsNotNone(
            _skip_reason("ToolSearch", {}, {"enabled": True, "tools": "all"})
        )
        # ...but a non-internal read-only tool is still classified
        self.assertIsNone(
            _skip_reason("Read", {}, {"enabled": True, "tools": "all"})
        )
        # disabled
        self.assertEqual(
            _skip_reason("Bash", {}, {"enabled": False, "tools": "all"}),
            "bouncer disabled in config",
        )
        # bouncer's own diagnostic command
        self.assertIn(
            "diagnostic",
            _skip_reason("Bash", {"command": "bouncer status"},
                         {"enabled": True, "tools": "all"}),
        )

    def test_intercept_fold_and_groups(self):
        from bouncer.config import (
            expand_tools, resolve_groups, resolve_intercept,
            _intercepted, tool_intercepted, uses_bare_all,
        )

        def gated(tool, layers):
            ops, groups = resolve_intercept(layers)
            return _intercepted(tool, ops, groups)

        # default `all` => everything except @internal plumbing
        self.assertTrue(gated("Bash", [{}]))
        self.assertTrue(gated("Read", [{}]))
        self.assertFalse(gated("ToolSearch", [{}]))

        # delta: project skips Read on top of the inherited set
        self.assertFalse(gated("Read", [{}, {"tools": ["-Read"]}]))
        self.assertTrue(gated("Bash", [{}, {"tools": ["-Read"]}]))

        # delta: force-gate a plumbing tool in one layer
        self.assertTrue(gated("ToolSearch", [{"tools": ["+ToolSearch"]}]))

        # legacy bare list == absolute "only Bash" (implicit -@all)
        self.assertTrue(gated("Bash", [{"tools": ["Bash"]}]))
        self.assertFalse(gated("Write", [{"tools": ["Bash"]}]))

        # explicit absolute via -all reset, regardless of inherited base
        self.assertTrue(gated("Bash", [{"tools": ["-all", "+Bash"]}]))
        self.assertFalse(gated("Read", [{"tools": ["-all", "+Bash"]}]))

        # later layer wins (last matching op)
        self.assertTrue(
            gated("Read", [{"tools": ["-Read"]}, {"tools": ["+Read"]}])
        )

        # glob selector
        self.assertFalse(
            gated("mcp__google_workspace__list_calendars",
                  [{"tools": ["-mcp__google_workspace__*"]}])
        )
        self.assertTrue(
            gated("mcp__squirrel__search",
                  [{"tools": ["-mcp__google_workspace__*"]}])
        )

        # editing @internal globally re-gates ToolSearch (group fold + all sugar)
        self.assertTrue(
            gated("ToolSearch", [{"groups": {"internal": "-ToolSearch"}}])
        )
        # ...and a custom group can be skipped wholesale
        self.assertFalse(
            gated("Edit", [{"groups": {"risky": ["+Edit", "+Write"]}},
                           {"tools": ["-@risky"]}])
        )
        self.assertTrue(
            gated("Bash", [{"groups": {"risky": ["+Edit", "+Write"]}},
                           {"tools": ["-@risky"]}])
        )

        # resolve_groups seeds DEFAULT_GROUPS and folds edits
        self.assertEqual(resolve_groups([])["internal"], frozenset({"ToolSearch"}))
        self.assertEqual(
            resolve_groups([{"internal": "-ToolSearch"}])["internal"],
            frozenset(),
        )

        # tool_intercepted fallback path (directly-built config, no _tools_ops)
        self.assertFalse(tool_intercepted("ToolSearch", {"tools": "all"}))
        self.assertTrue(tool_intercepted("Bash", {"tools": "all"}))
        self.assertFalse(tool_intercepted("Write", {"tools": ["Bash"]}))

        # expand_tools sugar
        self.assertEqual(
            expand_tools("all"), [("+", "@all"), ("-", "@internal")]
        )
        self.assertEqual(expand_tools("none"), [("-", "@all")])
        self.assertEqual(expand_tools([]), [("-", "@all")])

        # deprecation detector
        self.assertTrue(uses_bare_all("all"))
        self.assertTrue(uses_bare_all(["all", "+Bash"]))
        self.assertFalse(uses_bare_all(["+@all", "-@internal"]))
        self.assertFalse(uses_bare_all(["Bash"]))

    def test_skipped_tool_does_not_strand_pending(self):
        # Regression: a non-intercepted tool must not write a PENDING entry to
        # the project log (and must not call the LLM).
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user_dir = tmp_path / "user"
            user_dir.mkdir()
            _make_bouncer_dir(tmp_path, "enabled: true\ntools:\n  - Bash\n")
            full_input = {
                "tool_name": "Write",
                "tool_input": {"file_path": "/x", "content": "y"},
                "cwd": str(tmp_path),
                "session_id": "s",
            }
            with (
                patch.object(cfg, "USER_CONFIG_FILE", user_dir / "config.yaml"),
                patch.object(cfg, "USER_POLICY_FILE", user_dir / "policy.md"),
                patch.object(cfg, "USER_LOG_FILE",    user_dir / "log.jsonl"),
                patch.object(classify_mod, "call_llm") as mock_llm,
                patch("sys.stdin", io.StringIO(json.dumps(full_input))),
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
                proj_log = tmp_path / ".bouncer" / "log.jsonl"
                contents = proj_log.read_text() if proj_log.exists() else ""
            self.assertNotIn("PENDING", contents)
            mock_llm.assert_not_called()


class TestConfigSetTools(unittest.TestCase):
    def test_set_tools_replaces_list_and_inline_without_clobbering(self):
        from bouncer.commands.config import _set_tools
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.yaml"
            p.write_text(
                "enabled: true\ntools:\n  - Bash\n  - Write\n\nllm:\n  provider: ollama\n",
                encoding="utf-8",
            )
            _set_tools(p, "all")
            t = p.read_text()
            self.assertIn("tools: all", t)
            self.assertNotIn("- Bash", t)
            self.assertIn("provider: ollama", t)  # rest of file intact

            _set_tools(p, ["bash", "read"])
            t2 = p.read_text()
            self.assertIn("tools:\n  - bash\n  - read", t2)
            self.assertNotIn("tools: all", t2)
            self.assertIn("provider: ollama", t2)

    def test_set_tools_prepends_when_key_absent(self):
        from bouncer.commands.config import _set_tools
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.yaml"
            p.write_text("enabled: true\n", encoding="utf-8")
            _set_tools(p, "all")
            self.assertIn("tools: all", p.read_text())


class TestEscalationCache(unittest.TestCase):
    def test_normalize_collapses_whitespace(self):
        self.assertEqual(
            escalation_mod.normalize("  rm   -rf\tdist/\n"), "rm -rf dist/"
        )

    def test_strip_escalate_prefix(self):
        self.assertEqual(
            escalation_mod.strip_escalate_prefix("# ESCALATE: why\nrm -rf dist/"),
            "rm -rf dist/",
        )

    def test_strip_escalate_trailing_inline_marker(self):
        # A trailing inline marker must recover the real command, not strip to
        # empty (the old first-line-discard behavior).
        self.assertEqual(
            escalation_mod.strip_escalate_prefix("rm -rf dist/  # ESCALATE: why"),
            "rm -rf dist/",
        )

    def test_parse_escalation_leading_and_trailing(self):
        self.assertEqual(
            escalation_mod.parse_escalation("# ESCALATE: deploy\nrm -rf dist/"),
            ("deploy", "rm -rf dist/"),
        )
        self.assertEqual(
            escalation_mod.parse_escalation("rm -rf dist/  # ESCALATE: deploy"),
            ("deploy", "rm -rf dist/"),
        )

    def test_parse_escalation_none_when_no_marker(self):
        self.assertIsNone(escalation_mod.parse_escalation("rm -rf dist/"))

    def test_parse_escalation_ignores_marker_inside_quotes(self):
        # A marker buried in a quoted string is not a real escalation — the
        # `#` is not at line start or preceded by whitespace.
        self.assertIsNone(
            escalation_mod.parse_escalation('grep "# ESCALATE:" file.txt')
        )

    def test_escalate_honored_after_bare_attempt_trailing_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(escalation_mod, "ESCALATION_DIR", Path(tmp)):
                escalation_mod.record_attempt("rm -rf dist/", "sess")
                # The trailing-marker form resolves to the same underlying
                # command, so the gate honors it.
                underlying = escalation_mod.strip_escalate_prefix(
                    "rm -rf dist/  # ESCALATE: deploy"
                )
                self.assertTrue(
                    escalation_mod.was_attempted(underlying, "sess", 300)
                )

    def test_record_then_attempted_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(escalation_mod, "ESCALATION_DIR", Path(tmp)):
                escalation_mod.record_attempt("rm -rf dist/", "sess")
                self.assertTrue(
                    escalation_mod.was_attempted("rm -rf dist/", "sess", 300)
                )

    def test_whitespace_forgiving_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(escalation_mod, "ESCALATION_DIR", Path(tmp)):
                escalation_mod.record_attempt("rm    -rf\tdist/", "sess")
                self.assertTrue(
                    escalation_mod.was_attempted("rm -rf dist/", "sess", 300)
                )

    def test_different_command_not_matched(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(escalation_mod, "ESCALATION_DIR", Path(tmp)):
                escalation_mod.record_attempt("ls", "sess")
                self.assertFalse(
                    escalation_mod.was_attempted("rm -rf dist/", "sess", 300)
                )

    def test_ttl_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "sess.json"
            cache.write_text(
                json.dumps([{"cmd": "rm -rf dist/", "ts": time.time() - 600}]),
                encoding="utf-8",
            )
            with patch.object(escalation_mod, "ESCALATION_DIR", Path(tmp)):
                self.assertFalse(
                    escalation_mod.was_attempted("rm -rf dist/", "sess", 300)
                )
                self.assertTrue(
                    escalation_mod.was_attempted("rm -rf dist/", "sess", 900)
                )

    def test_empty_command_never_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(escalation_mod, "ESCALATION_DIR", Path(tmp)):
                escalation_mod.record_attempt("   ", "sess")
                self.assertFalse(escalation_mod.was_attempted("", "sess", 300))

    def test_missing_session_file_is_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(escalation_mod, "ESCALATION_DIR", Path(tmp)):
                self.assertFalse(escalation_mod.was_attempted("ls", "nope", 300))


class TestEscalationGrant(unittest.TestCase):
    """The out-of-band, project-keyed, fingerprint-bound grant used to escalate
    non-Bash tool calls (Tool -> DENY -> `bouncer escalate` -> Tool again)."""

    def test_fingerprint_stable_and_sensitive(self):
        fp = grant_mod.fingerprint
        a = fp("Write", {"file_path": "/x", "content": "hi"})
        # canonical: key order doesn't matter
        self.assertEqual(a, fp("Write", {"content": "hi", "file_path": "/x"}))
        # different input or different tool -> different fingerprint
        self.assertNotEqual(a, fp("Write", {"file_path": "/y", "content": "hi"}))
        self.assertNotEqual(a, fp("Read", {"file_path": "/x", "content": "hi"}))

    def test_record_arm_take_roundtrip_and_one_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            with patch.object(grant_mod, "GRANT_DIR", proj / "g"):
                ti = {"file_path": "/etc/hosts", "content": "x"}
                grant_mod.record_denial(proj, "Write", ti, "blocked by policy")
                target = grant_mod.arm_escalation(proj, "please allow")
                self.assertEqual(target["tool"], "Write")
                self.assertEqual(grant_mod.take_grant(proj, "Write", ti), "please allow")
                # one-shot: consumed
                self.assertIsNone(grant_mod.take_grant(proj, "Write", ti))

    def test_grant_is_fingerprint_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            with patch.object(grant_mod, "GRANT_DIR", proj / "g"):
                grant_mod.record_denial(proj, "Write", {"file_path": "/a"}, "no")
                grant_mod.arm_escalation(proj, "r")
                # a different call cannot consume the grant
                self.assertIsNone(grant_mod.take_grant(proj, "Write", {"file_path": "/b"}))
                # the exact call can
                self.assertEqual(grant_mod.take_grant(proj, "Write", {"file_path": "/a"}), "r")

    def test_identical_calls_share_a_grant_accepted_tradeoff(self):
        # Two byte-identical denied calls share a fingerprint, so either may
        # consume the grant. This is the accepted residual (same call, still an
        # ASK) — assert it explicitly so the behavior is intentional.
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            with patch.object(grant_mod, "GRANT_DIR", proj / "g"):
                ti = {"file_path": "/shared"}
                grant_mod.record_denial(proj, "Read", ti, "no")
                grant_mod.arm_escalation(proj, "r")
                self.assertEqual(grant_mod.take_grant(proj, "Read", dict(ti)), "r")

    def test_arm_with_no_denial_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            with patch.object(grant_mod, "GRANT_DIR", proj / "g"):
                self.assertIsNone(grant_mod.arm_escalation(proj, "r"))

    def test_grant_ttl_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            with patch.object(grant_mod, "GRANT_DIR", proj / "g"), \
                 patch.object(grant_mod, "_GRANT_TTL_S", 0.0):
                ti = {"file_path": "/a"}
                grant_mod.record_denial(proj, "Write", ti, "no")
                grant_mod.arm_escalation(proj, "r")
                self.assertIsNone(grant_mod.take_grant(proj, "Write", ti))

    def test_parse_escalate_command(self):
        from bouncer.classify import _parse_escalate_command
        self.assertEqual(_parse_escalate_command('bouncer escalate "why now"'), "why now")
        self.assertEqual(_parse_escalate_command("bouncer escalate"), "")
        self.assertEqual(_parse_escalate_command("/usr/bin/bouncer escalate hi"), "hi")
        self.assertIsNone(_parse_escalate_command("bouncer status"))
        self.assertIsNone(_parse_escalate_command("echo bouncer escalate"))

    def test_end_to_end_non_bash_escalation(self):
        # Full flow against ONE shared project: Write DENY -> bouncer escalate
        # (arms) -> Write re-issued -> ASK.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _make_bouncer_dir(tmp_path, config_yaml="enabled: true\ntools: all\n")
            user_dir = tmp_path / "user"; user_dir.mkdir()
            esc = tmp_path / "esc"

            def run(hook_input, llm=("DENY", "blocked", None, None)):
                full = dict(hook_input, cwd=str(tmp_path))
                out, err, code = io.StringIO(), io.StringIO(), 0
                with (
                    patch.object(cfg, "USER_CONFIG_FILE", user_dir / "config.yaml"),
                    patch.object(cfg, "USER_POLICY_FILE", user_dir / "policy.md"),
                    patch.object(cfg, "USER_LOG_FILE", user_dir / "log.jsonl"),
                    patch.object(escalation_mod, "ESCALATION_DIR", esc),
                    patch.object(grant_mod, "GRANT_DIR", esc),
                    patch.object(classify_mod, "call_llm", return_value=llm),
                    patch("sys.stdin", io.StringIO(json.dumps(full))),
                    redirect_stdout(out), redirect_stderr(err),
                ):
                    class _A:
                        hook = True
                        format = "json"
                    try:
                        cmd_classify(_A())
                    except SystemExit as e:
                        code = e.code
                return out.getvalue(), err.getvalue(), code

            write = {"tool_name": "Write",
                     "tool_input": {"file_path": "/etc/hosts", "content": "x"}}

            # 1) Write is denied -> denial recorded
            _, _, code = run(write)
            self.assertEqual(code, 2)

            # 2) bouncer escalate arms a grant and is itself ALLOWed
            out, _, code = run({"tool_name": "Bash",
                                "tool_input": {"command": 'bouncer escalate "need it"'}})
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "allow")

            # 3) Re-issue the exact Write -> grant fires -> ASK (no LLM)
            out, _, code = run(write)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_unrelated_call_does_not_consume_grant_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _make_bouncer_dir(tmp_path, config_yaml="enabled: true\ntools: all\n")
            user_dir = tmp_path / "user"; user_dir.mkdir()
            esc = tmp_path / "esc"

            def run(hook_input, llm=("DENY", "blocked", None, None)):
                full = dict(hook_input, cwd=str(tmp_path))
                out, err, code = io.StringIO(), io.StringIO(), 0
                with (
                    patch.object(cfg, "USER_CONFIG_FILE", user_dir / "config.yaml"),
                    patch.object(cfg, "USER_POLICY_FILE", user_dir / "policy.md"),
                    patch.object(cfg, "USER_LOG_FILE", user_dir / "log.jsonl"),
                    patch.object(escalation_mod, "ESCALATION_DIR", esc),
                    patch.object(grant_mod, "GRANT_DIR", esc),
                    patch.object(classify_mod, "call_llm", return_value=llm),
                    patch("sys.stdin", io.StringIO(json.dumps(full))),
                    redirect_stdout(out), redirect_stderr(err),
                ):
                    class _A:
                        hook = True
                        format = "json"
                    try:
                        cmd_classify(_A())
                    except SystemExit as e:
                        code = e.code
                return out.getvalue(), err.getvalue(), code

            run({"tool_name": "Write", "tool_input": {"file_path": "/a"}})
            run({"tool_name": "Bash", "tool_input": {"command": "bouncer escalate x"}})
            # A DIFFERENT write must not inherit the grant: still denied.
            _, _, code = run({"tool_name": "Write", "tool_input": {"file_path": "/b"}})
            self.assertEqual(code, 2)


# ---------------------------------------------------------------------------
# Session profiles
# ---------------------------------------------------------------------------

_SOLO_YAML = """\
default_profile: solo
"""


class _ProfileTestCase(unittest.TestCase):
    """A temp project with its own user config and profile state dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.user_dir = self.root / "user"
        self.user_dir.mkdir()
        self.proj = self.root / "proj"
        self.proj.mkdir()
        self.bdir = _make_bouncer_dir(self.proj, config_yaml="")
        self._patches = [
            patch.object(cfg, "USER_CONFIG_FILE", self.user_dir / "config.yaml"),
            patch.object(cfg, "USER_POLICY_FILE", self.user_dir / "policy.md"),
            patch.object(cfg, "USER_LOG_FILE", self.user_dir / "log.jsonl"),
            patch.object(profile_mod, "PROFILE_DIR", self.root / "profile"),
        ]
        for pt in self._patches:
            pt.start()

    def tearDown(self):
        for pt in reversed(self._patches):
            pt.stop()
        self._tmp.cleanup()

    def write_user(self, text):
        (self.user_dir / "config.yaml").write_text(text, encoding="utf-8")

    def write_project(self, text):
        (self.bdir / "config.yaml").write_text(text, encoding="utf-8")

    def write_local(self, text):
        (self.bdir / "config.local.yaml").write_text(text, encoding="utf-8")


class TestProfileMergeOrder(_ProfileTestCase):
    """user.base -> user.profiles[P] -> project.base -> project.profiles[P]
    -> local.base -> local.profiles[P]."""

    def test_profile_fragment_beats_base_in_same_layer(self):
        self.write_user("on_unsure: ask\nprofiles:\n  solo:\n    on_unsure: deny\n")
        c = _merged_config(self.proj, profile="solo")
        self.assertEqual(c["on_unsure"], "deny")

    def test_later_layer_base_beats_earlier_layer_fragment(self):
        self.write_user("profiles:\n  solo:\n    on_unsure: deny\n")
        self.write_project("on_unsure: allow\n")
        c = _merged_config(self.proj, profile="solo")
        self.assertEqual(c["on_unsure"], "allow")

    def test_local_fragment_wins_over_everything(self):
        self.write_user("on_unsure: ask\nprofiles:\n  solo:\n    on_unsure: deny\n")
        self.write_project("on_unsure: allow\nprofiles:\n  solo:\n    on_unsure: ask\n")
        self.write_local("profiles:\n  solo:\n    on_unsure: abstain\n")
        c = _merged_config(self.proj, profile="solo")
        self.assertEqual(c["on_unsure"], "abstain")

    def test_other_profiles_fragment_is_ignored(self):
        self.write_user("on_unsure: ask\nprofiles:\n  solo:\n    on_unsure: deny\n")
        c = _merged_config(self.proj, profile="live")
        self.assertEqual(c["on_unsure"], "ask")

    def test_builtin_solo_defaults_apply_with_no_config(self):
        c = _merged_config(self.proj, profile="solo")
        self.assertEqual(c["on_unsure"], "abstain")
        self.assertEqual(c["on_unavailable"], "abstain")
        self.assertFalse(c["escalation"])

    def test_builtin_live_defaults_do_not_override_user_base(self):
        # The builtin fragment sits in the defaults layer, so a user's own
        # base setting still wins — existing configs keep working.
        self.write_user("on_unsure: abstain\n")
        c = _merged_config(self.proj, profile="live")
        self.assertEqual(c["on_unsure"], "abstain")


class TestProfileResolution(_ProfileTestCase):
    def test_no_state_uses_default_profile(self):
        self.write_user("default_profile: solo\n")
        self.assertEqual(cfg.resolve_profile(self.proj), "solo")
        self.assertEqual(_merged_config(self.proj)["_profile"], "solo")

    def test_no_state_and_no_config_uses_builtin_default(self):
        self.assertEqual(cfg.resolve_profile(self.proj), "live")

    def test_default_path_still_allows_escalation(self):
        # The regression that would silently turn every existing session into
        # a no-ASK one: an empty config must resolve to live with escalation on.
        c = _merged_config(self.proj)
        self.assertEqual(c["_profile"], "live")
        self.assertTrue(profile_mod.profile_allows_ask(c))
        self.assertEqual(c["on_unsure"], "ask")

    def test_default_profile_solo_turns_escalation_off(self):
        self.write_user("default_profile: solo\n")
        c = _merged_config(self.proj)
        self.assertEqual(c["_profile"], "solo")
        self.assertFalse(profile_mod.profile_allows_ask(c))

    def test_state_beats_default_profile(self):
        self.write_user("default_profile: live\n")
        profile_mod.set_profile(self.bdir, "solo")
        self.assertEqual(cfg.resolve_profile(self.proj), "solo")

    def test_stale_state_name_falls_back_to_default_profile(self):
        self.write_user("default_profile: solo\n")
        profile_mod.set_profile(self.bdir, "midnight")
        self.assertEqual(cfg.resolve_profile(self.proj), "solo")

    def test_custom_profile_name_is_honored(self):
        self.write_user("profiles:\n  midnight:\n    escalation: off\n")
        profile_mod.set_profile(self.bdir, "midnight")
        self.assertEqual(cfg.resolve_profile(self.proj), "midnight")
        self.assertIn("midnight", cfg.known_profile_names(cfg._config_layers(self.proj)))

    def test_profile_state_is_keyed_like_the_grant_state(self):
        self.assertEqual(
            profile_mod._state_file(self.bdir).name,
            f"profile-{cfg.project_key(self.bdir)}.json")
        self.assertEqual(
            grant_mod._grant_file(self.bdir).name,
            f"grant-{cfg.project_key(self.bdir)}.json")


class TestProfileAndHarness(unittest.TestCase):
    """Effective capability is profile AND harness — never one alone."""

    def test_ask_needs_both_halves(self):
        for fmt, allows, expect in (
            ("json",  True,  True),
            ("json",  False, False),
            ("plain", True,  False),
            ("plain", False, False),
        ):
            self.assertEqual(
                hook_mod.harness_can_ask(fmt) and allows, expect,
                f"{fmt} / profile_allows_ask={allows}")

    def test_solo_deny_hint_replaces_the_escalation_advert(self):
        _, err, code = hook_mod.format_hook_response(
            "DENY", "nope", "json", profile_allows_ask=False)
        self.assertEqual(code, 2)
        self.assertIn("You may not perform this action at this time", err)
        self.assertNotIn("Escalation support is available", err)

    def test_live_deny_hint_still_advertises_escalation(self):
        _, err, _ = hook_mod.format_hook_response(
            "DENY", "nope", "json", profile_allows_ask=True)
        self.assertIn("Escalation support is available", err)

    def test_ask_becomes_deny_under_a_no_ask_profile(self):
        # Every harness delivers the refusal in its own vocabulary; none of
        # them may deliver an ASK, and none may quietly pass the call through.
        out, err, code = hook_mod.format_hook_response(
            "ASK", "unsure", "json", profile_allows_ask=False)
        self.assertEqual(code, 2)
        self.assertNotIn("permissionDecision", out)

        for fmt in ("codex-pretool", "plain"):
            out, err, code = hook_mod.format_hook_response(
                "ASK", "unsure", fmt, profile_allows_ask=False)
            self.assertEqual(code, 2, fmt)

        # Codex PermissionRequest expresses a deny in JSON at exit 0; the
        # thing that matters is that it is a deny, not an abstain into the
        # user's approval prompt.
        out, _, code = hook_mod.format_hook_response(
            "ASK", "unsure", "codex-permission", profile_allows_ask=False)
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out)["hookSpecificOutput"]["decision"]["behavior"],
            "deny")

    def test_abstain_is_untouched_by_the_profile(self):
        out, err, code = hook_mod.format_hook_response(
            "ABSTAIN", "unsure", "json", profile_allows_ask=False)
        self.assertEqual((out, err, code), ("", "", 0))

    def test_floor_is_a_harness_property_not_a_format_one(self):
        # json covers both Claude Code and opencode, and they abstain into
        # very different places.
        self.assertTrue(profile_mod.harness_has_unattended_floor("claude_code"))
        self.assertFalse(profile_mod.harness_has_unattended_floor("opencode"))
        self.assertFalse(profile_mod.harness_has_unattended_floor("codex"))
        self.assertFalse(profile_mod.harness_has_unattended_floor("shim"))
        self.assertFalse(profile_mod.harness_has_unattended_floor("unknown"))

    def test_solo_resolves_ask_to_the_floor_or_to_deny(self):
        r = profile_mod.resolve_unattended_action
        self.assertEqual(r("ask", "claude_code"), "abstain")
        self.assertEqual(r("ask", "opencode"), "deny")
        self.assertEqual(r("abstain", "claude_code"), "abstain")
        self.assertEqual(r("abstain", "shim"), "deny")
        self.assertEqual(r("allow", "shim"), "allow")
        self.assertEqual(r("deny", "claude_code"), "deny")

    def test_infer_harness_names_claude_code(self):
        self.assertEqual(
            _infer_harness({"hook_event_name": "PreToolUse"}, "json"),
            "claude_code")
        self.assertEqual(
            _infer_harness({"harness": "opencode"}, "json"), "opencode")
        self.assertEqual(_infer_harness({}, "plain"), "shim")
        self.assertEqual(_infer_harness({}, "codex-permission"), "codex")


class TestEffectiveProfile(_ProfileTestCase):
    """The indicator shows effective, not nominal (design item 13)."""

    def test_live_on_an_asking_harness_is_live(self):
        profile_mod.set_profile(self.bdir, "live")
        profile_mod.note_harness(self.bdir, "claude_code", True)
        st = effective_state(self.proj)
        self.assertEqual(st["profile"], "live")
        self.assertFalse(st["degraded"])

    def test_live_on_a_harness_that_cannot_ask_shows_degraded_solo(self):
        profile_mod.set_profile(self.bdir, "live")
        profile_mod.note_harness(self.bdir, "shim", False)
        st = effective_state(self.proj)
        self.assertEqual(st["profile"], "solo")
        self.assertTrue(st["degraded"], "must not show green live")
        self.assertEqual(st["nominal"], "live")

    def test_chosen_solo_is_not_degraded(self):
        profile_mod.set_profile(self.bdir, "solo")
        profile_mod.note_harness(self.bdir, "shim", False)
        st = effective_state(self.proj)
        self.assertEqual(st["profile"], "solo")
        self.assertFalse(st["degraded"], "chosen solo is normal, not a warning")

    def test_unseen_harness_reports_the_nominal_profile(self):
        profile_mod.set_profile(self.bdir, "live")
        st = effective_state(self.proj)
        self.assertEqual(st["profile"], "live")
        self.assertFalse(st["degraded"])
        self.assertIsNone(st["harness"])

    def test_degraded_and_chosen_solo_render_differently(self):
        chosen = {"profile": "solo", "nominal": "solo", "degraded": False,
                  "harness": "shim", "chosen": True}
        degraded = dict(chosen, nominal="live", degraded=True)
        for fmt in ("ansi", "tmux"):
            a = profile_cmd._render(chosen, fmt)
            b = profile_cmd._render(degraded, fmt)
            self.assertNotEqual(a, b, fmt)
            self.assertIn("solo", a)
            self.assertIn("solo", b)

    def test_note_harness_only_writes_when_the_answer_changes(self):
        profile_mod.note_harness(self.bdir, "claude_code", True)
        stamp = profile_mod._state_file(self.bdir).stat().st_mtime_ns
        profile_mod.note_harness(self.bdir, "claude_code", True)
        self.assertEqual(profile_mod._state_file(self.bdir).stat().st_mtime_ns,
                         stamp)


class TestSoloEscalationGating(unittest.TestCase):
    """Both escalation entry points refuse under a no-ASK profile."""

    ESC_CMD = "# ESCALATE: needed\nrm -rf build/"

    def test_bash_escalate_marker_is_refused(self):
        with tempfile.TemporaryDirectory() as esc:
            _classify(
                {"tool_name": "Bash", "tool_input": {"command": "rm -rf build/"},
                 "session_id": "s1"},
                call_llm_result=("DENY", "no", None, None),
                config_yaml=_SOLO_YAML, escalation_dir=esc)
            out, err, code = _classify(
                {"tool_name": "Bash", "tool_input": {"command": self.ESC_CMD},
                 "session_id": "s1"},
                config_yaml=_SOLO_YAML, escalation_dir=esc)
        self.assertEqual(code, 2)
        self.assertNotIn("permissionDecision", out)
        self.assertIn("Escalation is not available in this session", err)
        self.assertIn("You may not perform this action at this time", err)

    def test_bash_escalate_marker_still_works_under_live(self):
        with tempfile.TemporaryDirectory() as esc:
            _classify(
                {"tool_name": "Bash", "tool_input": {"command": "rm -rf build/"},
                 "session_id": "s1"},
                call_llm_result=("DENY", "no", None, None),
                config_yaml="default_profile: live\n", escalation_dir=esc)
            out, _, code = _classify(
                {"tool_name": "Bash", "tool_input": {"command": self.ESC_CMD},
                 "session_id": "s1"},
                config_yaml="default_profile: live\n", escalation_dir=esc)
        self.assertEqual(code, 0)
        self.assertIn('"permissionDecision": "ask"', out)

    def test_bouncer_escalate_side_channel_is_refused(self):
        with tempfile.TemporaryDirectory() as esc:
            out, err, code = _classify(
                {"tool_name": "Bash",
                 "tool_input": {"command": 'bouncer escalate "please"'},
                 "session_id": "s1"},
                config_yaml=_SOLO_YAML, escalation_dir=esc)
            armed = list(Path(esc).glob("grant-*.json"))
        self.assertEqual(code, 2)
        self.assertIn("Escalation is not available in this session", err)
        self.assertEqual(armed, [], "no ASK state may be created under solo")

    def test_an_armed_grant_is_not_consumed_under_solo(self):
        call = {"tool_name": "Write",
                "tool_input": {"file_path": "/tmp/x", "content": "hi"},
                "session_id": "s1"}
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            esc = Path(tmp) / "esc"
            _make_bouncer_dir(proj, config_yaml=_SOLO_YAML)
            payload = dict(call, cwd=str(proj))
            with patch.object(grant_mod, "GRANT_DIR", esc):
                grant_mod.record_denial(proj / ".bouncer", "Write",
                                        payload["tool_input"], "no")
                grant_mod.arm_escalation(proj / ".bouncer", "please")
                out, err, code = _classify(
                    payload, call_llm_result=("DENY", "still no", None, None),
                    escalation_dir=str(esc))
                still = grant_mod._load(proj / ".bouncer").get("grant")
        self.assertEqual(code, 2)
        self.assertNotIn("escalation requested", out)
        self.assertIsNotNone(still, "grant left to expire, not consumed")

    def test_unsure_does_not_ask_under_solo(self):
        out, err, code = _classify(
            {"tool_name": "Bash", "tool_input": {"command": "ls"},
             "session_id": "s1", "hook_event_name": "PreToolUse"},
            call_llm_result=("UNSURE", "dunno", None, None),
            config_yaml=_SOLO_YAML)
        # Claude Code has an unattended floor, so solo abstains into it.
        self.assertEqual((out, err, code), ("", "", 0))

    def test_unsure_denies_under_solo_on_a_floorless_harness(self):
        out, err, code = _classify(
            {"tool_name": "Bash", "tool_input": {"command": "ls"},
             "session_id": "s1", "harness": "opencode"},
            call_llm_result=("UNSURE", "dunno", None, None),
            config_yaml=_SOLO_YAML)
        self.assertEqual(code, 2)
        self.assertIn("You may not perform this action at this time", err)

    def test_unavailable_denies_under_solo_on_a_floorless_harness(self):
        out, err, code = _classify(
            {"tool_name": "Bash", "tool_input": {"command": "ls"},
             "session_id": "s1", "harness": "shim"},
            call_llm_result=("TIMEOUT", "too slow", None, None),
            config_yaml=_SOLO_YAML, fmt="plain")
        self.assertEqual(code, 2)
        self.assertIn("deny\t", out)

    def test_session_may_not_set_its_own_profile(self):
        out, err, code = _classify(
            {"tool_name": "Bash",
             "tool_input": {"command": "bouncer profile live"},
             "session_id": "s1"},
            config_yaml=_SOLO_YAML)
        self.assertEqual(code, 2)
        self.assertIn("may not set its own bouncer profile", err)

    def test_reading_the_profile_is_still_a_diagnostic(self):
        out, err, code = _classify(
            {"tool_name": "Bash", "tool_input": {"command": "bouncer profile"},
             "session_id": "s1"},
            config_yaml=_SOLO_YAML)
        self.assertEqual((out, err, code), ("", "", 0))


class TestProfileCommand(_ProfileTestCase):
    def _run(self, name=None, as_format="plain"):
        class _A:
            pass
        _A.name = name
        _A.as_format = as_format
        buf = io.StringIO()
        err = io.StringIO()
        code = 0
        with patch("pathlib.Path.cwd", return_value=self.proj), \
                redirect_stdout(buf), redirect_stderr(err):
            try:
                profile_cmd.cmd_profile(_A())
            except SystemExit as e:
                code = e.code
        return buf.getvalue(), err.getvalue(), code

    def test_show_reports_the_default_profile_when_unset(self):
        out, _, code = self._run()
        self.assertEqual(code, 0)
        self.assertIn("live", out)
        self.assertIn("default_profile", out)

    def test_set_and_show_round_trip(self):
        self._run("solo")
        self.assertEqual(profile_mod.get_profile(self.bdir), "solo")
        out, _, _ = self._run()
        self.assertIn("solo", out)
        self.assertNotIn("default_profile", out)

    def test_unknown_token_is_an_error_not_an_action(self):
        _, err, code = self._run("yolo")
        self.assertEqual(code, 2)
        self.assertIn("unknown profile", err)
        self.assertIsNone(profile_mod.get_profile(self.bdir))

    def test_tmux_format_is_a_bare_styled_word(self):
        self._run("solo")
        out, _, _ = self._run(as_format="tmux")
        self.assertEqual(out, "#[fg=yellow]solo#[default]")

    def test_show_resolves_actions_through_the_harness(self):
        self._run("solo")
        profile_mod.note_harness(self.bdir, "opencode", True)
        out, _, _ = self._run()
        self.assertIn("abstain", out)
        self.assertIn("deny", out)


class TestProfileLint(unittest.TestCase):
    def test_profiles_block_is_accepted(self):
        out, code = _lint("default_profile: solo\n"
                          "profiles:\n  solo:\n    escalation: off\n")
        self.assertEqual(code, 0)
        self.assertIn("default_profile: solo", out)

    def test_unknown_default_profile_is_an_error(self):
        out, code = _lint("default_profile: yolo\n")
        self.assertEqual(code, 1)
        self.assertIn("must name a known profile", out)

    def test_solo_re_enabling_escalation_warns(self):
        out, code = _lint("profiles:\n  solo:\n    escalation: on\n")
        self.assertEqual(code, 0)
        self.assertIn("solo means no ASK is ever produced", out)

    def test_non_plumbing_key_in_a_profile_warns(self):
        out, code = _lint("profiles:\n  solo:\n    policy_mode: replace\n")
        self.assertEqual(code, 0)
        self.assertIn("not a profile setting", out)

    def test_bad_profile_action_is_an_error(self):
        out, code = _lint("profiles:\n  solo:\n    on_unsure: maybe\n")
        self.assertEqual(code, 1)
        self.assertIn("profiles.solo.on_unsure", out)

    def test_live_default_warns_when_a_no_ask_harness_is_installed(self):
        with patch.object(lint_mod, "installed_harnesses",
                          return_value=["shim"]):
            out, code = _lint("default_profile: live\n")
        self.assertEqual(code, 0)
        self.assertIn("cannot ask a human at all", out)

    def test_abstain_warns_when_no_installed_harness_has_a_floor(self):
        with patch.object(lint_mod, "installed_harnesses",
                          return_value=["opencode"]):
            out, code = _lint("profiles:\n  solo:\n    escalation: off\n"
                              "    on_unsure: abstain\n")
        self.assertEqual(code, 0)
        self.assertIn("no unattended", out)


class TestPackagedIntegrationAssets(unittest.TestCase):
    """Portability contract: every asset `bouncer init` copies out must live
    inside the package AND be declared in package-data, so a plain (non-editable)
    install can wire harnesses — not just a source checkout. Regression guard
    for the asset dir having lived at the repo root, outside the wheel."""

    _PACKAGE_ROOT = Path(init_mod.__file__).parent.parent  # bouncer/ (init is in commands/)

    # Path (relative to the package) of each asset an installer copies out.
    _ASSETS = [
        "shim/bash",
        "integrations/codex/bouncer_hook.py",
        "integrations/codex/bouncer_pre_tool_use.py",
        "integrations/opencode/bouncer_plugin.ts",
    ]

    def test_assets_exist_inside_package(self):
        for rel in self._ASSETS:
            with self.subTest(asset=rel):
                self.assertTrue((self._PACKAGE_ROOT / rel).is_file(),
                                f"missing packaged asset: {rel}")

    def test_asset_dir_anchors_to_package_not_repo_root(self):
        self.assertEqual(init_mod._ASSET_DIR,
                         self._PACKAGE_ROOT / "integrations")

    def test_package_data_covers_every_copied_asset(self):
        import tomllib
        from fnmatch import fnmatch
        pyproject = self._PACKAGE_ROOT.parent / "pyproject.toml"
        if not pyproject.exists():
            self.skipTest("pyproject.toml not present (installed, not source)")
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        patterns = data["tool"]["setuptools"]["package-data"]["bouncer"]
        for rel in self._ASSETS:
            with self.subTest(asset=rel):
                self.assertTrue(
                    any(fnmatch(rel, pat) for pat in patterns),
                    f"{rel} not covered by package-data {patterns}",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
