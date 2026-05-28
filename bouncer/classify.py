import os
import signal
import sys
import time
from pathlib import Path

from .config import _merged_config, project_has_bouncer, project_log_file
from .log import log_decision
from .activity import _update_activity
from .hook import _emit_hook_response, resolve_fallback
from .providers import call_llm
from ._abort import ABORT_EVENT


def get_classification(
    tool_name: str,
    tool_input: dict,
    cwd: str,
) -> tuple[str, str, str | None, int | None]:
    """
    Pure logic: get decision and reason for a tool call.
    Returns (decision, reason, action_to_take, prompt_chars).

    decision: ALLOW | DENY | UNSURE | ESCALATE | SKIP
    reason:   text explanation
    action_to_take: ALLOW | DENY | ASK | None (if skipped/disabled)
    prompt_chars: combined system+user prompt length, or None if LLM not called
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()

    if not project_has_bouncer(cwd_path):
        return "SKIP", "no project config", None, None

    config = _merged_config(cwd_path)
    if not config.get("enabled", True):
        return "SKIP", "bouncer disabled in config", None, None

    tools = config.get("tools", ["Bash"])
    if tools != "all":
        tools_lower = [t.lower() for t in tools]
        if tool_name.lower() not in tools_lower:
            return "SKIP", f"tool {tool_name!r} not in intercepted list", None, None

    command = tool_input.get("command", "")

    if command.strip() in ("bouncer --agent-help", "bouncer --help", "bouncer -h"):
        return "SKIP", "bouncer help command", None, None

    if command.lstrip().startswith("# ESCALATE:"):
        first_line      = command.split("\n")[0]
        escalate_reason = first_line.replace("# ESCALATE:", "").strip()
        return "ESCALATE", escalate_reason, "ASK", None

    decision, reason, prompt_chars = call_llm(tool_name, tool_input, cwd_path, config)

    if decision is None or decision == "TIMEOUT":
        display_dec = decision or "UNSURE"
        fallback_action = config.get("on_unavailable", "ask")
        final_dec, final_reason = resolve_fallback(
            fallback_action,
            f"LLM unavailable: {reason}"
        )
        return display_dec, final_reason, final_dec, prompt_chars

    if decision in ("ALLOW", "DENY"):
        return decision, reason, decision, prompt_chars

    # UNSURE
    fallback_action = config.get("on_unsure", "ask")
    final_dec, final_reason = resolve_fallback(
        fallback_action,
        f"LLM unsure: {reason}"
    )
    return "UNSURE", final_reason, final_dec, prompt_chars


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

    # Fast early exit for projects with no bouncer config at all. Avoids
    # writing a stranded PENDING entry to the user log for every tool call
    # in unrelated projects. The enabled / tools-filter SKIP checks are
    # handled by get_classification below — single source of truth.
    if not project_has_bouncer(cwd_path):
        sys.exit(0)

    config         = _merged_config(cwd_path)
    proj_log       = project_log_file(cwd_path)
    activity_width = config.get("activity_width", 10)
    rid            = os.getpid()

    # ESCALATE bypasses the LLM, so we log a single entry (no PENDING).
    command = tool_input.get("command", "")
    if command.lstrip().startswith("# ESCALATE:"):
        decision, reason, action, _ = get_classification(tool_name, tool_input, cwd)
        if decision == "SKIP":
            sys.exit(0)
        log_decision(tool_name, tool_input, cwd, "ESCALATE", reason,
                     config, proj_log)
        _update_activity(tool_name, "ESCALATE", session_id, activity_width)
        _emit_hook_response(action, f"agent escalation requested: {reason}", fmt)
        return

    signal.signal(signal.SIGUSR1, lambda sig, frame: ABORT_EVENT.set())

    log_decision(tool_name, tool_input, cwd, "PENDING", "calling LLM",
                 None, proj_log, rid)

    t0 = time.monotonic()
    decision, reason, action, prompt_chars = get_classification(tool_name, tool_input, cwd)
    elapsed = time.monotonic() - t0

    if decision == "SKIP":
        sys.exit(0)

    if ABORT_EVENT.is_set():
        decision, reason, action = "ALLOW", "user aborted — allowing", "ALLOW"

    log_decision(tool_name, tool_input, cwd, decision, reason,
                 config, proj_log, rid,
                 elapsed_s=elapsed, prompt_chars=prompt_chars)
    _update_activity(tool_name, decision, session_id, activity_width)

    if action:
        _emit_hook_response(action, reason, fmt)
    else:
        sys.exit(0)
