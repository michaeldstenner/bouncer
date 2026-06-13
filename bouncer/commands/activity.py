import json
from pathlib import Path

from ..activity import _render_activity
from ..config import _merged_config, project_has_bouncer, project_log_file


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
    # Both the Claude Code statusline and the tmux/Codex strip render from the
    # same source — the project decision log — so break markers (logged by a
    # harness's prompt hook) and decisions flow through one path.  --session
    # and --project are kept for backward compatibility but no longer change
    # behavior.
    width     = getattr(args, "width", None)
    cwd_arg   = getattr(args, "cwd", None)
    as_format = getattr(args, "as_format", "plain")

    cwd_path = Path(cwd_arg) if cwd_arg else Path.cwd()
    cfg = _merged_config(cwd_path)
    if width is None:
        width = cfg.get("activity_width", 10)

    if cwd_arg and not cfg.get("enabled", True):
        _print_project_marker(cwd_path, as_format)
        return

    entries = _project_entries(cwd_path, width)
    if entries:
        out = _render_activity(entries, as_format=as_format, cfg=cfg)
        if out:
            print(out, end="")
    elif cwd_arg:
        _print_project_marker(cwd_path, as_format)
