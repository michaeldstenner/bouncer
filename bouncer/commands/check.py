import sys
from pathlib import Path

from ..classify import _fast_path_allow_reason
from ..colors import RESET, BOLD, DIM, YELLOW, DECISION_COLORS, WHITE
from ..config import (_build_policy_context, _merged_config,
                      project_has_bouncer, tool_intercepted)
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

    if not tool_intercepted("Bash", config):
        print(f"{DIM}Bash not intercepted — would pass through.{RESET}")
        return

    ctx   = _build_policy_context(cwd, config)
    first = ctx.splitlines()[0][:72] if ctx else "(none)"
    print(f"Command:        {BOLD}{command}{RESET}")
    print(f"Policy context: {DIM}{first}{'…' if len(ctx) > 72 else ''}{RESET}")

    if getattr(args, "llm", False):
        decision, reason, _prompt_chars, _queue_snapshot = call_llm(
            "Bash", {"command": command}, cwd, config
        )[:4]
        if decision is None or decision in ("TIMEOUT", "LLM_ERROR"):
            action = config.get("on_unavailable", "ask")
            label  = decision or "UNAVAILABLE"
            print(f"  {YELLOW}{label}{RESET} — {reason}  (on_unavailable → {action})")
        else:
            color = DECISION_COLORS.get(decision, WHITE)
            print(f"  {color}{decision}{RESET} — {reason}")
    else:
        reason = _fast_path_allow_reason(
            "Bash", {"command": command}, cwd, config)
        if reason:
            print(f"  {DECISION_COLORS['ALLOW']}ALLOW{RESET} — {reason}")
        else:
            print(f"  {DIM}(would call LLM; use --llm to test live){RESET}")
