#!/usr/bin/env python3
"""Drive a real Claude Code session in a detached tmux window and observe how
bouncer's PreToolUse hook decides a single tool call.

This is the automation behind the agent-based slice. It follows the project's
"driving CLI agents in tmux" pattern: launch the agent detached, inject a prompt
with send-keys, and triangulate three channels — the bouncer decision log
(authoritative), the filesystem (ground truth), and the captured pane (context).

Determinism comes from the *project* `.bouncer/config.yaml` pointing bouncer's
LLM at the stub (set up by run.py); this driver only handles the harness.

Gotchas handled (learned the hard way, see squirrel
docs/patterns/driving-cli-agents-in-tmux.md):
  - first-run trust prompt in a fresh dir -> accept it
  - Claude Code often needs a second Enter to submit a send-keys buffer
  - the agent thinks for seconds -> poll the log, don't conclude from one capture
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Observation:
    decision: str | None          # terminal bouncer decision seen in the log
    flag_present: bool            # filesystem ground truth
    pane: str                     # captured screen (for debugging)
    consulted: bool               # did a log entry appear at all


def _tmux(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check)


def _capture(session: str, lines: int = 200) -> str:
    return _tmux("capture-pane", "-p", "-t", session, "-S", f"-{lines}").stdout


def _terminal_decision(log_path: Path, sentinel: str) -> str | None:
    """Most recent non-PENDING decision whose input mentions the sentinel."""
    if not log_path.exists():
        return None
    found = None
    for line in log_path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if sentinel in rec.get("input_summary", "") and rec.get("decision") != "PENDING":
            found = rec["decision"]
    return found


def _wait_for_text(session: str, needles: tuple[str, ...], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pane = _capture(session).lower()
        if any(n.lower() in pane for n in needles):
            return True
        time.sleep(1.0)
    return False


def _wait_ready(session: str, timeout: float) -> bool:
    """Wait for the REPL input prompt (❯) to appear and stay stable. The welcome
    box renders almost immediately, so we key on the prompt glyph and require it
    to persist across two captures — otherwise keystrokes land before the REPL
    accepts input and are silently dropped."""
    deadline = time.monotonic() + timeout
    stable = 0
    while time.monotonic() < deadline:
        pane = _capture(session, 25)
        low = pane.lower()
        ready = "❯" in pane and "trust this folder" not in low and "do you trust" not in low
        stable = stable + 1 if ready else 0
        if stable >= 2:
            return True
        time.sleep(1.5)
    return False


def run_claude_scenario(
    *,
    session: str,
    project_dir: Path,
    out_dir: Path,
    command: str,
    sentinel: str,
    flag_name: str,
    log_path: Path,
    launch_cmd: str = "claude",
    boot_timeout: float = 40.0,
    settle_timeout: float = 120.0,
) -> Observation:
    """Launch Claude in tmux, ask it to run `command`, and observe the outcome."""
    if shutil.which("tmux") is None:
        raise RuntimeError("tmux not found on PATH")

    _tmux("kill-session", "-t", session)
    _tmux("new-session", "-d", "-s", session, "-x", "200", "-y", "50",
          "-c", str(project_dir), check=True)
    try:
        # Launch the agent. The global bouncer PreToolUse hook is already active;
        # the project .bouncer routes its verdict through the stub.
        _tmux("send-keys", "-t", session, launch_cmd, "Enter")

        # First run in a fresh dir shows a trust prompt; accept it if it appears.
        if _wait_for_text(session, ("trust this folder", "do you trust"), 12.0):
            _tmux("send-keys", "-t", session, "Enter")
            time.sleep(3.0)

        # Wait for the REPL prompt to be ready and stable before typing.
        if not _wait_ready(session, boot_timeout):
            return Observation(None, False, _capture(session), False)

        prompt = f"Run exactly this shell command and nothing else: {command}"
        _tmux("send-keys", "-t", session, prompt)
        time.sleep(0.6)
        # Submit. Claude Code sometimes needs a second Enter; re-send until the
        # command leaves the input line (bottom of the pane).
        for _ in range(3):
            _tmux("send-keys", "-t", session, "Enter")
            time.sleep(1.5)
            bottom = _capture(session, 6)
            if not ("❯" in bottom and command[-15:] in bottom):
                break

        # Poll the authoritative signal: a terminal decision in the log.
        deadline = time.monotonic() + settle_timeout
        decision = None
        while time.monotonic() < deadline:
            decision = _terminal_decision(log_path, sentinel)
            if decision:
                break
            time.sleep(2.0)

        flag = (out_dir / flag_name).exists()
        return Observation(
            decision=decision,
            flag_present=flag,
            pane=_capture(session),
            consulted=decision is not None,
        )
    finally:
        _tmux("send-keys", "-t", session, "C-c")
        _tmux("kill-session", "-t", session)
