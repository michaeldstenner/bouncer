import json
import sys


def _emit_hook_response(decision: str, reason: str, fmt: str = "json") -> None:
    """Emit a hook response on stdout and exit. decision: ALLOW | DENY | ASK"""
    if fmt == "plain":
        sep = "\t" if reason else ""
        if decision == "DENY":
            print(f"deny{sep}{reason}")
            sys.exit(2)
        if decision == "ASK":
            print(f"ask{sep}{reason}")
            sys.exit(0)
        print("allow")
        sys.exit(0)
    # JSON (default — Claude Code / Codex hookSpecificOutput protocol)
    if decision == "DENY":
        print(
            f"{reason}\n"
            "To override: prefix your command with `# OVERRIDE: reason` and retry.\n"
            "For persistent context: edit .bouncer/policy.md",
            file=sys.stderr,
        )
        sys.exit(2)
    if decision == "ASK":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": reason,
            }
        }))
        sys.exit(0)
    # ALLOW
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def _handle_fallback(action: str, reason: str, fmt: str = "json") -> None:
    """Dispatch on_unsure / on_unavailable action. Always exits."""
    if action == "allow":
        sys.exit(0)
    elif action in ("deny", "deny_with_message"):
        if action == "deny_with_message":
            reason = (
                f"{reason}\n"
                "To override: prefix your command with `# OVERRIDE: reason` and retry."
            )
        _emit_hook_response("DENY", reason, fmt)
    else:  # ask (default)
        _emit_hook_response("ASK", reason, fmt)
