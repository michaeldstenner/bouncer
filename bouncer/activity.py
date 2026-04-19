import json
from datetime import datetime
from pathlib import Path

from . import config as _config

_TOOL_CHARS = {
    "Bash":      "B",
    "Write":     "W",
    "Edit":      "E",
    "Read":      "R",
    "Glob":      "G",
    "Grep":      "G",
    "Task":      "T",
    "WebFetch":  "F",
    "WebSearch": "S",
}

# Always emit ANSI — statusline.sh passes them through via echo -e even in non-TTY context
_ACTIVITY_COLORS = {
    "ALLOW":    "\033[32m",
    "DENY":     "\033[30;41m",
    "BLOCK":    "\033[30;41m",
    "UNSURE":   "\033[33m",
    "OVERRIDE": "\033[36m",
}
_ACTIVITY_RESET = "\033[0m"


def _tool_char(tool_name: str) -> str:
    key = tool_name.capitalize()
    return _TOOL_CHARS.get(key) or _TOOL_CHARS.get(tool_name, "?")


def _render_activity(entries: list[dict]) -> str:
    parts = []
    for e in entries:
        decision = e.get("d", "?").upper()
        if decision == "BREAK":
            parts.append("\033[2m·\033[0m")
            continue
        tool  = e.get("t", "?")
        char  = _tool_char(tool)
        color = _ACTIVITY_COLORS.get(decision, "")
        reset = _ACTIVITY_RESET if color else ""
        parts.append(f"{color}{char}{reset}")
    return "".join(parts)


def _update_activity(tool_name: str, decision: str, session_id: str, width: int = 10) -> None:
    try:
        af = _config._activity_file(session_id)
        entries = []
        if af.exists():
            try:
                entries = json.loads(af.read_text(encoding="utf-8"))
            except Exception:
                entries = []
        entries.append({"d": decision, "t": tool_name, "ts": datetime.now().isoformat()})
        entries = entries[-width:]
        _config.ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
        af.write_text(json.dumps(entries), encoding="utf-8")
    except Exception:
        pass


def _append_activity_entry(entry: dict, session_id: str, width: int = 10) -> None:
    try:
        af = _config._activity_file(session_id)
        entries = []
        if af.exists():
            try:
                entries = json.loads(af.read_text(encoding="utf-8"))
            except Exception:
                entries = []
        entries.append(entry)
        entries = entries[-width:]
        _config.ACTIVITY_DIR.mkdir(parents=True, exist_ok=True)
        af.write_text(json.dumps(entries), encoding="utf-8")
    except Exception:
        pass
