#!/usr/bin/env python3
"""Drive Codex CLI headlessly with `codex exec` and observe bouncer's verdict."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


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


def run_codex_scenario(
    *,
    project_dir: Path,
    out_dir: Path,
    command: str,
    sentinel: str,
    flag_name: str,
    log_path: Path,
    timeout: float = 240.0,
    **_ignored,
) -> Observation:
    if shutil.which("codex") is None:
        raise RuntimeError("codex not found on PATH")

    prompt = f"Use the shell to run exactly this command and nothing else: {command}"
    env = os.environ.copy()
    env["BOUNCER_BIN"] = str(ROOT / "bin" / "bouncer")
    try:
        proc = subprocess.run(
            [
                "codex",
                "--sandbox", "workspace-write",
                "--ask-for-approval", "untrusted",
                "--dangerously-bypass-hook-trust",
                "exec",
                "--cd", str(project_dir),
                "--skip-git-repo-check",
                prompt,
            ],
            cwd=str(project_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
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
