import json
import os
import shutil
import stat
import sys
from pathlib import Path

from ..colors import RESET, BOLD, GREEN, YELLOW, RED, DIM
from ..config import (
    PROJECT_DIR_NAME,
    CONFIG_YAML_TEMPLATE,
    POLICY_MD_TEMPLATE,
)

# ── hook wrapper (identical for claude_code and codex) ───────────────────────

_HOOK_WRAPPER = """\
#!/usr/bin/env python3
import subprocess, sys
result = subprocess.run(
    ["bouncer", "classify", "--hook"],
    input=sys.stdin.read(), capture_output=True, text=True,
)
if result.stdout: print(result.stdout, end="")
if result.stderr: print(result.stderr, end="", file=sys.stderr)
sys.exit(result.returncode)
"""

# ── auto-detect ───────────────────────────────────────────────────────────────

_HARNESS_ROOTS = {
    "claude_code": Path.home() / ".claude",
    "codex":       Path.home() / ".codex",
    "opencode":    Path.home() / ".config" / "opencode",
}


def _detect_harnesses():
    return [name for name, root in _HARNESS_ROOTS.items() if root.exists()]


# ── per-harness install ───────────────────────────────────────────────────────

def _install_claude_code():
    hooks_dir  = Path.home() / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    hook_script = hooks_dir / "bouncer_hook.py"
    hook_script.write_text(_HOOK_WRAPPER, encoding="utf-8")
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    settings_path = Path.home() / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}

    hooks = settings.setdefault("hooks", {})

    # PreToolUse
    pre = hooks.setdefault("PreToolUse", [])
    script_str = str(hook_script)
    if not any(
        h.get("matcher") == "Bash" and
        any(x.get("command") == script_str for x in h.get("hooks", []))
        for h in pre
    ):
        pre.append({
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": script_str, "timeout": 30000}],
        })

    # UserPromptSubmit
    submit = hooks.setdefault("UserPromptSubmit", [])
    if not any(
        any(x.get("command") == "bouncer log --break" for x in h.get("hooks", []))
        for h in submit
    ):
        submit.append({"hooks": [{"type": "command", "command": "bouncer log --break"}]})

    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"  {GREEN}Installed{RESET} {hook_script}")
    print(f"  {GREEN}Patched{RESET}   {settings_path}")


def _install_codex():
    repo_hook = Path(__file__).parent.parent.parent / "integrations" / "codex" / "bouncer_hook.py"
    hooks_dir = Path.home() / ".codex" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    dst = hooks_dir / "bouncer_hook.py"
    if repo_hook.exists():
        shutil.copy2(repo_hook, dst)
    else:
        dst.write_text(_HOOK_WRAPPER, encoding="utf-8")
    dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    hooks_json = Path.home() / ".codex" / "hooks.json"
    cfg = json.loads(hooks_json.read_text()) if hooks_json.exists() else {}
    hook_list = cfg.setdefault("hooks", [])
    dst_str = "~/.codex/hooks/bouncer_hook.py"
    if not any(h.get("command") == dst_str for h in hook_list):
        hook_list.append({"matcher": "tool == \"Bash\"", "command": dst_str, "timeout": 30})
    hooks_json.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"  {GREEN}Installed{RESET} {dst}")
    print(f"  {GREEN}Patched{RESET}   {hooks_json}")


def _install_opencode():
    repo_plugin = Path(__file__).parent.parent.parent / "integrations" / "opencode" / "bouncer_plugin.ts"
    plugins_dir = Path.home() / ".config" / "opencode" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)

    dst = plugins_dir / "bouncer.ts"
    if repo_plugin.exists():
        shutil.copy2(repo_plugin, dst)
    else:
        print(f"  {YELLOW}Warning:{RESET} plugin source not found at {repo_plugin}; skipping copy")
        return

    oc_json = Path.home() / ".config" / "opencode" / "opencode.json"
    cfg = json.loads(oc_json.read_text()) if oc_json.exists() else {}
    plugins = cfg.setdefault("plugin", [])
    if "bouncer" not in plugins:
        plugins.append("bouncer")
    oc_json.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"  {GREEN}Installed{RESET} {dst}")
    print(f"  {GREEN}Patched{RESET}   {oc_json}")


_INSTALLERS = {
    "claude_code": _install_claude_code,
    "codex":       _install_codex,
    "opencode":    _install_opencode,
}

# ── command ───────────────────────────────────────────────────────────────────

def cmd_init(args):
    harness = getattr(args, "harness", None)

    # ── project init ──────────────────────────────────────────────────────────
    bouncer_dir   = Path.cwd() / PROJECT_DIR_NAME
    config_target = bouncer_dir / "config.yaml"
    policy_target = bouncer_dir / "policy.md"

    if config_target.exists() or policy_target.exists():
        print(f"{YELLOW}Note:{RESET} .bouncer already initialized — skipping project files.")
    else:
        bouncer_dir.mkdir(exist_ok=True)
        config_target.write_text(CONFIG_YAML_TEMPLATE, encoding="utf-8")
        policy_target.write_text(POLICY_MD_TEMPLATE, encoding="utf-8")
        gitignore = bouncer_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("log.jsonl\nconfig.local.yaml\n", encoding="utf-8")
        print(f"{GREEN}Created{RESET} {config_target}")
        print(f"{GREEN}Created{RESET} {policy_target}")
        print(f"         {DIM}{gitignore}{RESET}  (protects log + config.local.yaml)")

    # ── harness wiring ────────────────────────────────────────────────────────
    if harness:
        targets = [harness] if harness != "auto" else _detect_harnesses()
        if not targets:
            print(f"{YELLOW}No supported AI harnesses detected.{RESET} "
                  "Pass --harness=<name> to wire one explicitly.")
        for name in targets:
            print(f"\n{BOLD}Wiring {name}{RESET}")
            _INSTALLERS[name]()

    # ── next steps ────────────────────────────────────────────────────────────
    print()
    print(f"Edit policy with: {BOLD}bouncer policy{RESET}")
    print(f"Edit config with: {BOLD}bouncer config{RESET}")
    if not harness:
        print(f"Wire a harness:   {BOLD}bouncer init --harness=auto{RESET}  "
              f"(or --harness=claude_code / codex / opencode)")
