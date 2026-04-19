import json
import sys

from ..classify import run_classify


def cmd_classify(args):
    try:
        hook_input = json.load(sys.stdin)
    except Exception as e:
        print(f"bouncer classify: failed to parse stdin: {e}", file=sys.stderr)
        sys.exit(0)  # fail open

    tool_name  = hook_input.get("tool_name", "unknown")
    tool_input = hook_input.get("tool_input", {})
    cwd        = hook_input.get("cwd", "")
    session_id = hook_input.get("session_id", "unknown")
    fmt        = getattr(args, "format", "json")

    run_classify(tool_name, tool_input, cwd, session_id, fmt)
