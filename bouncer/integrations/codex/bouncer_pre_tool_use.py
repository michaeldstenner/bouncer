#!/usr/bin/env python3
"""
Legacy Codex PreToolUse bridge.

This is not installed by default and is not recommended for normal bouncer use.
Codex PreToolUse cannot ask the user; it can only block or pass through. Keep
the PermissionRequest integration as the normal Codex path.
"""

import os
import json
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


raw = sys.stdin.read()
try:
    payload = json.loads(raw)
    payload.setdefault("harness", "codex")
    data = json.dumps(payload)
except Exception:
    data = raw

result = subprocess.run(
    [_bouncer_cmd(), "classify", "--hook", "--format", "codex-pretool"],
    input=data,
    capture_output=True,
    text=True,
)
if result.stdout:
    print(result.stdout, end="")
if result.stderr:
    print(result.stderr, end="", file=sys.stderr)
sys.exit(result.returncode)
