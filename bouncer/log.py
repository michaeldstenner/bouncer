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


def log_decision(
    tool_name: str,
    tool_input: dict,
    cwd: str,
    decision: str,
    reason: str,
    cfg: dict | None = None,
    proj_log: Path | None = None,
    request_id: int | None = None,
) -> None:
    if cfg and decision != "PENDING" and not _should_log(decision, cfg):
        return
    entry: dict = {
        "timestamp":     datetime.now().isoformat(),
        "tool":          tool_name,
        "cwd":           cwd,
        "input_summary": str(tool_input)[:200],
        "decision":      decision,
        "reason":        reason,
    }
    if request_id is not None:
        entry["request_id"] = request_id
    line = (json.dumps(entry) + "\n").encode()
    user_log = _config.USER_LOG_FILE
    user_log.parent.mkdir(parents=True, exist_ok=True)
    with open(user_log, "ab") as f:
        f.write(line)
    if proj_log:
        proj_log.parent.mkdir(parents=True, exist_ok=True)
        with open(proj_log, "ab") as f:
            f.write(line)
    if cfg and decision != "PENDING":
        max_entries = cfg.get("log", {}).get("max_entries")
        _maybe_prune_log(user_log, max_entries)
        if proj_log:
            _maybe_prune_log(proj_log, max_entries)
