import os
import sys
from pathlib import Path

from .config import _merged_config, project_has_bouncer, project_log_file
from .log import log_decision
from .activity import _update_activity
from .hook import _emit_hook_response, resolve_fallback
from .providers import call_llm


def get_classification(
    tool_name: str,
    tool_input: dict,
    cwd: str,
) -> tuple[str, str, str | None]:
    """
    Pure logic: get decision and reason for a tool call.
    Returns (decision, reason, action_to_take).
    
    decision: ALLOW | DENY | UNSURE | OVERRIDE | SKIP
    reason:   text explanation
    action_to_take: ALLOW | DENY | ASK | None (if skipped/disabled)
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()

    if not project_has_bouncer(cwd_path):
        return "SKIP", "no project config", None

    config = _merged_config(cwd_path)
    if not config.get("enabled", True):
        return "SKIP", "bouncer disabled in config", None

    tools = config.get("tools", ["Bash"])
    if tools != "all":
        tools_lower = [t.lower() for t in tools]
        if tool_name.lower() not in tools_lower:
            return "SKIP", f"tool {tool_name!r} not in intercepted list", None

    command = tool_input.get("command", "")
    if command.lstrip().startswith("# OVERRIDE:"):
        first_line      = command.split("\n")[0]
        override_reason = first_line.replace("# OVERRIDE:", "").strip()
        return "OVERRIDE", override_reason, "ASK"

    decision, reason = call_llm(tool_name, tool_input, cwd_path, config)
    
    if decision is None:
        fallback_action = config.get("on_unavailable", "ask")
        final_dec, final_reason = resolve_fallback(
            fallback_action,
            f"LLM unavailable: {reason}"
        )
        return "UNSURE", final_reason, final_dec

    if decision in ("ALLOW", "DENY"):
        return decision, reason, decision

    # UNSURE
    fallback_action = config.get("on_unsure", "ask")
    final_dec, final_reason = resolve_fallback(
        fallback_action,
        f"LLM unsure: {reason}"
    )
    return "UNSURE", final_reason, final_dec


def run_classify(
    tool_name: str,
    tool_input: dict,
    cwd: str,
    session_id: str,
    fmt: str = "json",
) -> None:
    """
    Legacy/CLI entry point: classifies, logs, updates activity, 
    and EXITS the process.
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()
    
    # 1. Quick check for project active
    if not project_has_bouncer(cwd_path):
        sys.exit(0)
    config = _merged_config(cwd_path)
    if not config.get("enabled", True):
        sys.exit(0)
    
    # 2. Logic & Logging
    tools = config.get("tools", ["Bash"])
    if tools != "all":
        tools_lower = [t.lower() for t in tools]
        if tool_name.lower() not in tools_lower:
            sys.exit(0)

    proj_log       = project_log_file(cwd_path)
    activity_width = config.get("activity_width", 10)
    rid            = os.getpid()

    # Special handling for OVERRIDE to match old behavior (logging before/after)
    command = tool_input.get("command", "")
    if command.lstrip().startswith("# OVERRIDE:"):
        decision, reason, action = get_classification(tool_name, tool_input, cwd)
        log_decision(tool_name, tool_input, cwd, "OVERRIDE", reason,
                     config, proj_log)
        _update_activity(tool_name, "OVERRIDE", session_id, activity_width)
        _emit_hook_response(action, f"agent override requested: {reason}", fmt)
        return

    # Normal classification path
    log_decision(tool_name, tool_input, cwd, "PENDING", "calling LLM",
                 None, proj_log, rid)
    
    decision, reason, action = get_classification(tool_name, tool_input, cwd)
    
    # Map 'SKIP' or other internal states to what logging expects
    log_dec = decision
    if decision == "SKIP": 
        sys.exit(0) # Should have been caught by early checks, but for safety

    log_decision(tool_name, tool_input, cwd, log_dec, reason,
                 config, proj_log, rid)
    _update_activity(tool_name, log_dec, session_id, activity_width)

    if action:
        _emit_hook_response(action, reason, fmt)
    else:
        sys.exit(0)
