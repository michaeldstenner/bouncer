import os
import shlex
import subprocess
import sys
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
)
from .lint import cmd_lint


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
    else:
        d = _find_bouncer_dir()
        if d is None:
            print(f"{YELLOW}No .bouncer directory found.{RESET} Run 'bouncer init' first.")
            sys.exit(1)
        path = d / "policy.md"
        if not path.exists():
            path.write_text(POLICY_MD_TEMPLATE, encoding="utf-8")
            print(f"{GREEN}Created{RESET} {path}")

    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
    subprocess.run(shlex.split(editor) + [str(path)])
