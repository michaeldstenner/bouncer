import json
from datetime import datetime
from pathlib import Path

from . import config as _config


def _should_log(decision: str, cfg: dict) -> bool:
    verbosity = cfg.get("log", {}).get("verbosity", "all")
    if verbosity == "off":
        return False
    if verbosity == "deny_only":
        return decision in ("DENY", "BLOCK")
    return True


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
    if cfg and decision != "PENDING" and not _should_log(decision, cfg):
        return
    entry: dict = {
        "timestamp":     datetime.now().isoformat(),
        "tool":          tool_name,
        "cwd":           cwd,
        "input_summary": json.dumps(tool_input, ensure_ascii=False)[:2000],
        "decision":      decision,
        "reason":        reason,
    }
    if request_id is not None:
        entry["request_id"] = request_id
    if elapsed_s is not None:
        entry["elapsed_s"] = round(elapsed_s, 3)
    if prompt_chars is not None:
        entry["prompt_chars"] = prompt_chars
    if queue_snapshot is not None:
        entry["queue_snapshot"] = queue_snapshot
    line = (json.dumps(entry) + "\n").encode()
    user_log = _config.USER_LOG_FILE
    wrote_user_log = False
    try:
        user_log.parent.mkdir(parents=True, exist_ok=True)
        with open(user_log, "ab") as f:
            f.write(line)
        wrote_user_log = True
    except OSError:
        pass
    wrote_proj_log = False
    if proj_log:
        try:
            proj_log.parent.mkdir(parents=True, exist_ok=True)
            with open(proj_log, "ab") as f:
                f.write(line)
            wrote_proj_log = True
        except OSError:
            pass
    if cfg and decision != "PENDING":
        max_entries = cfg.get("log", {}).get("max_entries")
        if wrote_user_log:
            try:
                _maybe_prune_log(user_log, max_entries)
            except OSError:
                pass
        if proj_log and wrote_proj_log:
            try:
                _maybe_prune_log(proj_log, max_entries)
            except OSError:
                pass
