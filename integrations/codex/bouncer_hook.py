#!/usr/bin/env python3
"""
Codex CLI → bouncer bridge.

Install:  cp bouncer_hook.py ~/.codex/hooks/bouncer_hook.py
          chmod +x ~/.codex/hooks/bouncer_hook.py

Codex sends a PermissionRequest JSON payload on stdin when it is about to ask
the user for approval. Bouncer pre-triages that approval request:
ALLOW auto-approves, DENY blocks, and UNSURE emits no decision so Codex asks
the user normally.
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
    [_bouncer_cmd(), "classify", "--hook", "--format", "codex-permission"],
    input=data,
    capture_output=True,
    text=True,
)
if result.stdout:
    print(result.stdout, end="")
if result.stderr:
    print(result.stderr, end="", file=sys.stderr)
sys.exit(result.returncode)
