import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

from ..colors import RESET, BOLD, GREEN, YELLOW, RED, DIM
from ..config import (
    PROJECT_DIR_NAME,
    CONFIG_YAML_TEMPLATE,
    POLICY_MD_TEMPLATE,
    USER_CONFIG_DIR,
    USER_CONFIG_FILE,
    USER_POLICY_FILE,
    USER_CONFIG_YAML_TEMPLATE,
    USER_POLICY_MD_TEMPLATE,
)

def _hook_wrapper(harness: str, fmt: str = "json") -> str:
    return f"""\
#!/usr/bin/env python3
import json, subprocess, sys
raw = sys.stdin.read()
try:
    payload = json.loads(raw)
    payload.setdefault("harness", {harness!r})
    data = json.dumps(payload)
except Exception:
    data = raw
result = subprocess.run(
    ["bouncer", "classify", "--hook", "--format", {fmt!r}],
    input=data, capture_output=True, text=True,
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

_SHIM_INSTALL_DIR = Path.home() / ".local" / "share" / "bouncer" / "shim"


def _detect_harnesses():
    return [name for name, root in _HARNESS_ROOTS.items() if root.exists()]


# ── per-harness install ───────────────────────────────────────────────────────

def _install_claude_code():
    hooks_dir  = Path.home() / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    hook_script = hooks_dir / "bouncer_hook.py"
    hook_script.write_text(_hook_wrapper("claude_code"), encoding="utf-8")
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
        dst.write_text(_hook_wrapper("codex", "codex-permission"), encoding="utf-8")
    dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    hooks_json = Path.home() / ".codex" / "hooks.json"
    cfg = json.loads(hooks_json.read_text()) if hooks_json.exists() else {}
    hooks = cfg.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        cfg["hooks"] = hooks

    dst_str = str(dst)
    legacy_dst_str = "~/.codex/hooks/bouncer_hook.py"

    # Older bouncer installs used PreToolUse. Codex cannot ask from that hook,
    # so default installs remove bouncer's old PreToolUse entry and use
    # PermissionRequest instead to pre-triage native Codex approvals.
    pre = hooks.get("PreToolUse", [])
    if isinstance(pre, list):
        cleaned = []
        for group in pre:
            handlers = [
                h for h in group.get("hooks", [])
                if h.get("command") not in (dst_str, legacy_dst_str)
            ]
            if handlers:
                new_group = dict(group)
                new_group["hooks"] = handlers
                cleaned.append(new_group)
        if cleaned:
            hooks["PreToolUse"] = cleaned
        else:
            hooks.pop("PreToolUse", None)

    req = hooks.setdefault("PermissionRequest", [])
    if not any(
        group.get("matcher") == "Bash"
        and any(h.get("command") in (dst_str, legacy_dst_str) for h in group.get("hooks", []))
        for group in req
    ):
        req.append({
            "matcher": "Bash",
            "hooks": [{
                "type": "command",
                "command": dst_str,
                "timeout": 30,
                "statusMessage": "Bouncer reviewing approval",
            }],
        })
    for group in req:
        if group.get("matcher") != "Bash":
            continue
        for hook in group.get("hooks", []):
            if hook.get("command") in (dst_str, legacy_dst_str):
                hook["statusMessage"] = "Bouncer reviewing approval"
    hooks_json.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"  {GREEN}Installed{RESET} {dst}")
    print(f"  {GREEN}Patched{RESET}   {hooks_json}")


def _install_codex_pretool():
    repo_hook = Path(__file__).parent.parent.parent / "integrations" / "codex" / "bouncer_pre_tool_use.py"
    hooks_dir = Path.home() / ".codex" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    dst = hooks_dir / "bouncer_pre_tool_use.py"
    if repo_hook.exists():
        shutil.copy2(repo_hook, dst)
    else:
        print(f"  {YELLOW}Warning:{RESET} hook source not found at {repo_hook}; skipping copy")
        return
    dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    hooks_json = Path.home() / ".codex" / "hooks.json"
    cfg = json.loads(hooks_json.read_text()) if hooks_json.exists() else {}
    hooks = cfg.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        cfg["hooks"] = hooks

    dst_str = str(dst)
    pre = hooks.setdefault("PreToolUse", [])
    if not any(
        group.get("matcher") == "Bash"
        and any(h.get("command") == dst_str for h in group.get("hooks", []))
        for group in pre
    ):
        pre.append({
            "matcher": "Bash",
            "hooks": [{
                "type": "command",
                "command": dst_str,
                "timeout": 30,
                "statusMessage": "Bouncer reviewing command",
            }],
        })
    for group in pre:
        if group.get("matcher") != "Bash":
            continue
        for hook in group.get("hooks", []):
            if hook.get("command") == dst_str:
                hook["statusMessage"] = "Bouncer reviewing command"

    hooks_json.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"  {GREEN}Installed{RESET} {dst}")
    print(f"  {GREEN}Patched{RESET}   {hooks_json}")


def _install_opencode():
    repo_plugin = Path(__file__).parent.parent.parent / "integrations" / "opencode" / "bouncer_plugin.ts"
    opencode_dir = Path.home() / ".config" / "opencode"
    config_path = _opencode_config_path(opencode_dir)
    if config_path is None:
        return

    plugins_dir = opencode_dir / "plugin"
    plugins_dir.mkdir(parents=True, exist_ok=True)

    dst = plugins_dir / "bouncer.ts"
    if repo_plugin.exists():
        shutil.copy2(repo_plugin, dst)
    else:
        print(f"  {YELLOW}Warning:{RESET} plugin source not found at {repo_plugin}; skipping copy")
        return

    cfg = _read_opencode_config(config_path)
    plugins = cfg.setdefault("plugin", [])
    if "bouncer" in plugins:
        changed = False
    else:
        plugins.append("bouncer")
        changed = True
    if changed:
        config_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"  {GREEN}Installed{RESET} {dst}")
    action = "Patched" if changed else "Unchanged"
    print(f"  {GREEN}{action}{RESET}   {config_path}")


def _opencode_config_path(opencode_dir: Path) -> Path | None:
    json_path = opencode_dir / "opencode.json"
    jsonc_path = opencode_dir / "opencode.jsonc"
    json_exists = json_path.exists()
    jsonc_exists = jsonc_path.exists()
    if json_exists and jsonc_exists:
        print(
            f"  {YELLOW}Warning:{RESET} both {json_path} and {jsonc_path} exist; "
            "not patching opencode config. Remove or merge one file and retry."
        )
        return None
    if jsonc_exists:
        return jsonc_path
    return json_path


def _read_opencode_config(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonc":
        text = _strip_json_comments(text)
    return json.loads(text)


def _strip_json_comments(text: str) -> str:
    # JSONC comments are allowed in opencode.jsonc; Python's json module does
    # not support them. This conservative stripper leaves quoted strings intact
    # and removes // line comments plus /* block comments */.
    pattern = r'("(?:\\.|[^"\\])*")|(//[^\n\r]*|/\*.*?\*/)'

    def repl(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            return match.group(1)
        return ""

    return re.sub(pattern, repl, text, flags=re.DOTALL)


def _install_shim():
    repo_shim = Path(__file__).parent.parent / "shim" / "bash"
    _SHIM_INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    dst = _SHIM_INSTALL_DIR / "bash"
    if repo_shim.exists():
        shutil.copy2(repo_shim, dst)
    else:
        print(f"  {YELLOW}Warning:{RESET} shim source not found at {repo_shim}; skipping copy")
        return
    dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(f"  {GREEN}Installed{RESET} {dst}")
    print()
    print("  To protect an agent that runs commands via the shell, launch it with:")
    print(f'    {BOLD}PATH="{_SHIM_INSTALL_DIR}:$PATH" <your-agent-command>{RESET}')
    print("  The shim gates 'bash -c' calls in any project with .bouncer/config.yaml.")
    print("  ASK is not available there — internal ASK/escalation outcomes are delivered as denials.")


_INSTALLERS = {
    "claude_code": _install_claude_code,
    "codex":       _install_codex,
    "codex_pretool": _install_codex_pretool,
    "opencode":    _install_opencode,
    "shim":        _install_shim,
}

_DEFAULT_INSTALLERS = ("claude_code", "codex", "opencode", "shim")

_HARNESS_PROMPTS = {
    "claude_code": "Add PreToolUse hook to ~/.claude/settings.json",
    "codex":       "Add PermissionRequest hook to ~/.codex/hooks.json",
    "codex_pretool": "Add legacy Codex PreToolUse hard-guard hook to ~/.codex/hooks.json",
    "opencode":    "Copy bouncer_plugin.ts to ~/.config/opencode/plugin/bouncer.ts",
    "shim":        f"Install shell shim to {_SHIM_INSTALL_DIR}/bash (universal PATH-based gate)",
}


def _is_installed_claude_code() -> bool:
    hook_script = str(Path.home() / ".claude" / "hooks" / "bouncer_hook.py")
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return False
    try:
        settings = json.loads(settings_path.read_text())
    except Exception:
        return False
    pre = settings.get("hooks", {}).get("PreToolUse", [])
    return any(
        any(x.get("command") == hook_script for x in h.get("hooks", []))
        for h in pre
    )


def _is_installed_codex() -> bool:
    hook_script = str(Path.home() / ".codex" / "hooks" / "bouncer_hook.py")
    hooks_json = Path.home() / ".codex" / "hooks.json"
    if not hooks_json.exists():
        return False
    try:
        cfg = json.loads(hooks_json.read_text())
    except Exception:
        return False
    hooks = cfg.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    req = hooks.get("PermissionRequest", [])
    return any(
        any(h.get("command") == hook_script for h in group.get("hooks", []))
        for group in req
    )


def _is_installed_codex_pretool() -> bool:
    hook_script = str(Path.home() / ".codex" / "hooks" / "bouncer_pre_tool_use.py")
    hooks_json = Path.home() / ".codex" / "hooks.json"
    if not hooks_json.exists():
        return False
    try:
        cfg = json.loads(hooks_json.read_text())
    except Exception:
        return False
    hooks = cfg.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    pre = hooks.get("PreToolUse", [])
    return any(
        any(h.get("command") == hook_script for h in group.get("hooks", []))
        for group in pre
    )


def _is_installed_opencode() -> bool:
    return (Path.home() / ".config" / "opencode" / "plugin" / "bouncer.ts").exists()


def _is_installed_shim() -> bool:
    return (_SHIM_INSTALL_DIR / "bash").exists()


_IS_INSTALLED = {
    "claude_code": _is_installed_claude_code,
    "codex":       _is_installed_codex,
    "codex_pretool": _is_installed_codex_pretool,
    "opencode":    _is_installed_opencode,
    "shim":        _is_installed_shim,
}


def _resolve_harness_targets(arg: str | None) -> list[str]:
    """Map --harness <arg> to a list of installer names. None → []."""
    if arg is None:
        return []
    if arg == "all":
        return list(_DEFAULT_INSTALLERS)
    if arg == "auto":
        return _detect_harnesses()
    return [h.strip().replace("-", "_") for h in arg.split(",")]


def _has_installed_harness() -> bool:
    return bool(installed_harnesses())


def installed_harnesses() -> list[str]:
    """Return installed bouncer harness integrations."""
    return [name for name in _INSTALLERS if _IS_INSTALLED[name]()]


def format_installed_harnesses() -> str:
    names = installed_harnesses()
    return ", ".join(names) if names else "(none)"


def _install_targets(targets: list[str], *, refresh: bool = False) -> None:
    """Install the given harnesses.

    In interactive / auto-detect flows we skip already-installed harnesses to
    avoid surprising overwrites. In explicit `--harness=...` flows we refresh
    the installer output so users can push updated hook/plugin versions.
    """
    for name in targets:
        if name not in _INSTALLERS:
            print(f"{RED}Unknown harness:{RESET} {name!r}  "
                  f"(valid: {', '.join(_INSTALLERS)})")
            continue
        if _IS_INSTALLED[name]():
            if not refresh:
                print(f"\n  {name}: already installed — skipping.")
                continue
            print(f"\n{BOLD}Refreshing {name}{RESET}")
        else:
            print(f"\n{BOLD}Wiring {name}{RESET}")
        _INSTALLERS[name]()


# ── commands ──────────────────────────────────────────────────────────────────

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
            gitignore.write_text(
                "log.jsonl\nllm_debug.jsonl\nconfig.local.yaml\npolicy.local.md\n",
                encoding="utf-8",
            )
        print(f"{GREEN}Created{RESET} {config_target}")
        print(f"{GREEN}Created{RESET} {policy_target}")
        print(f"         {DIM}{gitignore}{RESET}  (protects logs + config.local.yaml + policy.local.md)")

    # ── harness wiring ────────────────────────────────────────────────────────
    if harness:
        targets = _resolve_harness_targets(harness)
        if not targets:
            print(f"{YELLOW}No supported AI harnesses detected.{RESET} "
                  "Pass --harness=<name> to wire one explicitly.")
        _install_targets(targets, refresh=True)

    # ── next steps ────────────────────────────────────────────────────────────
    print()
    print(f"Edit policy with: {BOLD}bouncer policy{RESET}")
    print(f"Edit config with: {BOLD}bouncer config{RESET}")
    print(f"Installed integrations: {BOLD}{format_installed_harnesses()}{RESET}")
    if not harness and not _has_installed_harness():
        print(f"{YELLOW}Note:{RESET} bouncer is enabled for this project, but no harness "
              "integration appears to be installed.")
        print(f"Wire harnesses with: {BOLD}bouncer -g init{RESET}")


def cmd_global_init(args):
    harness_arg = getattr(args, "harness", None)

    # ── user-level config and policy ──────────────────────────────────────────
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    if USER_CONFIG_FILE.exists():
        print(f"{YELLOW}Note:{RESET} {USER_CONFIG_FILE} already exists — skipping.")
    else:
        USER_CONFIG_FILE.write_text(USER_CONFIG_YAML_TEMPLATE, encoding="utf-8")
        print(f"{GREEN}Created{RESET} {USER_CONFIG_FILE}")

    if USER_POLICY_FILE.exists():
        print(f"{YELLOW}Note:{RESET} {USER_POLICY_FILE} already exists — skipping.")
    else:
        USER_POLICY_FILE.write_text(USER_POLICY_MD_TEMPLATE, encoding="utf-8")
        print(f"{GREEN}Created{RESET} {USER_POLICY_FILE}")

    # ── harness wiring ────────────────────────────────────────────────────────
    # The shim is a universal gate, not a detected harness. Always offer it
    # in interactive mode; include it in --harness=all.
    if harness_arg is None:
        to_prompt = _detect_harnesses() + ["shim"]
        for name in to_prompt:
            if _IS_INSTALLED[name]():
                print(f"\n  {name}: already installed — skipping.")
                continue
            prompt = _HARNESS_PROMPTS[name]
            try:
                answer = input(f"\n  {prompt}? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer == "y":
                print(f"{BOLD}Wiring {name}{RESET}")
                _INSTALLERS[name]()
    else:
        targets = _resolve_harness_targets(harness_arg)
        if not targets:
            print(f"\n{YELLOW}No supported AI harnesses detected.{RESET}")
        _install_targets(targets, refresh=True)

    # ── next steps ────────────────────────────────────────────────────────────
    print()
    print(f"Edit user policy with: {BOLD}bouncer -g policy{RESET}")
    print(f"Edit user config with: {BOLD}bouncer -g config{RESET}")
    print(f"Enable a project with: {BOLD}bouncer init{RESET}")
