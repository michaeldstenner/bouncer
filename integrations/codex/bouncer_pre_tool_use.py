#!/usr/bin/env python3
"""
Optional Codex PreToolUse hard-guard bridge.

This is not installed by default. Codex PreToolUse cannot ask the user; it can
only block or pass through. Use this only when you want bouncer to deny commands
Codex would otherwise run without its normal approval prompt.
"""

import subprocess
import sys

result = subprocess.run(
    ["bouncer", "classify", "--hook", "--format", "codex-pretool"],
    input=sys.stdin.read(),
    capture_output=True,
    text=True,
)
if result.stdout:
    print(result.stdout, end="")
if result.stderr:
    print(result.stderr, end="", file=sys.stderr)
sys.exit(result.returncode)
