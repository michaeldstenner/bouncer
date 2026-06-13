#!/usr/bin/env python3
import json
import subprocess
import sys


def main() -> int:
    payload = json.load(sys.stdin)
    decision = payload.get("decision", "?")
    command = payload.get("command") or payload.get("tool") or ""
    reason = payload.get("reason") or ""
    message = command or reason
    if len(message) > 180:
        message = message[:177].rstrip() + "..."

    subprocess.run(
        [
            "osascript",
            "-e",
            f"display notification {json.dumps(message)} with title {json.dumps(f'bouncer: {decision}')}",
        ],
        check=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
