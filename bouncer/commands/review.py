import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from ..colors import RESET, BOLD, GREEN, YELLOW, DIM
from ..config import (
    USER_LOG_FILE,
    USER_POLICY_FILE,
    USER_POLICY_MD_TEMPLATE,
    POLICY_MD_TEMPLATE,
    _find_bouncer_dir,
    project_log_file,
)
from .log import _extract_command


def cmd_review(args):
    if args.user:
        log_file        = USER_LOG_FILE
        policy_path     = USER_POLICY_FILE
        policy_template = USER_POLICY_MD_TEMPLATE
    else:
        lf = project_log_file()
        if lf is None:
            print(f"{YELLOW}No project config found.{RESET} Run 'bouncer init' first.")
            sys.exit(1)
        log_file = lf
        d = _find_bouncer_dir()
        if d is None:
            print(f"{YELLOW}No project config found.{RESET} Run 'bouncer init' first.")
            sys.exit(1)
        policy_path     = d / "policy.md"
        policy_template = POLICY_MD_TEMPLATE

    if not log_file or not log_file.exists():
        print(f"{DIM}No log entries to review.{RESET}")
        return

    seen: set[str] = set()
    entries = []
    with open(log_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("decision") != "UNSURE":
                continue
            cmd = _extract_command(entry.get("input_summary", ""))
            if cmd and cmd not in seen:
                seen.add(cmd)
                entry["_cmd"] = cmd
                entries.append(entry)

    if not entries:
        print(f"{GREEN}No UNSURE decisions to review.{RESET}")
        return

    print(f"{BOLD}Review UNSURE decisions{RESET} ({len(entries)} unique commands)")
    print(f"Policy file: {policy_path}\n")

    for entry in entries:
        cmd    = entry["_cmd"]
        reason = entry.get("reason", "")
        print(f"  {YELLOW}UNSURE{RESET}  {cmd}")
        print(f"  {DIM}{reason}{RESET}")
        print("  [n]ote  [e]dit policy.md  [s]kip  [q]uit: ", end="", flush=True)
        try:
            choice = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice == "q":
            break
        elif choice in ("n", "note"):
            print("  Note: ", end="", flush=True)
            try:
                note = input().strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if note:
                if not policy_path.exists():
                    policy_path.write_text(policy_template, encoding="utf-8")
                with open(policy_path, "a", encoding="utf-8") as f:
                    f.write(f"\n- {note}\n")
                print(f"  {GREEN}→ appended to {policy_path.name}{RESET}")
        elif choice in ("e", "edit"):
            if not policy_path.exists():
                policy_path.write_text(policy_template, encoding="utf-8")
            editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
            subprocess.run(shlex.split(editor) + [str(policy_path)])
        print()
