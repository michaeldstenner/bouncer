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
import re
import time
from pathlib import Path

from . import config as _config

ESCALATION_DIR = _config.HOME / ".local" / "share" / "bouncer" / "escalation"
_MAX_ENTRIES = 100

# The escalation marker. Recognized either as a leading line (the documented,
# preferred form) or as a trailing inline comment on a single-line command. The
# `(?:^|(?<=\s))` guard requires the `#` to begin a line or follow whitespace,
# so a marker buried inside a quoted string (e.g. `grep "# ESCALATE:" file`) is
# NOT mistaken for a real escalation. Spacing around the marker is forgiving.
_ESCALATE_RE = re.compile(r'(?:^|(?<=\s))#[ \t]*ESCALATE:[ \t]*(?P<reason>[^\n]*)')


def _cache_file(session_id: str) -> Path:
    return ESCALATION_DIR / f"{session_id}.json"


def normalize(command: str) -> str:
    """Collapse every run of whitespace to a single space; strip the ends."""
    return " ".join(command.split())


def parse_escalation(command: str) -> tuple[str, str] | None:
    """Recognize an escalation request and split it into (reason, command).

    The `# ESCALATE: <reason>` marker may be a leading line (preferred) or a
    trailing inline comment; either way this returns the stated reason and the
    underlying command the escalation wraps (what the gate matches against prior
    bare attempts). Returns None when the command carries no escalation marker.
    """
    m = _ESCALATE_RE.search(command)
    if not m:
        return None
    reason = m.group("reason").strip()
    underlying = (command[:m.start()] + command[m.end():]).strip()
    return reason, underlying


def strip_escalate_prefix(command: str) -> str:
    """The command an escalation wraps, with the `# ESCALATE:` marker removed
    wherever it appears (leading line or trailing inline comment)."""
    parsed = parse_escalation(command)
    return parsed[1] if parsed else command


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
