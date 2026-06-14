import sys
from pathlib import Path

from ..colors import RESET, BOLD, YELLOW, DIM
from ..config import _find_bouncer_dir
from .. import escalation_grant as eg


def cmd_escalate(args):
    """Arm a one-shot escalation for this project's most recent denial.

    The agent runs this after a tool call is DENIED, then re-issues that exact
    call to send it to the user. In a hooked harness the PreToolUse hook has
    already armed the grant (with the session id) by the time this prints, so
    here we just report it; in a hookless context we arm it project-scoped.
    """
    reason = " ".join(getattr(args, "reason", None) or [])
    project_dir = _find_bouncer_dir(Path.cwd())
    if project_dir is None:
        print(f"{YELLOW}bouncer escalate:{RESET} no .bouncer project here.",
              file=sys.stderr)
        sys.exit(1)

    data = eg._load(project_dir)
    grant = data.get("grant")
    if grant is None:
        # No hook armed it (e.g. a harness without a PreToolUse hook). Arm it
        # here so the flow still works.
        target = eg.arm_escalation(project_dir, reason)
        grant = {"tool": target.get("tool")} if target else None

    if grant is None:
        print(f"{YELLOW}Nothing to escalate{RESET} — no recent denial recorded "
              f"for this project.\n{DIM}Submit the call first; escalate only "
              f"after it is denied.{RESET}")
        return

    tool = grant.get("tool") or "call"
    print(f"{BOLD}Escalation armed{RESET} for your last denied {BOLD}{tool}{RESET} "
          f"call.\nRe-issue that exact call now — bouncer will send it to the "
          f"user instead of denying it.")
