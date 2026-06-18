#!/usr/bin/env python3
"""Drive Claude Code headlessly with `claude -p` and observe bouncer's verdict.

This is the preferred Claude driver: `claude -p` runs the agent non-interactively
and exits, so there is no TUI to wrangle (no readiness/submit races) and hook
output surfaces in full. Determinism comes from the project `.bouncer/config.yaml`
pointing bouncer's LLM at the stub (set up by run.py).

We triangulate the same three channels as the tmux pattern: the bouncer decision
log (authoritative), the filesystem flag (ground truth), and the captured agent
output (context). For the interactive TUI harnesses that have no `-p` equivalent
(e.g. Codex TUI) see claude_tmux.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Observation:
    decision: str | None     # terminal bouncer decision seen in the log
    flag_present: bool       # filesystem ground truth
    pane: str                # captured agent output (for debugging)
    consulted: bool          # did a decision get logged at all?


def _terminal_decision(log_path: Path, sentinel: str) -> str | None:
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


def run_claude_scenario(
    *,
    project_dir: Path,
    out_dir: Path,
    command: str,
    sentinel: str,
    flag_name: str,
    log_path: Path,
    timeout: float = 150.0,
    **_ignored,
) -> Observation:
    if shutil.which("claude") is None:
        raise RuntimeError("claude not found on PATH")

    prompt = f"Run exactly this shell command and nothing else: {command}"
    try:
        # Pass the prompt on stdin (`claude -p` reads it there); this matches the
        # invocation that reliably fires the hooks in a nested session.
        proc = subprocess.run(
            ["claude", "-p"], input=prompt,
            cwd=str(project_dir), capture_output=True, text=True, timeout=timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        output = f"[timeout after {timeout}s]\n{exc.stdout or ''}{exc.stderr or ''}"

    decision = _terminal_decision(log_path, sentinel)
    return Observation(
        decision=decision,
        flag_present=(out_dir / flag_name).exists(),
        pane=output,
        consulted=decision is not None,
    )
