import os
import signal
import shlex
import sys
import time
from pathlib import Path

from .config import _merged_config, project_has_bouncer, project_log_file, _find_bouncer_dir, tool_intercepted
from .log import log_decision
from .escalation_cache import record_attempt, was_attempted, strip_escalate_prefix, parse_escalation
from .escalation_grant import record_denial, arm_escalation, take_grant
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

_BOUNCER_SELF_MANAGEMENT_COMMANDS = {
    "config",
    "init",
    "policy",
    "review",
}


def _is_bouncer_diagnostic_command(command: str) -> bool:
    segments = _shell_segments(command)
    if len(segments) != 1:
        return False
    parts = segments[0]
    if len(parts) > 1 and Path(parts[0]).name == "bouncer" \
            and parts[1] in ("--agent-help", "--help", "-h"):
        return True
    return _segment_bouncer_subcommand(parts) in _BOUNCER_DIAGNOSTIC_COMMANDS


def _shell_segments(command: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        parts = list(lexer)
    except ValueError:
        return []
    segments, current = [], []
    for part in parts:
        if part and all(char in ";&|()" for char in part):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(part)
    if current:
        segments.append(current)
    return segments


def _segment_bouncer_subcommand(parts: list[str]) -> str | None:
    index = 0
    while index < len(parts) and "=" in parts[index] and not parts[index].startswith("="):
        index += 1
    while index < len(parts) and Path(parts[index]).name in (
        "command", "exec", "env", "nice", "nohup", "sudo",
    ):
        wrapper = Path(parts[index]).name
        index += 1
        while index < len(parts):
            part = parts[index]
            if wrapper == "env" and "=" in part and not part.startswith("="):
                index += 1
                continue
            if not part.startswith("-"):
                break
            option = part.split("=", 1)[0]
            index += 1
            takes_value = (
                (wrapper == "env" and option in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"))
                or (wrapper == "sudo" and option in ("-u", "--user", "-g", "--group", "-h", "--host",
                                                         "-p", "--prompt", "-C", "--close-from",
                                                         "-R", "--chroot", "-D", "--chdir"))
                or (wrapper == "nice" and option in ("-n", "--adjustment"))
            )
            if takes_value and "=" not in part and index < len(parts):
                index += 1
    if index >= len(parts):
        return None
    executable = Path(parts[index]).name
    index += 1
    if executable.startswith("python"):
        try:
            module_index = parts.index("-m", index)
        except ValueError:
            return None
        if module_index + 1 >= len(parts) or parts[module_index + 1] != "bouncer":
            return None
        index = module_index + 2
    elif executable != "bouncer":
        if executable in ("bash", "sh", "zsh") and index < len(parts):
            try:
                command_index = parts.index("-c", index)
            except ValueError:
                return None
            nested = _bouncer_subcommand(parts[command_index + 1]) \
                if command_index + 1 < len(parts) else None
            return nested
        return None
    if index < len(parts) and parts[index] in ("-g", "--global"):
        index += 1
    return parts[index] if index < len(parts) else None


def _bouncer_subcommand(command: str) -> str | None:
    for segment in _shell_segments(command):
        subcommand = _segment_bouncer_subcommand(segment)
        if subcommand:
            return subcommand
    return None


def _is_bouncer_self_management_command(command: str) -> bool:
    return any(
        _segment_bouncer_subcommand(segment) in _BOUNCER_SELF_MANAGEMENT_COMMANDS
        for segment in _shell_segments(command)
    )


def _is_protected_bouncer_path(value: str, cwd: Path) -> bool:
    try:
        path = Path(value).expanduser()
        lexical = (cwd / path).absolute() if not path.is_absolute() else path.absolute()
        resolved = lexical.resolve()
    except (OSError, ValueError):
        return False
    protected_user_files = {
        (Path.home() / ".config" / "bouncer" / name).resolve()
        for name in ("config.yaml", "policy.md", "system_prompt.txt")
    }
    def protected(candidate: Path) -> bool:
        if candidate in protected_user_files:
            return True
        return candidate.parent.name == ".bouncer" and candidate.name in {
            "config.yaml", "config.local.yaml", "policy.md", "policy.local.md",
        }
    return protected(lexical) or protected(resolved)


def _shell_mutates_protected_path(command: str, cwd: Path) -> bool:
    mutators = {
        "chmod", "chown", "cp", "install", "ln", "mv", "perl", "rm", "sed",
        "tee", "touch", "truncate",
    }
    for segment in _shell_segments(command):
        if not segment:
            continue
        executable = Path(segment[0]).name
        for index, part in enumerate(segment):
            if part in (">", ">>") and index + 1 < len(segment):
                if _is_protected_bouncer_path(segment[index + 1], cwd):
                    return True
            if executable in mutators and _is_protected_bouncer_path(part, cwd):
                return True
    return False


def _management_denial_reason(
    tool_name: str,
    tool_input: dict,
    cwd: Path,
) -> str | None:
    if _is_bouncer_self_management_command(tool_input.get("command", "")):
        return ("Bouncer policy and configuration management must be run directly "
                "by the user, outside an agent tool call")
    if _shell_mutates_protected_path(tool_input.get("command", ""), cwd):
        return ("Bouncer policy and configuration files cannot be modified "
                "by an agent shell command")
    mutating_tool = any(word in tool_name.lower() for word in ("write", "edit", "move", "delete"))
    if mutating_tool:
        for key in (
            "file_path", "path", "source", "source_path", "destination",
            "destination_path", "target", "target_path",
        ):
            value = tool_input.get(key)
            if isinstance(value, str) and _is_protected_bouncer_path(value, cwd):
                return ("Bouncer policy and configuration files cannot be modified "
                        "by an agent tool call")
    if "patch" in tool_name.lower():
        patch_text = tool_input.get("patchText") or tool_input.get("patch_text") or ""
        for line in str(patch_text).splitlines():
            prefixes = ("*** Add File: ", "*** Update File: ", "*** Delete File: ",
                        "*** Move to: ")
            for prefix in prefixes:
                if line.startswith(prefix) and _is_protected_bouncer_path(
                    line[len(prefix):].strip(), cwd
                ):
                    return ("Bouncer policy and configuration files cannot be modified "
                            "by an agent patch tool call")
    return None


def _parse_escalate_command(command: str) -> str | None:
    """If `command` is a `bouncer escalate [reason]` invocation, return the
    reason (possibly ""); otherwise None. This is the out-of-band escalation
    signal that works for any tool, not just Bash."""
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        return None
    if not parts or Path(parts[0]).name != "bouncer":
        return None
    idx = 1
    if idx < len(parts) and parts[idx] in ("-g", "--global"):
        idx += 1
    if idx < len(parts) and parts[idx] == "escalate":
        return " ".join(parts[idx + 1:])
    return None


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
    if not tool_intercepted(tool_name, config):
        return f"tool {tool_name!r} not intercepted (tools/groups config)"
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

    management_denial = _management_denial_reason(tool_name, tool_input, cwd_path)
    if management_denial:
        return "DENY", management_denial, "DENY", 0, None

    config = _merged_config(cwd_path)
    skip = _skip_reason(tool_name, tool_input, config)
    if skip:
        return "SKIP", skip, None, None, None

    command = tool_input.get("command", "")

    escalation = parse_escalation(command)
    if escalation is not None:
        escalate_reason, _underlying = escalation
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
    bouncer_dir    = _find_bouncer_dir(cwd_path)  # non-None given the gate above
    rid            = os.getpid()
    command        = tool_input.get("command", "")

    management_denial = _management_denial_reason(tool_name, tool_input, cwd_path)
    if management_denial:
        log_decision(tool_name, tool_input, cwd, "DENY", management_denial,
                     config, proj_log)
        notify_decision(
            cfg=config,
            tool_name=tool_name,
            tool_input=tool_input,
            cwd=cwd,
            session_id=session_id,
            decision="DENY",
            action="DENY",
            reason=management_denial,
            request_id=None,
            proj_log=proj_log,
        )
        _emit_hook_response("DENY", management_denial, fmt)
        return

    # `bouncer escalate [reason]` is the out-of-band escalation signal that
    # works for any tool: it arms a one-shot grant for this project's most
    # recent denial, then the agent re-issues the denied call. Intercept it
    # here (before skip/LLM) so the arming runs with the hook payload's
    # session_id + cwd. Allowing the command lets the CLI print a confirmation.
    escalate_reason = _parse_escalate_command(command)
    if escalate_reason is not None:
        if bouncer_dir is not None:
            arm_escalation(bouncer_dir, escalate_reason)
        _emit_hook_response("ALLOW", "bouncer: escalation armed", fmt)
        return

    # Bail on the config-derived SKIP conditions (disabled, tool not in the
    # intercept list, bouncer's own diagnostic commands) BEFORE logging a
    # PENDING entry — otherwise a non-intercepted call strands a PENDING with
    # no resolving entry. Shares _skip_reason with get_classification.
    if _skip_reason(tool_name, tool_input, config):
        sys.exit(0)

    # ESCALATE bypasses the LLM, so we log a single entry (no PENDING).
    if parse_escalation(command) is not None:
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
                    "The command under your `# ESCALATE:` marker doesn't match "
                    "a command you submitted recently. Resubmit it byte-for-byte "
                    "(whitespace aside), changing only the marker, then escalate "
                    "that exact text. Escalation is gated to a command bouncer "
                    "has already seen this session."
                )
                log_decision(tool_name, tool_input, cwd, "DENY", reject,
                             config, proj_log)
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

    # Cross-tool escalation: if the agent armed a grant for this exact call (via
    # `bouncer escalate`) after it was denied, honor it as an ESCALATE without
    # consulting the LLM. One-shot — take_grant consumes it.
    grant_reason = (take_grant(bouncer_dir, tool_name, tool_input)
                    if bouncer_dir is not None else None)
    if grant_reason is not None:
        log_decision(tool_name, tool_input, cwd, "ESCALATE", grant_reason,
                     config, proj_log)
        notify_decision(
            cfg=config,
            tool_name=tool_name,
            tool_input=tool_input,
            cwd=cwd,
            session_id=session_id,
            decision="ESCALATE",
            action="ASK",
            reason=grant_reason,
            request_id=None,
            proj_log=proj_log,
        )
        _emit_hook_response("ASK", f"agent escalation requested: {grant_reason}", fmt)
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
    # Record this bare attempt so a later `# ESCALATE:` of the same command is
    # recognized as a genuine retry rather than a pre-emptive escalation.
    if config.get("escalation_requires_attempt", True):
        record_attempt(command, session_id)
    # Remember a denial so a later `bouncer escalate` can route this exact call
    # to the user — the cross-tool equivalent of the bash attempt gate.
    if decision == "DENY" and bouncer_dir is not None:
        record_denial(bouncer_dir, tool_name, tool_input, reason)
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
