import sys

_TTY = sys.stdout.isatty()


def _c(code: str) -> str:
    return code if _TTY else ""


RESET   = _c("\033[0m")
BOLD    = _c("\033[1m")
DIM     = _c("\033[2m")
GREEN   = _c("\033[32m")
RED     = _c("\033[31m")
YELLOW  = _c("\033[33m")
CYAN    = _c("\033[36m")
WHITE   = _c("\033[37m")
MAGENTA = _c("\033[35m")

DECISION_COLORS = {
    "ALLOW":    GREEN,
    "DENY":     RED,
    "BLOCK":    RED,
    "UNSURE":   MAGENTA,
    "TIMEOUT":  _c("\033[30;45m"),
    "ESCALATE": CYAN,
    "PENDING":  DIM,
}
