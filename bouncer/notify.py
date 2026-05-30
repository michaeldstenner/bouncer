import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from . import config as _config


def _notify_config(cfg: dict) -> dict:
    raw = cfg.get("notify")
    if not raw:
        return {}
    if isinstance(raw, str):
        return {"command": raw}
    if isinstance(raw, dict):
        return raw
    return {}


def _wants_decision(notify_cfg: dict, decision: str) -> bool:
    decisions = notify_cfg.get("decisions", "all")
    if decisions == "all":
        return True
    if isinstance(decisions, str):
        decisions = [decisions]
    if not isinstance(decisions, list):
        return True
    return decision.upper() in {str(d).upper() for d in decisions}


def _payload(
    *,
    tool_name: str,
    tool_input: dict,
    cwd: str,
    session_id: str,
    decision: str,
    action: str | None,
    reason: str,
    request_id: int | None,
    elapsed_s: float | None,
    prompt_chars: int | None,
    proj_log: Path | None,
) -> dict:
    cwd_path = Path(cwd) if cwd else Path.cwd()
    bouncer_dir = _config._find_bouncer_dir(cwd_path)
    command = tool_input.get("command") if isinstance(tool_input, dict) else None

    data = {
        "version": 1,
        "timestamp": datetime.now().isoformat(),
        "tool": tool_name,
        "tool_input": tool_input,
        "command": command,
        "cwd": str(cwd_path),
        "project": {
            "cwd": str(cwd_path),
            "bouncer_dir": str(bouncer_dir) if bouncer_dir else None,
            "log_file": str(proj_log) if proj_log else None,
        },
        "session_id": session_id,
        "decision": decision,
        "action": action,
        "reason": reason,
    }
    if request_id is not None:
        data["request_id"] = request_id
    if elapsed_s is not None:
        data["elapsed_s"] = round(elapsed_s, 3)
    if prompt_chars is not None:
        data["prompt_chars"] = prompt_chars
    return data


def notify_decision(
    *,
    cfg: dict,
    tool_name: str,
    tool_input: dict,
    cwd: str,
    session_id: str,
    decision: str,
    action: str | None,
    reason: str,
    request_id: int | None = None,
    elapsed_s: float | None = None,
    prompt_chars: int | None = None,
    proj_log: Path | None = None,
) -> None:
    notify_cfg = _notify_config(cfg)
    command = notify_cfg.get("command")
    if not command or not _wants_decision(notify_cfg, decision):
        return

    payload = _payload(
        tool_name=tool_name,
        tool_input=tool_input,
        cwd=cwd,
        session_id=session_id,
        decision=decision,
        action=action,
        reason=reason,
        request_id=request_id,
        elapsed_s=elapsed_s,
        prompt_chars=prompt_chars,
        proj_log=proj_log,
    )

    try:
        popen_kwargs = {
            "stdin": subprocess.PIPE,
            "cwd": str(Path(cwd) if cwd else Path.cwd()),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "shell": isinstance(command, str),
            "text": True,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            command,
            **popen_kwargs,
        )
        if proc.stdin:
            try:
                proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                proc.stdin.close()
            except OSError:
                pass
    except Exception:
        pass
