import json
import os
import signal
from pathlib import Path

from ..config import USER_LOG_FILE


def cmd_abort(args):
    cwd = str(Path.cwd().resolve())

    if not USER_LOG_FILE.exists():
        print("No bouncer log found.")
        return

    entries = []
    with open(USER_LOG_FILE) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except Exception:
                pass

    pending_pids = set()
    resolved_pids = set()
    for e in entries:
        pid = e.get("request_id")
        if pid is None:
            continue
        if e.get("decision") == "PENDING" and e.get("cwd", "") == cwd:
            pending_pids.add(pid)
        elif e.get("decision") in ("ALLOW", "DENY", "UNSURE", "ESCALATE"):
            resolved_pids.add(pid)

    live_pids = []
    for pid in pending_pids - resolved_pids:
        try:
            os.kill(pid, 0)
            live_pids.append(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            live_pids.append(pid)

    if not live_pids:
        print("No pending bouncer requests found for this project.")
        return

    if len(live_pids) > 1:
        print(f"Note: {len(live_pids)} pending requests found — signaling all.")

    signaled, failed = [], []
    for pid in live_pids:
        try:
            os.kill(pid, signal.SIGUSR1)
            signaled.append(pid)
        except Exception as e:
            failed.append((pid, str(e)))

    if signaled:
        print(f"Aborted {len(signaled)} pending request(s) → ALLOW "
              f"(PID{'s' if len(signaled) > 1 else ''}: "
              f"{', '.join(str(p) for p in signaled)})")
    for pid, err in failed:
        print(f"Failed to signal PID {pid}: {err}")
