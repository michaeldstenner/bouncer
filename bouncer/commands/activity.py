import json
from pathlib import Path

from ..activity import _render_activity
from ..config import _activity_file, _merged_config, project_has_bouncer


def cmd_activity(args):
    width      = getattr(args, "width", 10)
    session_id = getattr(args, "session", None)
    cwd_arg    = getattr(args, "cwd", None)
    as_format  = getattr(args, "as_format", "plain")

    if not session_id:
        return

    af = _activity_file(session_id)

    if not af.exists():
        if cwd_arg:
            cwd_path = Path(cwd_arg)
            if project_has_bouncer(cwd_path):
                if _merged_config(cwd_path).get("enabled", True):
                    if as_format == "ansi":
                        print("\033[2m○\033[0m", end="")
                    elif as_format == "json":
                        print(json.dumps([{"c": "○", "d": "muted"}]), end="")
                    else:
                        print("○", end="")
                else:
                    if as_format == "ansi":
                        print("\033[31m⊘\033[0m", end="")
                    elif as_format == "json":
                        print(json.dumps([{"c": "⊘", "d": "error"}]), end="")
                    else:
                        print("⊘", end="")
            else:
                if as_format == "ansi":
                    print("\033[2m⊘\033[0m", end="")
                elif as_format == "json":
                    print(json.dumps([{"c": "⊘", "d": "muted"}]), end="")
                else:
                    print("⊘", end="")
        return

    try:
        import json
        entries = json.loads(af.read_text(encoding="utf-8"))
    except Exception:
        return

    entries = entries[-width:]
    entries.reverse()
    out = _render_activity(entries, as_format=as_format)
    if out:
        print(out, end="")
