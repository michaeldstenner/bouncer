"""Cross-tool escalation via an out-of-band signal.

The `# ESCALATE:` comment only works for Bash, because a shell command has an
inert place to carry the marker. Other tools (Read/Write/Edit/WebFetch/MCP/...)
have no such carrier, so escalation for them uses a side-channel instead:

    Tool        -> bouncer DENY        (the denial is recorded here)
    bouncer escalate "<reason>"        (arms a one-shot grant for that denial)
    Tool (same call, re-issued)        (grant matches -> ASK -> user decides)

The three steps are linked by the **fingerprint of the call itself** (tool name
+ canonical input). State is keyed on the **project** (the resolved `.bouncer/`
dir), which is universal — unlike `session_id`, which several harnesses omit.
The grant is fingerprint-bound, one-shot, and short-lived, so it can only ever
escalate the exact call it was armed for, only ever to an ASK (never an
auto-allow), and only ever a call that was genuinely denied. The one accepted
residual: two byte-identical denied calls in the same project share a
fingerprint, so a grant armed for one could be consumed by the other — which is
fine, because they are the same call and the result is still a human ASK.

Everything here is plain JSON-file state under the dir bouncer already uses, so
it carries no platform-specific dependency.
"""

import hashlib
import json
import time
from pathlib import Path

from . import config as _config

# Reuse the escalation state dir; project grant files use a distinct prefix so
# they never collide with the per-session attempt files (escalation_cache.py).
GRANT_DIR = _config.HOME / ".local" / "share" / "bouncer" / "escalation"

_MAX_DENIALS = 50
_DENIAL_TTL_S = 600.0
_GRANT_TTL_S = 120.0


def fingerprint(tool_name: str, tool_input: dict) -> str:
    """Stable identity of a tool call: tool name + canonical input."""
    canonical = json.dumps(tool_input, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    digest = hashlib.sha1(f"{tool_name}\x00{canonical}".encode("utf-8"))
    return digest.hexdigest()[:16]


def _grant_file(project_dir: Path) -> Path:
    key = hashlib.sha1(str(project_dir.resolve()).encode("utf-8")).hexdigest()[:16]
    return GRANT_DIR / f"grant-{key}.json"


def _load(project_dir: Path) -> dict:
    try:
        data = json.loads(_grant_file(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"denials": [], "grant": None}
    if not isinstance(data, dict):
        return {"denials": [], "grant": None}
    data.setdefault("denials", [])
    data.setdefault("grant", None)
    return data


def _store(project_dir: Path, data: dict) -> None:
    try:
        GRANT_DIR.mkdir(parents=True, exist_ok=True)
        _grant_file(project_dir).write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def record_denial(project_dir: Path, tool_name: str, tool_input: dict,
                  reason: str) -> None:
    """Remember that this exact call was denied, so it can later be escalated."""
    data = _load(project_dir)
    now = time.time()
    cutoff = now - _DENIAL_TTL_S
    denials = [d for d in data["denials"] if d.get("ts", 0) >= cutoff]
    denials.append({
        "fp": fingerprint(tool_name, tool_input),
        "tool": tool_name,
        "reason": reason,
        "ts": now,
    })
    data["denials"] = denials[-_MAX_DENIALS:]
    _store(project_dir, data)


def arm_escalation(project_dir: Path, reason: str) -> dict | None:
    """Arm a one-shot grant for the project's most recent denial. Returns the
    targeted denial (for the caller to report), or None if there is nothing to
    escalate."""
    data = _load(project_dir)
    cutoff = time.time() - _DENIAL_TTL_S
    candidates = [d for d in data["denials"] if d.get("ts", 0) >= cutoff]
    if not candidates:
        return None
    target = candidates[-1]
    data["grant"] = {
        "fp": target["fp"],
        "tool": target.get("tool"),
        "reason": reason or f"escalating denied {target.get('tool', 'call')}",
        "ts": time.time(),
    }
    _store(project_dir, data)
    return target


def take_grant(project_dir: Path, tool_name: str, tool_input: dict) -> str | None:
    """Consume a pending grant if it matches this call (same fingerprint, not
    expired). Returns the escalation reason, or None."""
    data = _load(project_dir)
    grant = data.get("grant")
    if not grant:
        return None
    if time.time() - grant.get("ts", 0) > _GRANT_TTL_S:
        data["grant"] = None
        _store(project_dir, data)
        return None
    if grant.get("fp") != fingerprint(tool_name, tool_input):
        return None
    # One-shot: consume it.
    data["grant"] = None
    _store(project_dir, data)
    return grant.get("reason") or "agent escalation requested"
