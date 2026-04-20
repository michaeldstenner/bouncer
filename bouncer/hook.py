import json
import sys


def format_hook_response(decision: str, reason: str, fmt: str = "json") -> tuple[str, str, int]:
    """
    Format a hook response.
    Returns (stdout, stderr, exit_code).
    decision: ALLOW | DENY | ASK
    """
    stdout, stderr, exit_code = "", "", 0

    if fmt == "plain":
        sep = "\t" if reason else ""
        if decision == "DENY":
            stdout = f"deny{sep}{reason}\n"
            exit_code = 2
        elif decision == "ASK":
            stdout = f"ask{sep}{reason}\n"
            exit_code = 0
        else:
            stdout = "allow\n"
            exit_code = 0
        return stdout, stderr, exit_code

    # JSON (default — Claude Code / Codex hookSpecificOutput protocol)
    if decision == "DENY":
        stderr = (
            f"{reason}\n"
            "To override: prefix your command with `# OVERRIDE: reason` and retry.\n"
            "Run 'bouncer --agent-help' if you haven't already.\n"
        )
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
    Map a fallback action (allow|deny|ask|deny_with_message) to a 
    canonical decision (ALLOW|DENY|ASK) and final reason.
    """
    if action == "allow":
        return "ALLOW", reason
    elif action in ("deny", "deny_with_message"):
        if action == "deny_with_message":
            reason = (
                f"{reason}\n"
                "To override: prefix your command with `# OVERRIDE: reason` and retry."
            )
        return "DENY", reason
    else:  # ask (default)
        return "ASK", reason


def _handle_fallback(action: str, reason: str, fmt: str = "json") -> None:
    """Dispatch on_unsure / on_unavailable action. Always exits."""
    decision, final_reason = resolve_fallback(action, reason)
    _emit_hook_response(decision, final_reason, fmt)
