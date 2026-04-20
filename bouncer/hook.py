import json
import sys


_DENY_HINT = (
    "To escalate to the user: prefix your command with `# ESCALATE: <reason>` and retry. "
    "Run 'bouncer --agent-help' if you haven't already."
)

_ASK_UNDELIVERED_HINT = (
    "This harness cannot prompt the user directly; relay the reason above and "
    "wait for the user's decision before retrying."
)


def _join_reason(reason: str, hint: str) -> str:
    if not reason:
        return hint
    sep = " " if reason.rstrip().endswith((".", "!", "?")) else ". "
    return f"{reason.rstrip()}{sep}{hint}"


def format_hook_response(decision: str, reason: str, fmt: str = "json") -> tuple[str, str, int]:
    """
    Format a hook response.
    Returns (stdout, stderr, exit_code).
    decision: ALLOW | DENY | ASK
    """
    stdout, stderr, exit_code = "", "", 0

    if fmt == "plain":
        # Plain format is a single tab-separated line, so hints get flattened
        # onto the reason field (no embedded newlines).
        if decision == "DENY":
            stdout = f"deny\t{_join_reason(reason, _DENY_HINT)}\n"
            exit_code = 2
        elif decision == "ASK":
            stdout = f"ask\t{_join_reason(reason, _ASK_UNDELIVERED_HINT)}\n"
            exit_code = 0
        else:
            stdout = "allow\n"
            exit_code = 0
        return stdout, stderr, exit_code

    # JSON (default — Claude Code / Codex hookSpecificOutput protocol)
    if decision == "DENY":
        stderr = f"{reason}\n{_DENY_HINT}\n"
        exit_code = 2
    elif decision == "ASK":
        stdout = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": reason,
            }
        }) + "\n"
        exit_code = 0
    else:  # ALLOW
        stdout = json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": reason,
            }
        }) + "\n"
        exit_code = 0
    
    return stdout, stderr, exit_code


def _emit_hook_response(decision: str, reason: str, fmt: str = "json") -> None:
    """Emit a hook response on stdout/stderr and exit."""
    stdout, stderr, exit_code = format_hook_response(decision, reason, fmt)
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    sys.exit(exit_code)


def resolve_fallback(action: str, reason: str) -> tuple[str, str]:
    """
    Map a fallback action (allow|deny|ask) to a canonical decision
    (ALLOW|DENY|ASK) and final reason. The escalate/agent-help hint is
    appended by format_hook_response, so this returns the bare reason.
    """
    if action == "allow":
        return "ALLOW", reason
    elif action == "deny":
        return "DENY", reason
    else:  # ask (default)
        return "ASK", reason
