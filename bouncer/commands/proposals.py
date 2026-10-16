"""`bouncer propose` / `apply` / `revert` — approved policy edits, applied by
the orchestrator. All checks live in bouncer.proposals; this only reads the
arguments and prints."""
import sys
from pathlib import Path

from ..colors import BOLD, DIM, GREEN, RED, RESET
from .. import proposals


def _refuse(exc: proposals.Refused) -> None:
    print(f"{RED}{exc}{RESET}", file=sys.stderr)
    sys.exit(1)


def cmd_propose(args):
    try:
        diff = (sys.stdin.read() if args.diff == "-"
                else Path(args.diff).expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        _refuse(proposals.Refused(f"Refused: cannot read the diff: {exc}"))
    try:
        record = proposals.propose(args.project, diff)
    except proposals.Refused as exc:
        _refuse(exc)
    print(f"{BOLD}proposal {record['code']}{RESET}  {record['target']}")
    print(f"{DIM}Show the user this diff and this code. Apply only on their "
          f"explicit yes to it: bouncer apply {record['code']}{RESET}")
    print()
    print(record["diff"], end="" if record["diff"].endswith("\n") else "\n")


def cmd_apply(args):
    try:
        record = proposals.apply(args.code)
    except proposals.Refused as exc:
        _refuse(exc)
    print(f"{GREEN}applied {record['code']}{RESET}  {record['target']}  "
          f"{DIM}(undo: bouncer revert {record['code']}){RESET}")


def cmd_revert(args):
    try:
        record = proposals.revert(args.code)
    except proposals.Refused as exc:
        _refuse(exc)
    print(f"{GREEN}reverted {record['code']}{RESET}  {record['target']}")
