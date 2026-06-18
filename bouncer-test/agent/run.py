#!/usr/bin/env python3
"""Agent-based bouncer test runner.

Two modes over one shared scenario matrix (scenarios.yaml):

  --hook   (default)  Deterministic plumbing check. Synthesizes PreToolUse
                      payloads and drives `bouncer classify --hook` directly —
                      proves the stub LLM, the tools-fold SKIP, and the
                      allow/deny protocol with no agent. Fast, CI-friendly.

  --agent claude      The real slice. Drives a live Claude Code session in tmux
                      (drivers/claude_tmux.py) and observes the same decisions
                      end to end. Requires `claude` and `tmux`.

Both point bouncer's LLM at an in-process stub (stub_llm.py) via a disposable
project `.bouncer/config.yaml`, so verdicts are deterministic.

    uv run python bouncer-test/agent/run.py            # hook mode
    uv run python bouncer-test/agent/run.py --agent claude
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bouncer.yaml import MicroYAML  # noqa: E402
from stub_llm import StubLLM  # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def load_scenarios() -> list[dict]:
    data = MicroYAML().load((Path(__file__).parent / "scenarios.yaml").read_text())
    return data["scenarios"]


def make_project(root: Path, stub_url: str) -> Path:
    bdir = root / ".bouncer"
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "config.yaml").write_text(
        "enabled: true\n"
        "tools: [+@all, -@internal]\n"
        "policy_mode: replace\n"
        "on_unsure: ask\n"
        "llm:\n"
        "  provider: openai_compatible\n"
        "  model: stub-model\n"
        f"  url: {stub_url}\n"
        "log:\n"
        "  verbosity: all\n"
    )
    (bdir / "policy.md").write_text(
        "# Stub test policy\n\nVerdicts come from the deterministic stub LLM.\n"
    )
    return root


def terminal_decision(log_path: Path, sentinel: str) -> str | None:
    if not log_path.exists():
        return None
    found = None
    for line in log_path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if sentinel in rec.get("input_summary", "") and rec.get("decision") != "PENDING":
            found = rec["decision"]
    return found


# --- hook mode ----------------------------------------------------------------

def run_hook_mode(scenarios, project, out_dir, log_path, stub) -> list[tuple]:
    results = []
    for sc in scenarios:
        sid, want = sc["id"], sc["decision"]
        if sc["id"] == "internal_skip":
            payload = {"tool_name": "ToolSearch",
                       "tool_input": {"query": "select:BNCR_ALLOW_probe"},
                       "cwd": str(project), "session_id": "hooktest"}
            sentinel = "BNCR_ALLOW_probe"
        else:
            cid = uuid.uuid4().hex[:8]
            command = sc["command"].format(OUT=out_dir, ID=cid)
            sentinel = command.split("/")[-1]
            payload = {"tool_name": "Bash", "tool_input": {"command": command},
                       "cwd": str(project), "session_id": "hooktest"}

        before = len(stub.find(sentinel))
        proc = subprocess.run(
            [sys.executable, "-m", "bouncer", "classify", "--hook", "--format", "json"],
            input=json.dumps(payload), capture_output=True, text=True, cwd=ROOT,
        )
        consulted = len(stub.find(sentinel)) > before
        decision = terminal_decision(log_path, sentinel)

        if want == "SKIP":
            ok = (not consulted) and decision is None and proc.returncode == 0 and not proc.stdout.strip()
            detail = f"consulted={consulted} log={decision} exit={proc.returncode}"
        elif want == "ALLOW":
            ok = decision == "ALLOW" and proc.returncode == 0 and "allow" in proc.stdout
            detail = f"log={decision} exit={proc.returncode}"
        elif want == "DENY":
            ok = decision == "DENY" and proc.returncode == 2
            detail = f"log={decision} exit={proc.returncode}"
        else:
            ok, detail = False, f"unknown expected decision {want}"
        results.append((sid, want, ok, detail))
    return results


# --- agent mode ---------------------------------------------------------------

def run_agent_mode(scenarios, project, out_dir, log_path, harness) -> list[tuple]:
    if harness != "claude":
        raise SystemExit(f"agent driver for {harness!r} not implemented yet (slice = claude)")
    from drivers.claude_headless import run_claude_scenario

    results = []
    for sc in scenarios:
        if harness not in sc.get("harnesses", []) or sc["id"] == "internal_skip":
            results.append((sc["id"], sc["decision"], None, "n/a for this slice"))
            continue
        cid = uuid.uuid4().hex[:8]
        command = sc["command"].format(OUT=out_dir, ID=cid)
        flag_name = command.split("/")[-1]
        obs = run_claude_scenario(
            session=f"bncr-agent-{sc['id']}",
            project_dir=project, out_dir=out_dir, command=command,
            sentinel=flag_name, flag_name=flag_name, log_path=log_path,
        )
        want = sc["decision"]
        flag_ok = obs.flag_present == (sc["flag"] == "present")
        ok = obs.decision == want and flag_ok
        if obs.consulted:
            detail = f"log={obs.decision} flag={'present' if obs.flag_present else 'absent'}"
        else:
            pane_file = out_dir.parent / f"{sc['id']}.pane"
            pane_file.write_text(obs.pane)
            detail = f"INCONCLUSIVE (agent never attempted) — pane: {pane_file}"
        results.append((sc["id"], want, ok, detail))
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", metavar="HARNESS",
                    help="drive a live harness (claude) instead of the hook path")
    ap.add_argument("--work-root", metavar="DIR", default=None,
                    help="parent dir for the disposable project (default: system "
                         "temp). Set this to a sandbox-allowed path (e.g. /tmp) "
                         "when running --agent from inside another sandboxed agent.")
    args = ap.parse_args()

    scenarios = load_scenarios()
    stub = StubLLM().start()
    work = Path(tempfile.mkdtemp(prefix="bouncer-agenttest-", dir=args.work_root))
    project = make_project(work / "project", stub.url)
    # Flag files live INSIDE the project tree. Writing outside the workspace
    # trips Claude Code's own workspace-boundary prompt — a separate gate from
    # the bouncer hook that a bouncer ALLOW does not suppress — which would
    # stall the agent run regardless of the verdict under test.
    out_dir = project / "out"
    out_dir.mkdir()
    log_path = project / ".bouncer" / "log.jsonl"

    mode = f"agent:{args.agent}" if args.agent else "hook"
    print(f"{DIM}stub={stub.url}  work={work}  mode={mode}{RESET}\n")
    try:
        if args.agent:
            results = run_agent_mode(scenarios, project, out_dir, log_path, args.agent)
        else:
            results = run_hook_mode(scenarios, project, out_dir, log_path, stub)
    finally:
        stub.stop()

    failures = 0
    for sid, want, ok, detail in results:
        if ok is None:
            mark = f"{YELLOW}SKIP{RESET}"
        elif ok:
            mark = f"{GREEN} OK {RESET}"
        else:
            mark = f"{RED}FAIL{RESET}"
            failures += 1
        print(f"  [{mark}] {sid:<14} expect={want:<6} {DIM}{detail}{RESET}")

    print()
    if failures:
        print(f"{RED}{failures} failed{RESET}  ({work} left for inspection)")
        sys.exit(1)
    print(f"{GREEN}all passed{RESET}")
    subprocess.run(["rm", "-rf", str(work)])


if __name__ == "__main__":
    main()
