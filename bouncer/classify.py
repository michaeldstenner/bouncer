import os
import signal
import shlex
import sys
import time
from pathlib import Path

from .config import _merged_config, project_has_bouncer, project_log_file
from .log import log_decision
from .activity import _update_activity
from .escalation_cache import record_attempt, was_attempted, strip_escalate_prefix
from .hook import _emit_hook_response, resolve_fallback
from .notify import notify_decision
from .providers import call_llm
from ._abort import ABORT_EVENT


_BOUNCER_DIAGNOSTIC_COMMANDS = {
    "activity",
    "check",
    "log",
    "status",
}


def _is_bouncer_diagnostic_command(command: str) -> bool:
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        return False

    if not parts:
        return False

    executable = Path(parts[0]).name
    if executable != "bouncer":
        return False

    if len(parts) == 1:
        return False

    if parts[1] in ("--agent-help", "--help", "-h"):
        return True

    index = 1
    if parts[index] in ("-g", "--global"):
        index += 1

    return index < len(parts) and parts[index] in _BOUNCER_DIAGNOSTIC_COMMANDS


def _skip_reason(tool_name: str, tool_input: dict, config: dict) -> str | None:
    """
    Why bouncer should skip this call (no LLM, no decision), or None to classify.

    Single source of truth for the config-derived skip conditions, shared by
    get_classification (pure path) and run_classify (so it can bail before
    logging a PENDING entry). The cwd-based project_has_bouncer gate is checked
    by callers before the config is loaded.
    """
    if not config.get("enabled", True):
        return "bouncer disabled in config"
    tools = config.get("tools", "all")
    if tools != "all" and tool_name.lower() not in [t.lower() for t in tools]:
        return f"tool {tool_name!r} not in intercepted list"
    if _is_bouncer_diagnostic_command(tool_input.get("command", "")):
        return "bouncer diagnostic command"
    return None


def get_classification(
    tool_name: str,
    tool_input: dict,
    cwd: str,
) -> tuple[str, str, str | None, int | None, list[dict] | None]:
    """
    Pure logic: get decision and reason for a tool call.
    Returns (decision, reason, action_to_take, prompt_chars, queue_snapshot).

    decision: ALLOW | DENY | UNSURE | ESCALATE | SKIP
    reason:   text explanation
    action_to_take: ALLOW | DENY | ASK | None (if skipped/disabled)
    prompt_chars: combined system+user prompt length, or None if LLM not called
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()

    if not project_has_bouncer(cwd_path):
        return "SKIP", "no project config", None, None, None

    config = _merged_config(cwd_path)
    skip = _skip_reason(tool_name, tool_input, config)
    if skip:
        return "SKIP", skip, None, None, None

    command = tool_input.get("command", "")

    if command.lstrip().startswith("# ESCALATE:"):
        first_line      = command.split("\n")[0]
        escalate_reason = first_line.replace("# ESCALATE:", "").strip()
        return "ESCALATE", escalate_reason, "ASK", None, None

    try:
        decision, reason, prompt_chars, snap = call_llm(tool_name, tool_input, cwd_path, config)
    except Exception as exc:
        decision, reason, prompt_chars, snap = None, str(exc), None, None

    if decision is None or decision in ("TIMEOUT", "LLM_ERROR"):
        display_dec = decision or "UNSURE"
        fallback_action = config.get("on_unavailable", "ask")
        final_dec, final_reason = resolve_fallback(
            fallback_action,
            f"LLM unavailable: {reason}"
        )
        return display_dec, final_reason, final_dec, prompt_chars, snap

    if decision in ("ALLOW", "DENY"):
        return decision, reason, decision, prompt_chars, None

    # UNSURE
    fallback_action = config.get("on_unsure", "ask")
    final_dec, final_reason = resolve_fallback(
        fallback_action,
        f"LLM unsure: {reason}"
    )
    return "UNSURE", final_reason, final_dec, prompt_chars, None


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
    # in unrelated projects.
    if not project_has_bouncer(cwd_path):
        sys.exit(0)

    config         = _merged_config(cwd_path)
    proj_log       = project_log_file(cwd_path)
    activity_width = config.get("activity_width", 10)
    rid            = os.getpid()

    # Bail on the config-derived SKIP conditions (disabled, tool not in the
    # intercept list, bouncer's own diagnostic commands) BEFORE logging a
    # PENDING entry — otherwise a non-intercepted call strands a PENDING with
    # no resolving entry. Shares _skip_reason with get_classification.
    if _skip_reason(tool_name, tool_input, config):
        sys.exit(0)

    # ESCALATE bypasses the LLM, so we log a single entry (no PENDING).
    command = tool_input.get("command", "")
    if command.lstrip().startswith("# ESCALATE:"):
        decision, reason, action, _, _snap = get_classification(tool_name, tool_input, cwd)
        if decision == "SKIP":
            sys.exit(0)

        # Gate: only honor the escalation if the underlying command was
        # actually attempted (bare, without the prefix) recently this session.
        # Curbs agents that pre-emptively escalate commands they never tried.
        if config.get("escalation_requires_attempt", True):
            underlying = strip_escalate_prefix(command)
            ttl = config.get("escalation_attempt_ttl", 300)
            if not was_attempted(underlying, session_id, ttl):
                reject = (
                    "This command hasn't been attempted yet. Run it directly "
                    "(without the `# ESCALATE:` prefix) first; escalate only "
                    "if that attempt is denied."
                )
                log_decision(tool_name, tool_input, cwd, "DENY", reject,
                             config, proj_log)
                _update_activity(tool_name, "DENY", session_id, activity_width)
                notify_decision(
                    cfg=config,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    cwd=cwd,
                    session_id=session_id,
                    decision="DENY",
                    action="DENY",
                    reason=reject,
                    request_id=None,
                    proj_log=proj_log,
                )
                _emit_hook_response("DENY", reject, fmt)
                return

        log_decision(tool_name, tool_input, cwd, "ESCALATE", reason,
                     config, proj_log)
        _update_activity(tool_name, "ESCALATE", session_id, activity_width)
        notify_decision(
            cfg=config,
            tool_name=tool_name,
            tool_input=tool_input,
            cwd=cwd,
            session_id=session_id,
            decision="ESCALATE",
            action=action,
            reason=reason,
            request_id=None,
            proj_log=proj_log,
        )
        _emit_hook_response(action, f"agent escalation requested: {reason}", fmt)
        return

    signal.signal(signal.SIGUSR1, lambda sig, frame: ABORT_EVENT.set())

    log_decision(tool_name, tool_input, cwd, "PENDING", "calling LLM",
                 None, proj_log, rid)

    t0 = time.monotonic()
    decision, reason, action, prompt_chars, snap = get_classification(tool_name, tool_input, cwd)
    elapsed = time.monotonic() - t0

    if decision == "SKIP":
        sys.exit(0)

    if ABORT_EVENT.is_set():
        decision, reason, action = "ALLOW", "user aborted — allowing", "ALLOW"

    log_decision(tool_name, tool_input, cwd, decision, reason,
                 config, proj_log, rid,
                 elapsed_s=elapsed, prompt_chars=prompt_chars,
                 queue_snapshot=snap)
    _update_activity(tool_name, decision, session_id, activity_width)
    # Record this bare attempt so a later `# ESCALATE:` of the same command is
    # recognized as a genuine retry rather than a pre-emptive escalation.
    if config.get("escalation_requires_attempt", True):
        record_attempt(command, session_id)
    notify_decision(
        cfg=config,
        tool_name=tool_name,
        tool_input=tool_input,
        cwd=cwd,
        session_id=session_id,
        decision=decision,
        action=action,
        reason=reason,
        request_id=rid,
        elapsed_s=elapsed,
        prompt_chars=prompt_chars,
        proj_log=proj_log,
    )

    if action:
        _emit_hook_response(action, reason, fmt)
    else:
        sys.exit(0)
