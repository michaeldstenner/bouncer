#!/usr/bin/env python3
"""
Codex CLI → bouncer bridge.

Install:  cp bouncer_hook.py ~/.codex/hooks/bouncer_hook.py
          chmod +x ~/.codex/hooks/bouncer_hook.py

Codex sends a PreToolUse JSON payload on stdin; its schema is identical to
Claude Code's, so this wrapper is a straight pass-through to bouncer classify.

Codex interprets the same hookSpecificOutput protocol as Claude Code:
  permissionDecision "allow"  → skip Codex's own prompt
  permissionDecision "deny"   → block (also exit 2 with reason on stderr)
  permissionDecision "ask"    → escalate to Codex's built-in approval UI
"""

import subprocess
import sys

result = subprocess.run(
    ["bouncer", "classify", "--hook"],
    input=sys.stdin.read(),
    capture_output=True,
    text=True,
)
if result.stdout:
    print(result.stdout, end="")
if result.stderr:
    print(result.stderr, end="", file=sys.stderr)
sys.exit(result.returncode)
