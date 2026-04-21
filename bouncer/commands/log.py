import json
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta
from pathlib import Path

from ..colors import RESET, BOLD, DIM, YELLOW, DECISION_COLORS, WHITE
from ..activity import _append_activity_entry
from ..config import USER_LOG_FILE, project_log_file, project_has_bouncer


def _parse_since(s: str | None) -> datetime | None:
    if not s:
        return None
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        num  = int(s[:-1])
        unit = s[-1].lower()
        if unit not in units:
            raise ValueError
        return datetime.now() - timedelta(seconds=num * units[unit])
    except (ValueError, IndexError):
        print(f"Error: Invalid --since value '{s}'. Use format: 30m, 2h, 1d",
              file=sys.stderr)
        sys.exit(1)


def _extract_command(summary: str) -> str:
    try:
        data = json.loads(summary)
    except (json.JSONDecodeError, TypeError):
        return summary
    if isinstance(data, dict):
        return data.get("command", summary)
    return summary


def _format_entry(entry: dict, width: int | None = None, wrap: bool = True) -> str:
    if width is None:
        width = shutil.get_terminal_size(fallback=(120, 24)).columns

    ts       = entry.get("timestamp", "")[:19].replace("T", " ")
    decision = entry.get("decision", "?").upper()
    reason   = entry.get("reason", "")
    cmd      = _extract_command(entry.get("input_summary", ""))

    latency = ""
    if "_pending_ts" in entry:
        try:
            t0 = datetime.fromisoformat(entry["_pending_ts"])
            t1 = datetime.fromisoformat(entry["timestamp"])
            secs = (t1 - t0).total_seconds()
            latency = f"  {DIM}({secs:.1f}s){RESET}"
        except Exception:
            pass

    PREFIX = 31
    indent = " " * PREFIX
    avail  = max(20, width - PREFIX)
    color  = DECISION_COLORS.get(decision, WHITE)

    if wrap:
        cmd_lines    = textwrap.wrap(cmd, avail) or [""]
        reason_lines = textwrap.wrap(reason, avail) or [""]
    else:
        cmd_lines    = [cmd]
        reason_lines = [reason]

    parts = [f"{DIM}{ts}{RESET}  {color}{decision:<8}{RESET}  {color}{cmd_lines[0]}{RESET}{latency}"]
    for ln in cmd_lines[1:]:
        parts.append(f"{indent}{color}{ln}{RESET}")
    for ln in reason_lines:
        parts.append(f"{indent}{DIM}{ln}{RESET}")

    return "\n".join(parts)


def _stitch(entries: list[dict]) -> list[dict]:
    pending: dict[int, dict] = {}
    result = []
    for entry in entries:
        rid      = entry.get("request_id")
        decision = entry.get("decision", "")
        if decision == "PENDING":
            if rid is not None:
                pending[rid] = entry
        elif rid is not None and rid in pending:
            merged = dict(entry)
            merged["_pending_ts"] = pending.pop(rid)["timestamp"]
            result.append(merged)
        else:
            result.append(entry)
    return result


def _entry_matches(entry: dict, since: datetime | None, filter_dec: str) -> bool:
    if since:
        try:
            if datetime.fromisoformat(entry["timestamp"]) < since:
                return False
        except Exception:
            pass
    if filter_dec:
        d = entry.get("decision", "").upper()
        if filter_dec in ("BLOCK", "DENY"):
            if d not in ("BLOCK", "DENY"):
                return False
        elif d != filter_dec:
            return False
    return True


def cmd_log(args):
    if getattr(args, "mark_break", False):
        try:
            hook_input = json.load(sys.stdin)
            cwd = hook_input.get("cwd")
            if project_has_bouncer(Path(cwd) if cwd else None):
                session_id = hook_input.get("session_id", "unknown")
                _append_activity_entry(
                    {"d": "BREAK", "ts": datetime.now().isoformat()},
                    session_id,
                    width=50,
                )
        except Exception:
            pass
        return

    if args.user:
        log_file = USER_LOG_FILE
    else:
        lf = project_log_file()
        if lf is None:
            print(f"{YELLOW}No project config found.{RESET} "
                  f"Use -u for user log, or run 'bouncer init' first.")
            sys.exit(1)
        log_file = lf
        if not log_file.exists():
            print(f"{DIM}No project log yet.{RESET} "
                  f"Entries appear here once bouncer classifies commands.")
            return

    if not log_file.exists():
        print(f"{DIM}Log file not found:{RESET} {log_file}")
        return

    since      = _parse_since(getattr(args, "since", None))
    filter_dec = (getattr(args, "filter_dec", None) or "").upper()
    chop       = getattr(args, "pager", False)
    width      = getattr(args, "columns", None)

    def _parse_line(raw: str) -> dict | None:
        raw = raw.strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _render(entry: dict, wrap: bool = True) -> str:
        return _format_entry(entry, width=width, wrap=wrap)

    if args.tail:
        PENDING_TIMEOUT = 35
        pending_buf: dict[int, tuple[dict, float]] = {}

        def _flush_expired():
            now = time.time()
            for rid, (p_entry, ts) in list(pending_buf.items()):
                if now - ts > PENDING_TIMEOUT:
                    print(_render(p_entry), flush=True)
                    del pending_buf[rid]

        with open(log_file, encoding="utf-8") as f:
            raw_entries = [e for line in f if (e := _parse_line(line))]
            for entry in _stitch(raw_entries):
                if _entry_matches(entry, since, filter_dec):
                    print(_render(entry))

            print(f"{DIM}--- following {log_file.name} (ctrl-c to stop) ---{RESET}",
                  flush=True)
            try:
                while True:
                    line = f.readline()
                    if line:
                        entry = _parse_line(line)
                        if entry is None:
                            continue
                        rid      = entry.get("request_id")
                        decision = entry.get("decision", "")
                        if decision == "PENDING" and rid is not None:
                            pending_buf[rid] = (entry, time.time())
                        elif rid is not None and rid in pending_buf:
                            p_entry, _ = pending_buf.pop(rid)
                            merged = dict(entry)
                            merged["_pending_ts"] = p_entry["timestamp"]
                            if _entry_matches(merged, since, filter_dec):
                                print(_render(merged), flush=True)
                        else:
                            if _entry_matches(entry, since, filter_dec):
                                print(_render(entry), flush=True)
                    else:
                        _flush_expired()
                        time.sleep(0.4)
            except KeyboardInterrupt:
                pass
    else:
        with open(log_file, encoding="utf-8") as f:
            raw_entries = [e for line in f if (e := _parse_line(line))]
        lines = []
        for entry in _stitch(raw_entries):
            if _entry_matches(entry, since, filter_dec):
                lines.append(_render(entry, wrap=not chop))
        if not lines:
            print(f"{DIM}No matching entries.{RESET}")
            return
        text       = "\n".join(lines) + "\n"
        less_flags = "-RS" if chop else "-R"
        proc = subprocess.Popen(["less", less_flags], stdin=subprocess.PIPE)
        try:
            proc.communicate(text.encode())
        except BrokenPipeError:
            pass
