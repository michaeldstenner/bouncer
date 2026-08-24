import json
import uuid
from datetime import datetime
from pathlib import Path

from . import config as _config


_INPUT_SUMMARY_BUDGET = 2000


def _input_summary(tool_input: dict) -> str:
    """Serialize a bounded request without cutting JSON mid-token."""
    full = json.dumps(tool_input, ensure_ascii=False)
    if len(full) <= _INPUT_SUMMARY_BUDGET:
        return full

    summary = {}
    marker = "…[truncated]"

    def add_field(key: str, value, budget: int) -> bool:
        nonlocal summary
        candidate = dict(summary)
        candidate[key] = value
        if len(json.dumps(candidate, ensure_ascii=False)) <= budget:
            summary = candidate
            return True
        source = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        low, high = 0, len(source)
        best = None
        while low <= high:
            middle = (low + high) // 2
            candidate = dict(summary)
            candidate[key] = source[:middle] + marker
            if len(json.dumps(candidate, ensure_ascii=False)) <= budget:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        if best is not None:
            summary = best
            return True
        return False

    # Reserve independent space for path and command so one cannot crowd the
    # other out of a security review record.
    if "file_path" in tool_input:
        add_field("file_path", tool_input["file_path"], 600)
    if "command" in tool_input:
        add_field("command", tool_input["command"], 1700)

    omitted = []
    for key, value in tool_input.items():
        if key in summary or key in ("file_path", "command"):
            continue
        if not add_field(key, value, 1880):
            omitted.append(key)
    if omitted:
        add_field("_omitted_fields", omitted, _INPUT_SUMMARY_BUDGET)
    return json.dumps(summary, ensure_ascii=False)


def _log_mode(decision: str, cfg: dict) -> str:
    """How much of a row to persist for `decision`, per `log.verbosity`.

    Returns one of:
      "full"    — write the complete entry
      "compact" — write only the fields the activity strip needs
                  (timestamp, tool, decision), dropping the noisy command
                  text and reason so the strip stays complete without
                  bloating the log
      "skip"    — write nothing

    The compact tier is what lets the strip survive a filtered verbosity:
    `deny_only` keeps full detail for denials but still records a light
    marker for everything else.  `off` is a true kill switch (strip empty).
    """
    verbosity = cfg.get("log", {}).get("verbosity", "all")
    if verbosity == "off":
        return "skip"
    if verbosity == "deny_only":
        return "full" if decision in ("DENY", "BLOCK") else "compact"
    return "full"


def _maybe_prune_log(log_file: Path, max_entries: int | None) -> None:
    if not max_entries or not log_file.exists():
        return

    # Only prune if the file size suggests we're significantly over the limit.
    # This avoids expensive rewrites on every single decision.
    # 300 bytes is a safe upper bound for a JSONL log entry.
    if log_file.stat().st_size < (max_entries * 300 * 1.2):
        return

    from collections import deque
    with open(log_file, "rb") as f:
        last_lines = deque(f, maxlen=max_entries)

    with open(log_file, "wb") as f:
        f.writelines(last_lines)


def llm_debug_log_file(cwd: str | Path | None = None) -> Path:
    d = _config._find_bouncer_dir(Path(cwd) if cwd else None)
    if d:
        return d / "llm_debug.jsonl"
    return _config.USER_LOG_FILE.with_name("llm_debug.jsonl")


def log_llm_debug(
    cwd: str,
    cfg: dict,
    provider: str,
    model: str,
    request_payload: dict,
    response_body: dict | None = None,
    response_text: str | None = None,
    error: str | None = None,
    elapsed_s: float | None = None,
    queue_snapshot: list[dict] | None = None,
) -> None:
    if not cfg.get("log", {}).get("llm_debug", False) and not queue_snapshot:
        return

    headers = dict(request_payload.get("headers", {}))
    if "Authorization" in headers:
        headers["Authorization"] = "Bearer ***REDACTED***"

    entry = {
        "timestamp": datetime.now().isoformat(),
        "provider": provider,
        "model": model,
        "request": {
            **request_payload,
            "headers": headers,
        },
        "response_body": response_body,
        "response_text": response_text,
        "error": error,
    }
    if elapsed_s is not None:
        entry["elapsed_s"] = round(elapsed_s, 3)
    if queue_snapshot is not None:
        entry["queue_snapshot"] = queue_snapshot

    log_file = llm_debug_log_file(cwd)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "ab") as f:
            f.write((json.dumps(entry) + "\n").encode())
    except OSError:
        pass


def _append_row(entry: dict, proj_log: Path | None) -> tuple[bool, bool]:
    """Append one JSONL row to the user log and (if given) the project log.

    Returns (wrote_user_log, wrote_proj_log).  Failures are swallowed so
    logging never breaks a real decision.
    """
    line = (json.dumps(entry) + "\n").encode()
    wrote_user_log = wrote_proj_log = False
    try:
        _config.USER_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_config.USER_LOG_FILE, "ab") as f:
            f.write(line)
        wrote_user_log = True
    except OSError:
        pass
    if proj_log:
        try:
            proj_log.parent.mkdir(parents=True, exist_ok=True)
            with open(proj_log, "ab") as f:
                f.write(line)
            wrote_proj_log = True
        except OSError:
            pass
    return wrote_user_log, wrote_proj_log


def log_break(cwd: str | Path | None, cfg: dict) -> None:
    """Append a turn-boundary marker so the activity strip can draw a dot.

    Written into the project log as a minimal row, inserted by whatever
    harness hook fires (e.g. Claude Code's UserPromptSubmit).  Harnesses
    without such a hook simply produce no breaks.  Honors `log.verbosity`:
    suppressed entirely under `off`.
    """
    if _log_mode("BREAK", cfg) == "skip":
        return
    proj_log = _config.project_log_file(Path(cwd) if cwd else None)
    if not proj_log:
        return
    # Project log only: a break is a turn boundary for this project's strip,
    # and would be meaningless interleaved into the cross-project user log.
    entry = {"timestamp": datetime.now().isoformat(), "decision": "BREAK"}
    line = (json.dumps(entry) + "\n").encode()
    try:
        proj_log.parent.mkdir(parents=True, exist_ok=True)
        with open(proj_log, "ab") as f:
            f.write(line)
    except OSError:
        pass


def log_decision(
    tool_name: str,
    tool_input: dict,
    cwd: str,
    decision: str,
    reason: str,
    cfg: dict | None = None,
    proj_log: Path | None = None,
    request_id: int | None = None,
    elapsed_s: float | None = None,
    prompt_chars: int | None = None,
    queue_snapshot: list[dict] | None = None,
) -> None:
    # PENDING is an in-flight placeholder (cfg is None there) and always
    # written; real decisions consult verbosity for their detail level.
    mode = "full"
    if cfg and decision != "PENDING":
        mode = _log_mode(decision, cfg)
        if mode == "skip":
            return
    entry: dict = {
        "event_id":  uuid.uuid4().hex,
        "timestamp": datetime.now().isoformat(),
        "tool":      tool_name,
        "decision":  decision,
    }
    if mode == "full":
        entry["cwd"]           = cwd
        entry["input_summary"] = _input_summary(tool_input)
        entry["reason"]        = reason
        if request_id is not None:
            entry["request_id"] = request_id
        if elapsed_s is not None:
            entry["elapsed_s"] = round(elapsed_s, 3)
        if prompt_chars is not None:
            entry["prompt_chars"] = prompt_chars
        if queue_snapshot is not None:
            entry["queue_snapshot"] = queue_snapshot
    wrote_user_log, wrote_proj_log = _append_row(entry, proj_log)
    if cfg and decision != "PENDING":
        max_entries = cfg.get("log", {}).get("max_entries")
        if wrote_user_log:
            try:
                _maybe_prune_log(_config.USER_LOG_FILE, max_entries)
            except OSError:
                pass
        if proj_log and wrote_proj_log:
            try:
                _maybe_prune_log(proj_log, max_entries)
            except OSError:
                pass
