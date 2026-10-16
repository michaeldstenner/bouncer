"""`bouncer session-start` — Claude Code's SessionStart hook.

Says nothing unless bouncer is BYPASSED for the session's directory
(`enabled: bypass`). Then it says so once, where both the person and the
agent see it: `systemMessage` is shown to the user, `additionalContext` is
given to the agent. Bypass is never silent.

Only at session start: setting bypass in the middle of a session is
announced at the next one, and shown at any time by `bouncer status` and
`bouncer profile`.
"""
import json
import sys
from pathlib import Path

from ..config import bypass_source


def bypass_message(source: Path) -> str:
    return f"bouncer is BYPASSED here (enabled: bypass in {source})"


def cmd_session_start(args):
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        hook_input = {}
    cwd = hook_input.get("cwd") if isinstance(hook_input, dict) else None
    source = bypass_source(Path(cwd) if cwd else Path.cwd())
    if source is None:
        return
    line = bypass_message(source)
    print(json.dumps({
        "systemMessage": line,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                f"{line}. No tool call in this session is checked by "
                "bouncer: no hard guard, no policy, no log. The user set "
                "this; do not treat it as permission to change bouncer's "
                "config, here or anywhere else."),
        },
    }))
