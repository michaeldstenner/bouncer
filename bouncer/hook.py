import json
import sys


_DENY_HINT_WITH_ASK = (
    "To escalate to the user: prefix your command with `# ESCALATE: <reason>` and retry. "
    "Run 'bouncer --agent-help' if you haven't already."
)

_DENY_HINT_NO_ASK = (
    "This harness does not have ASK available. Find another way or suggest a policy change. "
    "Run 'bouncer --agent-help' if you haven't already."
)


def _join_reason(reason: str, hint: str) -> str:
    if not reason:
        return hint
    sep = " " if reason.rstrip().endswith((".", "!", "?")) else ". "
    return f"{reason.rstrip()}{sep}{hint}"


def _ask_available(fmt: str) -> bool:
    return fmt == "json"


def format_hook_response(decision: str, reason: str, fmt: str = "json") -> tuple[str, str, int]:
    """
    Format a hook response.
    Returns (stdout, stderr, exit_code).
    decision: ALLOW | DENY | ASK
    """
    stdout, stderr, exit_code = "", "", 0
    ask_available = _ask_available(fmt)
    deny_hint = _DENY_HINT_WITH_ASK if ask_available else _DENY_HINT_NO_ASK

    if fmt in ("plain", "codex-pretool"):
        # Plain format is a single tab-separated line, so hints get flattened
        # onto the reason field (no embedded newlines). Codex PreToolUse does
        # not support Claude's permissionDecision protocol; allow is silent
        # exit 0, deny is exit 2, and ask abstains so Codex can continue.
        if decision == "DENY":
            if fmt == "codex-pretool":
                stderr = f"{_join_reason(reason, deny_hint)}\n"
            else:
                stdout = f"deny\t{_join_reason(reason, deny_hint)}\n"
            exit_code = 2
        elif decision == "ASK":
            if fmt == "plain":
                stdout = f"deny\t{_join_reason(reason, deny_hint)}\n"
                exit_code = 2
            else:
                exit_code = 0
        else:
            if fmt == "plain":
                stdout = "allow\n"
            exit_code = 0
        return stdout, stderr, exit_code

    if fmt == "codex-permission":
        if decision == "ALLOW":
            stdout = json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                }
            }) + "\n"
        elif decision == "DENY":
            stdout = json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "deny", "message": reason},
                }
            }) + "\n"
        else:
            # No decision lets Codex continue to its normal approval prompt.
            stdout = ""
        return stdout, stderr, exit_code

    # JSON (default — ASK-capable hookSpecificOutput protocol)
    if decision == "DENY":
        stderr = f"{reason}\n{deny_hint}\n"
        exit_code = 2
    elif decision == "ASK":
        if ask_available:
            stdout = json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": reason,
                }
            }) + "\n"
            exit_code = 0
        else:
            stderr = f"{reason}\n{deny_hint}\n"
            exit_code = 2
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
    (ALLOW|DENY|ASK) and final reason. The harness-specific hint is
    appended by format_hook_response, so this returns the bare reason.
    """
    if action == "allow":
        return "ALLOW", reason
    elif action == "deny":
        return "DENY", reason
    else:  # ask (default)
        return "ASK", reason
