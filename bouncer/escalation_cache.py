"""Per-session record of attempted commands, used to gate escalation.

An agent escalates a denied command by re-running it with a `# ESCALATE:`
prefix. To stop agents from pre-emptively escalating commands they never
actually tried, we keep a lightweight per-session record of the (bare,
non-escalate) commands bouncer has seen. An escalation is only honored if its
underlying command was attempted recently.

Matching is forgiving about whitespace only: both sides are normalized by
collapsing every run of whitespace to a single space. Anything else must be
byte-identical.
"""

import json
import time
from pathlib import Path

from . import config as _config

ESCALATION_DIR = _config.HOME / ".local" / "share" / "bouncer" / "escalation"
_MAX_ENTRIES = 100


def _cache_file(session_id: str) -> Path:
    return ESCALATION_DIR / f"{session_id}.json"


def normalize(command: str) -> str:
    """Collapse every run of whitespace to a single space; strip the ends."""
    return " ".join(command.split())


def strip_escalate_prefix(command: str) -> str:
    """The command an escalation wraps: everything after the first line, which
    carries the `# ESCALATE:` marker (parsed the same way as classify.py)."""
    return "\n".join(command.split("\n")[1:])


def record_attempt(command: str, session_id: str) -> None:
    """Record that the agent attempted `command` (a bare, non-escalate call)."""
    norm = normalize(command)
    if not norm:
        return
    try:
        cf = _cache_file(session_id)
        entries = []
        if cf.exists():
            try:
                entries = json.loads(cf.read_text(encoding="utf-8"))
            except Exception:
                entries = []
        entries.append({"cmd": norm, "ts": time.time()})
        entries = entries[-_MAX_ENTRIES:]
        ESCALATION_DIR.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps(entries), encoding="utf-8")
    except Exception:
        pass


def was_attempted(command: str, session_id: str, ttl_s: float) -> bool:
    """True if `command` (whitespace-normalized) was recorded within ttl_s."""
    norm = normalize(command)
    if not norm:
        return False
    try:
        cf = _cache_file(session_id)
        if not cf.exists():
            return False
        entries = json.loads(cf.read_text(encoding="utf-8"))
    except Exception:
        return False
    cutoff = time.time() - ttl_s
    for e in entries:
        if e.get("cmd") == norm and e.get("ts", 0) >= cutoff:
            return True
    return False
