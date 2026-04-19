import os
import sys
from pathlib import Path

from .config import _merged_config, project_has_bouncer, project_log_file
from .log import log_decision
from .activity import _update_activity
from .hook import _emit_hook_response, _handle_fallback
from .providers import call_llm


def run_classify(
    tool_name: str,
    tool_input: dict,
    cwd: str,
    session_id: str,
    fmt: str = "json",
) -> None:
    """Core classify logic. Emits a hook response and exits."""
    cwd_path = Path(cwd) if cwd else Path.cwd()

    if not project_has_bouncer(cwd_path):
        sys.exit(0)

    config = _merged_config(cwd_path)

    if not config.get("enabled", True):
        sys.exit(0)

    tools = config.get("tools", ["Bash"])
    if tools != "all":
        tools_lower = [t.lower() for t in tools]
        if tool_name.lower() not in tools_lower:
            sys.exit(0)

    proj_log       = project_log_file(cwd_path)
    command        = tool_input.get("command", "")
    activity_width = config.get("activity_width", 10)

    if command.lstrip().startswith("# OVERRIDE:"):
        first_line      = command.split("\n")[0]
        override_reason = first_line.replace("# OVERRIDE:", "").strip()
        log_decision(tool_name, tool_input, cwd, "OVERRIDE", override_reason,
                     config, proj_log)
        _update_activity(tool_name, "OVERRIDE", session_id, activity_width)
        _emit_hook_response("ASK", f"agent override requested: {override_reason}", fmt)
        return

    rid = os.getpid()
    log_decision(tool_name, tool_input, cwd, "PENDING", "calling LLM",
                 None, proj_log, rid)
    decision, reason = call_llm(tool_name, tool_input, cwd_path, config)
    final_decision = decision or "UNSURE"
    log_decision(tool_name, tool_input, cwd, final_decision, reason,
                 config, proj_log, rid)
    _update_activity(tool_name, final_decision, session_id, activity_width)

    if decision is None:
        action = config.get("on_unavailable", "ask")
        _handle_fallback(
            action,
            f"LLM unavailable: {reason} — set on_unavailable in .bouncer/config.yaml",
            fmt,
        )
        return

    if decision == "ALLOW":
        _emit_hook_response("ALLOW", reason, fmt)

    if decision == "DENY":
        _emit_hook_response("DENY", reason, fmt)

    # UNSURE
    action = config.get("on_unsure", "ask")
    _handle_fallback(
        action,
        f"LLM unsure: {reason} — add context to .bouncer/policy.md",
        fmt,
    )
