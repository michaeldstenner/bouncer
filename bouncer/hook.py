import json
import sys


_DENY_HINT_WITH_ASK = (
    "Source: bouncer policy denial, not a direct user denial. "
    "Escalation support is available; run 'bouncer --agent-help' for protocol details."
)

_DENY_HINT_NO_ASK = (
    "Source: bouncer policy denial, not a direct user denial. "
    "This harness does not have ASK available; run 'bouncer --agent-help' for protocol details."
)


def _join_reason(reason: str, hint: str) -> str:
    if not reason:
        return hint
    sep = " " if reason.rstrip().endswith((".", "!", "?")) else ". "
    return f"{reason.rstrip()}{sep}{hint}"


def _codex_system_message(decision: str, reason: str) -> str:
    label = decision if decision in ("ALLOW", "DENY", "ABSTAIN") else "ASK"
    text = " ".join((reason or "").split())
    if len(text) > 160:
        text = text[:157].rstrip() + "..."
    return f"bouncer: {label}" + (f" - {text}" if text else "")


def _ask_available(fmt: str) -> bool:
    return fmt == "json"


def format_hook_response(decision: str, reason: str, fmt: str = "json") -> tuple[str, str, int]:
    """
    Format a hook response.
    Returns (stdout, stderr, exit_code).
    decision: ALLOW | DENY | ASK | ABSTAIN

    ABSTAIN means "no opinion — defer to the harness's own permission flow."
    On Claude Code (json) it emits nothing (no permissionDecision). On Codex it
    converges with ASK (no decision → Codex continues to its normal prompt). On
    the bash shim (plain) there is nothing to defer to, so it allows.
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
                "systemMessage": _codex_system_message(decision, reason),
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                }
            }) + "\n"
        elif decision == "DENY":
            stdout = json.dumps({
                "systemMessage": _codex_system_message(decision, reason),
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "deny", "message": reason},
                }
            }) + "\n"
        else:
            # No hookSpecificOutput decision lets Codex continue to its normal
            # approval prompt. systemMessage is experimental GUI feedback.
            stdout = json.dumps({
                "systemMessage": _codex_system_message(decision, reason),
            }) + "\n"
        return stdout, stderr, exit_code

    # JSON (default — ASK-capable hookSpecificOutput protocol)
    if decision == "ABSTAIN":
        # Emit no permissionDecision at all: the hook expresses no opinion, so
        # the harness falls back to its own permission flow (auto-mode
        # classifier, allow-rules, or a normal user prompt) exactly as if
        # bouncer had not run for this call. Distinct from ALLOW, which would
        # override that flow.
        exit_code = 0
    elif decision == "DENY":
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
    Map a fallback action (allow|deny|ask|abstain) to a canonical decision
    (ALLOW|DENY|ASK|ABSTAIN) and final reason. The harness-specific hint is
    appended by format_hook_response, so this returns the bare reason.
    """
    if action == "allow":
        return "ALLOW", reason
    elif action == "deny":
        return "DENY", reason
    elif action == "abstain":
        return "ABSTAIN", reason
    else:  # ask (default)
        return "ASK", reason
