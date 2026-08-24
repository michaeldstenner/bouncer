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
from .hook import _emit_hook_response, harness_can_ask, resolve_fallback
from .notify import notify_decision
from .profile import (note_harness, profile_allows_ask,
                      resolve_unattended_action)
from .providers import call_llm
from ._abort import ABORT_EVENT


_BOUNCER_DIAGNOSTIC_COMMANDS = {
    "activity",
    "check",
    "log",
    "profile",
    "status",
}


# Refusals that belong to the profile layer, not to policy. Both are returned
# to the agent immediately and create no ASK state, so there is nothing to hang
# in — the failure mode `solo` exists to remove.
_ESCALATION_OFF_REASON = (
    "Escalation is not available in this session, so this request cannot be "
    "sent to a human."
)

_PROFILE_SELF_SET_REASON = (
    "A session may not set its own bouncer profile. The profile decides "
    "whether you may appeal a denial to a human; changing it is the user's "
    "call, not yours. Ask the user to run 'bouncer profile <name>' if it is "
    "wrong."
)


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

    if index >= len(parts) or parts[index] not in _BOUNCER_DIAGNOSTIC_COMMANDS:
        return False

    # `bouncer profile` reads the profile; `bouncer profile <name>` sets it,
    # which is not a diagnostic — see the self-set guard in run_classify.
    if parts[index] == "profile" and len(parts) > index + 1:
        return False

    return True


def _parse_profile_set_command(command: str) -> str | None:
    """If `command` is a `bouncer profile <name>` invocation (the setting form,
    not the read-only bare one), return the name; otherwise None."""
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        return None
    if not parts or Path(parts[0]).name != "bouncer":
        return None
    idx = 1
    if idx < len(parts) and parts[idx] in ("-g", "--global"):
        idx += 1
    if idx < len(parts) and parts[idx] == "profile" and len(parts) > idx + 1:
        return parts[idx + 1]
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


def _fallback_action(config: dict, key: str, harness: str,
                     permission_mode: str | None = None) -> str:
    """The configured `on_unsure` / `on_unavailable` action, resolved through
    the profile.

    Under a profile with no ASK channel (`solo`), every path that could produce
    an ASK resolves to something else: the harness's own floor where one is
    known to decide without a human, and `deny` where none is. "Known" is
    narrow on purpose — for Claude Code it means the `auto` permission mode
    and nothing else, so an abstain can never turn into a prompt nobody
    answers. Under `live` the configured action is used as written."""
    action = config.get(key, "ask")
    if profile_allows_ask(config):
        return action
    return resolve_unattended_action(action, harness, permission_mode)


def get_classification(
    tool_name: str,
    tool_input: dict,
    cwd: str,
    harness: str = "unknown",
    permission_mode: str | None = None,
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

    escalation = parse_escalation(command)
    if escalation is not None:
        escalate_reason, _underlying = escalation
        if not profile_allows_ask(config):
            return "DENY", _ESCALATION_OFF_REASON, "DENY", None, None
        return "ESCALATE", escalate_reason, "ASK", None, None

    try:
        decision, reason, prompt_chars, snap = call_llm(tool_name, tool_input, cwd_path, config)
    except Exception as exc:
        decision, reason, prompt_chars, snap = None, str(exc), None, None

    if decision is None or decision in ("TIMEOUT", "LLM_ERROR"):
        display_dec = decision or "UNSURE"
        fallback_action = _fallback_action(config, "on_unavailable", harness,
                                           permission_mode)
        final_dec, final_reason = resolve_fallback(
            fallback_action,
            f"LLM unavailable: {reason}"
        )
        return display_dec, final_reason, final_dec, prompt_chars, snap

    if decision in ("ALLOW", "DENY"):
        return decision, reason, decision, prompt_chars, None

    # UNSURE
    fallback_action = _fallback_action(config, "on_unsure", harness,
                                       permission_mode)
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
    harness: str = "unknown",
    permission_mode: str | None = None,
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

    # Effective ASK capability = profile ∧ harness. Both halves are needed:
    # the profile says whether anyone is on the line, the format says whether
    # this harness has a channel to reach them at all. It feeds the DENY
    # wording below and, via note_harness, the indicator.
    profile_ask = profile_allows_ask(config)
    note_harness(bouncer_dir, harness, harness_can_ask(fmt), permission_mode)

    # Footgun cover, not a security boundary: an agent that notices `bouncer
    # profile` exists must not helpfully flip itself to `live`. The CLI
    # resolves its target project from its own cwd — the same cwd this hook
    # was handed — so any set seen here is a session setting its own profile.
    profile_target = _parse_profile_set_command(command)
    if profile_target is not None:
        log_decision(tool_name, tool_input, cwd, "DENY",
                     _PROFILE_SELF_SET_REASON, config, proj_log)
        _emit_hook_response("DENY", _PROFILE_SELF_SET_REASON, fmt, profile_ask)
        return

    # `bouncer escalate [reason]` is the out-of-band escalation signal that
    # works for any tool: it arms a one-shot grant for this project's most
    # recent denial, then the agent re-issues the denied call. Intercept it
    # here (before skip/LLM) so the arming runs with the hook payload's
    # session_id + cwd. Allowing the command lets the CLI print a confirmation.
    # Under a profile with no ASK channel there is nobody to arm it for, so
    # refuse before any grant state exists — an agent trying from habit or a
    # stale brief gets an immediate answer instead of a wait.
    escalate_reason = _parse_escalate_command(command)
    if escalate_reason is not None:
        if not profile_ask:
            log_decision(tool_name, tool_input, cwd, "DENY",
                         _ESCALATION_OFF_REASON, config, proj_log)
            _emit_hook_response("DENY", _ESCALATION_OFF_REASON, fmt,
                                profile_ask)
            return
        if bouncer_dir is not None:
            arm_escalation(bouncer_dir, escalate_reason)
        _emit_hook_response("ALLOW", "bouncer: escalation armed", fmt,
                            profile_ask)
        return

    # Bail on the config-derived SKIP conditions (disabled, tool not in the
    # intercept list, bouncer's own diagnostic commands) BEFORE logging a
    # PENDING entry — otherwise a non-intercepted call strands a PENDING with
    # no resolving entry. Shares _skip_reason with get_classification.
    if _skip_reason(tool_name, tool_input, config):
        sys.exit(0)

    # ESCALATE bypasses the LLM, so we log a single entry (no PENDING).
    if parse_escalation(command) is not None:
        # The profile gate sits ahead of this whole branch, because
        # parse_escalation short-circuits past the LLM straight to
        # _emit_hook_response — a check further down would never run. The
        # agent gets its DENY back immediately and no ASK state is created.
        if not profile_ask:
            log_decision(tool_name, tool_input, cwd, "DENY",
                         _ESCALATION_OFF_REASON, config, proj_log)
            notify_decision(
                cfg=config,
                tool_name=tool_name,
                tool_input=tool_input,
                cwd=cwd,
                session_id=session_id,
                decision="DENY",
                action="DENY",
                reason=_ESCALATION_OFF_REASON,
                request_id=None,
                proj_log=proj_log,
            )
            _emit_hook_response("DENY", _ESCALATION_OFF_REASON, fmt,
                                profile_ask)
            return

        decision, reason, action, _, _snap = get_classification(
            tool_name, tool_input, cwd, harness, permission_mode)
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
                _emit_hook_response("DENY", reject, fmt, profile_ask)
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
        _emit_hook_response(action, f"agent escalation requested: {reason}",
                            fmt, profile_ask)
        return

    # Cross-tool escalation: if the agent armed a grant for this exact call (via
    # `bouncer escalate`) after it was denied, honor it as an ESCALATE without
    # consulting the LLM. One-shot — take_grant consumes it.
    # Cross-tool grants are only consumed while the profile has an ASK
    # channel. Under `solo` the grant is left untouched to expire, and the
    # re-issued call goes to the LLM like any other — which is what a call
    # with no appeal available should do.
    grant_reason = (take_grant(bouncer_dir, tool_name, tool_input)
                    if bouncer_dir is not None and profile_ask else None)
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
        _emit_hook_response(
            "ASK", f"agent escalation requested: {grant_reason}",
            fmt, profile_ask)
        return

    signal.signal(signal.SIGUSR1, lambda sig, frame: ABORT_EVENT.set())

    log_decision(tool_name, tool_input, cwd, "PENDING", "calling LLM",
                 None, proj_log, rid)

    t0 = time.monotonic()
    decision, reason, action, prompt_chars, snap = get_classification(
        tool_name, tool_input, cwd, harness, permission_mode)
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
        _emit_hook_response(action, reason, fmt, profile_ask)
    else:
        sys.exit(0)
