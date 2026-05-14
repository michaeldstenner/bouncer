#!/usr/bin/env python3
"""Bouncer smoke tests using disposable fixture projects."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "bouncer-test" / "work"
EXTERNAL_WORK = Path(tempfile.gettempdir()) / "bouncer-test"


@dataclass
class Case:
    name: str
    argv: list[str]
    cwd: Path
    stdin: str = ""
    want_code: int = 0
    want_stdout: tuple[str, ...] = ()
    want_stderr: tuple[str, ...] = ()
    timeout: int = 10


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def reset_fixtures() -> dict[str, Path]:
    if WORK.exists():
        shutil.rmtree(WORK)
    if EXTERNAL_WORK.exists():
        shutil.rmtree(EXTERNAL_WORK)

    inactive = EXTERNAL_WORK / "inactive"
    active = WORK / "active"
    live = WORK / "live"
    child = active / "subdir"
    live_child = live / "subdir"
    inactive.mkdir(parents=True)
    child.mkdir(parents=True)
    live_child.mkdir(parents=True)

    _write(
        active / ".bouncer" / "config.yaml",
        """\
enabled: true
tools:
  - Bash
policy_mode: replace
llm:
  provider: openai_compatible
  model: unreachable-test-model
  url: http://127.0.0.1:9
  timeout: 1
on_unsure: ask
on_unavailable: ask
log:
  verbosity: all
  max_entries: 1000
""",
    )
    _write(
        active / ".bouncer" / "policy.md",
        """\
# Bouncer Smoke Test Policy

This disposable fixture allows harmless read-only shell inspection commands.
Anything destructive should be denied or escalated to the user.
""",
    )

    _write(
        live / ".bouncer" / "config.yaml",
        """\
enabled: true
tools:
  - Bash
policy_mode: replace
# Inherits provider/model/url/api key from the user-level Bouncer config.
# Add an llm: block here to test a specific endpoint without changing user config.
#llm:
#  provider: openai_compatible
#  model: example-model
#  url: https://example.test
#  timeout: 60
#  extra_params:
#    max_tokens: 4096
on_unsure: ask
on_unavailable: ask
log:
  verbosity: all
  max_entries: 1000
  llm_debug: true
""",
    )
    _write(
        live / ".bouncer" / "policy.md",
        """\
# Bouncer Live Smoke Test Policy

This disposable fixture allows harmless read-only shell inspection commands.
It is used to verify a real classifier round trip through the inherited LLM
configuration.
""",
    )

    return {
        "inactive": inactive,
        "active": active,
        "child": child,
        "live": live,
        "live_child": live_child,
    }


def run_case(case: Case) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "bouncer", *case.argv],
            input=case.stdin,
            cwd=case.cwd,
            capture_output=True,
            text=True,
            timeout=case.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return False, f"timed out after {case.timeout}s\n--- stdout ---\n{exc.stdout or ''}\n--- stderr ---\n{exc.stderr or ''}"
    failures: list[str] = []
    if proc.returncode != case.want_code:
        failures.append(f"exit {proc.returncode}, wanted {case.want_code}")
    for needle in case.want_stdout:
        if needle not in proc.stdout:
            failures.append(f"stdout missing {needle!r}")
    for needle in case.want_stderr:
        if needle not in proc.stderr:
            failures.append(f"stderr missing {needle!r}")

    if not failures:
        return True, ""

    detail = "\n".join(
        [
            *failures,
            "--- stdout ---",
            proc.stdout.rstrip(),
            "--- stderr ---",
            proc.stderr.rstrip(),
        ]
    )
    return False, detail


def build_cases(paths: dict[str, Path], *, live: bool) -> list[Case]:
    escalation_payload = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "# ESCALATE: verifying ask path\nrm -rf build"
        },
        "cwd": str(paths["child"]),
        "session_id": "bouncer-test",
    }
    read_payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": "README.md"},
        "cwd": str(paths["child"]),
        "session_id": "bouncer-test",
    }
    live_payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "pwd"},
        "cwd": str(paths["live_child"]),
        "session_id": "bouncer-test-live",
    }

    cases = [
        Case(
            name="inactive project check passes through",
            argv=["check", "pwd"],
            cwd=paths["inactive"],
            want_stdout=("No project config",),
        ),
        Case(
            name="active project dry-run does not call llm",
            argv=["check", "pwd"],
            cwd=paths["child"],
            want_stdout=("Command:", "Policy context:", "would call LLM"),
        ),
        Case(
            name="tool filter skip emits no hook response",
            argv=["classify", "--hook"],
            cwd=paths["child"],
            stdin=json.dumps(read_payload),
        ),
        Case(
            name="escalation maps to json ask",
            argv=["classify", "--hook"],
            cwd=paths["child"],
            stdin=json.dumps(escalation_payload),
            want_stdout=('"permissionDecision": "ask"', "agent escalation requested"),
        ),
        Case(
            name="escalation maps to plain deny without ask",
            argv=["classify", "--hook", "--format", "plain"],
            cwd=paths["child"],
            stdin=json.dumps(escalation_payload),
            want_code=2,
            want_stdout=("deny\t", "This harness does not have ASK available"),
        ),
        Case(
            name="escalation maps to codex permission abstain",
            argv=["classify", "--hook", "--format", "codex-permission"],
            cwd=paths["child"],
            stdin=json.dumps(escalation_payload),
        ),
        Case(
            name="unavailable llm falls back to ask",
            argv=["check", "pwd", "--llm"],
            cwd=paths["child"],
            want_stdout=("UNAVAILABLE", "on_unavailable"),
        ),
    ]
    if live:
        cases.append(
            Case(
                name="live llm allows harmless command",
                argv=["check", "pwd", "--llm"],
                cwd=paths["live_child"],
                want_stdout=("ALLOW",),
                timeout=90,
            )
        )
        cases.append(
            Case(
                name="live hook allows harmless command",
                argv=["classify", "--hook"],
                cwd=paths["live_child"],
                stdin=json.dumps(live_payload),
                want_stdout=('"permissionDecision": "allow"',),
                timeout=90,
            )
        )
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Bouncer smoke tests against disposable fixtures."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="also call the configured live LLM endpoint",
    )
    args = parser.parse_args()

    paths = reset_fixtures()
    cases = build_cases(paths, live=args.live)

    failed = 0
    for case in cases:
        ok, detail = run_case(case)
        if ok:
            print(f"ok - {case.name}")
        else:
            failed += 1
            print(f"not ok - {case.name}")
            print(detail)

    if failed:
        print(f"\n{failed} failed; fixtures left in {WORK}")
        return 1

    mode = "offline + live" if args.live else "offline"
    print(f"\n{len(cases)} passed ({mode}); fixtures in {WORK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
