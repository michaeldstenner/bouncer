#!/usr/bin/env python3
"""Agent-based bouncer test runner.

Two modes over one shared scenario matrix (scenarios.yaml):

  --hook   (default)  Deterministic plumbing check. Synthesizes PreToolUse
                      payloads and drives `bouncer classify --hook` directly —
                      proves the stub LLM, the tools-fold SKIP, and the
                      allow/deny protocol with no agent. Fast, CI-friendly.

  --agent claude      The real Claude slice. Drives `claude -p` and observes the
                      same decisions end to end. Requires `claude`.

  --agent codex_cli   The real Codex CLI slice. Drives `codex exec` and observes
                      the same decisions end to end. Requires `codex`.

Both point bouncer's LLM at an in-process stub (stub_llm.py) via a disposable
project `.bouncer/config.yaml`, so verdicts are deterministic.

    uv run python bouncer-test/agent/run.py            # hook mode
    uv run python bouncer-test/agent/run.py --agent claude
    uv run python bouncer-test/agent/run.py --agent codex_cli
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bouncer.proposals import calling_session  # noqa: E402
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


def seed_target(setup: str, project: Path, cid: str) -> Path:
    """The `bouncer seed` target a scenario's `setup` names, built beside
    the project. Its last component carries BNCR_ALLOW, so the stub would
    allow it: only the guard can refuse it."""
    work = project.parent
    leaf = f"seed_BNCR_ALLOW_{cid}"
    if setup == "fresh":
        target = work / leaf
        target.mkdir()
    elif setup == "armed":
        target = work / leaf
        (target / ".bouncer").mkdir(parents=True)
        (target / ".bouncer" / "config.yaml").write_text("enabled: true\n")
    elif setup == "nested":
        parent = work / f"armed_{cid}"
        (parent / ".bouncer").mkdir(parents=True)
        (parent / ".bouncer" / "config.yaml").write_text("enabled: true\n")
        target = parent / leaf
        target.mkdir()
    elif setup == "own_parent":
        # The caller is `project`; this names its parent by another path.
        target = work / leaf
        target.symlink_to(work)
    else:
        raise SystemExit(f"unknown seed setup {setup!r}")
    return target


_HOMES: list[Path] = []


def seed_home(project: Path, cid: str, caller: str) -> dict | None:
    """The environment for a seed scenario: a throwaway HOME whose user
    config names the orchestrator. `orchestrator` names the agent session
    this runner is under (the hook is its child, so the process-tree check
    finds it); `worker` names some other directory. None when there is no
    session to name: seeding is then refused whoever asks."""
    launch, _nested = calling_session()
    if caller == "orchestrator" and launch is None:
        return None
    named = launch if caller == "orchestrator" else project.parent / "elsewhere"
    # Outside the work tree: a HOME inside it would put every fixture
    # "under home", and the own-parent case would trip the home check first.
    home = Path(tempfile.mkdtemp(prefix="bouncer-agenttest-home-"))
    _HOMES.append(home)
    (home / ".config" / "bouncer").mkdir(parents=True)
    (home / ".config" / "bouncer" / "config.yaml").write_text(
        f"orchestrators: [{named}]\n")
    return dict(os.environ, HOME=str(home))


# --- hook mode ----------------------------------------------------------------

def run_hook_mode(scenarios, project, out_dir, log_path, stub) -> list[tuple]:
    results = []
    for sc in scenarios:
        sid, want = sc["id"], sc["decision"]
        env = None
        if sc["id"] == "internal_skip":
            payload = {"tool_name": "ToolSearch",
                       "tool_input": {"query": "select:BNCR_ALLOW_probe"},
                       "cwd": str(project), "session_id": "hooktest"}
            sentinel = "BNCR_ALLOW_probe"
        else:
            cid = uuid.uuid4().hex[:8]
            target = (seed_target(sc["setup"], project, cid)
                      if sc.get("setup") else "")
            if sc.get("setup"):
                env = seed_home(project, cid, sc.get("caller", "orchestrator"))
                if env is None:
                    results.append((sid, want, None,
                                    "needs an agent session to be the "
                                    "orchestrator"))
                    continue
            command = sc["command"].format(OUT=out_dir, ID=cid, TARGET=target)
            sentinel = command.split("/")[-1]
            payload = {"tool_name": "Bash", "tool_input": {"command": command},
                       "cwd": str(project), "session_id": "hooktest"}

        before = len(stub.find(sentinel))
        proc = subprocess.run(
            [sys.executable, "-m", "bouncer", "classify", "--hook", "--format", "json"],
            input=json.dumps(payload), capture_output=True, text=True, cwd=ROOT,
            env=env,
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
        if sc.get("expect"):
            # A refusal the guard owns: the stub is never asked, and the
            # reason is the one the scenario names, not some other.
            said = sc["expect"] in proc.stdout + proc.stderr
            ok = ok and said and not consulted
            detail += f" consulted={consulted} reason={'ok' if said else 'WRONG'}"
        results.append((sid, want, ok, detail))
    return results


# --- agent mode ---------------------------------------------------------------

def run_agent_mode(scenarios, project, out_dir, log_path, harness) -> list[tuple]:
    if harness == "claude":
        from drivers.claude_headless import run_claude_scenario as run_scenario
    elif harness == "codex_cli":
        from drivers.codex_headless import run_codex_scenario as run_scenario
    else:
        raise SystemExit(f"agent driver for {harness!r} not implemented yet")

    results = []
    for sc in scenarios:
        if harness not in sc.get("harnesses", []) or sc["id"] == "internal_skip":
            results.append((sc["id"], sc["decision"], None, "n/a for this slice"))
            continue
        cid = uuid.uuid4().hex[:8]
        command = sc["command"].format(OUT=out_dir, ID=cid)
        flag_name = command.split("/")[-1]
        obs = run_scenario(
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
        for home in _HOMES:
            subprocess.run(["rm", "-rf", str(home)])

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
