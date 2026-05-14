import json
from pathlib import Path

from ..activity import _render_activity
from ..config import _activity_file, _merged_config, project_has_bouncer, project_log_file


def _marker(c: str, decision: str, as_format: str) -> str:
    if as_format == "ansi":
        if decision == "error":
            return f"\033[31m{c}\033[0m"
        return f"\033[2m{c}\033[0m"
    if as_format == "json":
        return json.dumps([{"c": c, "d": decision}])
    if as_format == "tmux":
        if decision == "error":
            return f"#[fg=red]{c}#[default]"
        return f"#[dim]{c}#[default]"
    return c


def _print_project_marker(cwd_path: Path, as_format: str) -> None:
    if project_has_bouncer(cwd_path):
        if _merged_config(cwd_path).get("enabled", True):
            print(_marker("○", "muted", as_format), end="")
        else:
            print(_marker("⊘", "error", as_format), end="")
    else:
        print(_marker("⊘", "muted", as_format), end="")


def _project_entries(cwd_path: Path, width: int) -> list[dict]:
    log_file = project_log_file(cwd_path)
    if not log_file or not log_file.exists():
        return []

    entries: list[dict] = []
    try:
        lines = log_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        decision = str(row.get("decision", "")).upper()
        if not decision or decision == "PENDING":
            continue
        entries.append({"d": decision, "t": row.get("tool", "?")})
        if len(entries) >= width:
            break
    return entries


def cmd_activity(args):
    width      = getattr(args, "width", 10)
    session_id = getattr(args, "session", None)
    cwd_arg    = getattr(args, "cwd", None)
    as_format  = getattr(args, "as_format", "plain")
    project    = getattr(args, "project", False)

    cwd_path = Path(cwd_arg) if cwd_arg else Path.cwd()

    if project:
        entries = _project_entries(cwd_path, width)
        if entries:
            out = _render_activity(entries, as_format=as_format)
            if out:
                print(out, end="")
        else:
            _print_project_marker(cwd_path, as_format)
        return

    if not session_id:
        return

    af = _activity_file(session_id)

    if not af.exists():
        if cwd_arg:
            _print_project_marker(cwd_path, as_format)
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
