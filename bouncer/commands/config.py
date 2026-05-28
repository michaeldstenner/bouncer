import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from ..colors import RESET, GREEN, YELLOW
from ..config import (
    USER_CONFIG_DIR,
    USER_CONFIG_FILE,
    USER_POLICY_FILE,
    USER_CONFIG_YAML_TEMPLATE,
    USER_POLICY_MD_TEMPLATE,
    POLICY_MD_TEMPLATE,
    _find_bouncer_dir,
    load_policy,
    build_policy_tempfile,
    split_policy_tempfile,
)
from .lint import cmd_lint


def _set_enabled(path: Path, value: bool) -> None:
    text = path.read_text(encoding="utf-8")
    replacement = f"enabled: {'true' if value else 'false'}"
    new_text, n = re.subn(r"^enabled:\s*(true|false)", replacement, text, flags=re.MULTILINE)
    if n == 0:
        new_text = replacement + "\n" + text
    path.write_text(new_text, encoding="utf-8")
    state = f"{GREEN}enabled{RESET}" if value else f"{YELLOW}disabled{RESET}"
    print(f"bouncer {state}  ({path})")


def cmd_config(args):
    if args.user:
        USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path = USER_CONFIG_FILE
        if not path.exists():
            path.write_text(USER_CONFIG_YAML_TEMPLATE, encoding="utf-8")
            print(f"{GREEN}Created{RESET} {path}")
    else:
        d = _find_bouncer_dir()
        if d is None or not (d / "config.yaml").exists():
            print(f"{YELLOW}No project config found.{RESET} Run 'bouncer init' first.")
            sys.exit(1)
        path = d / "config.yaml"

    if getattr(args, "enable", False):
        _set_enabled(path, True)
        return
    if getattr(args, "disable", False):
        _set_enabled(path, False)
        return

    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
    subprocess.run(shlex.split(editor) + [str(path)])
    print()

    class _A:
        file = str(path)
    cmd_lint(_A())


def cmd_policy(args):
    if args.user:
        USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path = USER_POLICY_FILE
        if not path.exists():
            path.write_text(USER_POLICY_MD_TEMPLATE, encoding="utf-8")
            print(f"{GREEN}Created{RESET} {path}")
        editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
        subprocess.run(shlex.split(editor) + [str(path)])
        return

    d = _find_bouncer_dir()
    if d is None:
        print(f"{YELLOW}No .bouncer directory found.{RESET} Run 'bouncer init' first.")
        sys.exit(1)

    committed_path = d / "policy.md"
    local_path = d / "policy.local.md"
    committed = load_policy(committed_path)
    local = load_policy(local_path)

    tempfile_content = build_policy_tempfile(committed, local)
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="bouncer-policy-",
        delete=False, encoding="utf-8"
    ) as tf:
        tf.write(tempfile_content)
        tf_path = Path(tf.name)

    try:
        subprocess.run(shlex.split(editor) + [str(tf_path)])
        new_content = tf_path.read_text(encoding="utf-8")
    finally:
        tf_path.unlink(missing_ok=True)

    new_committed, new_local = split_policy_tempfile(new_content)

    if new_committed:
        committed_path.write_text(new_committed + "\n", encoding="utf-8")
        if not committed:
            print(f"{GREEN}Created{RESET} {committed_path}")
    elif committed_path.exists():
        committed_path.unlink()
        print(f"{YELLOW}Removed{RESET} {committed_path} (empty)")

    if new_local:
        local_path.write_text(new_local + "\n", encoding="utf-8")
        if not local:
            print(f"{GREEN}Created{RESET} {local_path}")
    elif local_path.exists():
        local_path.unlink()
        print(f"{YELLOW}Removed{RESET} {local_path} (empty)")
