#!/usr/bin/env python3
"""
Optional Codex PreToolUse hard-guard bridge.

This is not installed by default. Codex PreToolUse cannot ask the user; it can
only block or pass through. Use this only when you want bouncer to deny commands
Codex would otherwise run without its normal approval prompt.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys


def _bouncer_cmd() -> str:
    configured = os.environ.get("BOUNCER_BIN")
    if configured:
        return configured
    found = shutil.which("bouncer")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "bouncer"
    return str(fallback)


result = subprocess.run(
    [_bouncer_cmd(), "classify", "--hook", "--format", "codex-pretool"],
    input=sys.stdin.read(),
    capture_output=True,
    text=True,
)
if result.stdout:
    print(result.stdout, end="")
if result.stderr:
    print(result.stderr, end="", file=sys.stderr)
sys.exit(result.returncode)
