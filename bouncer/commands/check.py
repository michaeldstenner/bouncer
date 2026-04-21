import sys
from pathlib import Path

from ..colors import RESET, BOLD, DIM, YELLOW, DECISION_COLORS, WHITE
from ..config import _merged_config, _build_policy_context, project_has_bouncer
from ..providers import call_llm


def cmd_check(args):
    command = args.cmd
    cwd     = Path.cwd()

    if not project_has_bouncer(cwd):
        print(f"{YELLOW}No project config.{RESET} Bouncer inactive — harness default behavior.")
        return

    config = _merged_config(cwd)

    if not config.get("enabled", True):
        print(f"{DIM}Bouncer disabled (enabled: false).{RESET}")
        return

    tools = config.get("tools", ["Bash"])
    if tools != "all" and "bash" not in [t.lower() for t in tools]:
        print(f"{DIM}Bash not in intercepted tools {tools} — would pass through.{RESET}")
        return

    ctx   = _build_policy_context(cwd, config)
    first = ctx.splitlines()[0][:72] if ctx else "(none)"
    print(f"Command:        {BOLD}{command}{RESET}")
    print(f"Policy context: {DIM}{first}{'…' if len(ctx) > 72 else ''}{RESET}")

    if getattr(args, "llm", False):
        decision, reason = call_llm("Bash", {"command": command}, cwd, config)
        if decision is None:
            action = config.get("on_unavailable", "ask")
            print(f"  {YELLOW}UNAVAILABLE{RESET} — {reason}  (on_unavailable → {action})")
        else:
            color = DECISION_COLORS.get(decision, WHITE)
            print(f"  {color}{decision}{RESET} — {reason}")
    else:
        print(f"  {DIM}(would call LLM; use --llm to test live){RESET}")
