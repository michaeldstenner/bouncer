import json
import sys

from ..classify import run_classify
from ..tool_catalog import record_tool_observation


def _infer_harness(hook_input: dict, fmt: str) -> str:
    """Which harness is on the other end of this hook.

    Codex and opencode both stamp `harness` into the payload themselves. Claude
    Code does not, so it is identified by what only it sends: the `json` hook
    format plus a native `PreToolUse` event. Naming it matters beyond the tool
    catalog — `json` covers both Claude Code and opencode, and those two
    abstain into very different places (see profile.HARNESS_ABSTAIN_FLOOR).
    """
    explicit = hook_input.get("harness") or hook_input.get("harness_name")
    if explicit:
        return explicit
    if fmt in ("codex-permission", "codex-pretool"):
        return "codex"
    if fmt == "plain":
        return "shim"
    if fmt == "json" and hook_input.get("hook_event_name") == "PreToolUse":
        return "claude_code"
    return "unknown"


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
    harness    = _infer_harness(hook_input, fmt)
    # Claude Code stamps the session's permission mode into the payload. It
    # decides whether an abstain reaches auto-mode's classifier or a human,
    # so `solo` needs it. Absent for every other harness, and treated as
    # unknown — which denies rather than abstains.
    perm_mode  = hook_input.get("permission_mode")
    if not isinstance(perm_mode, str) or not perm_mode:
        perm_mode = None

    record_tool_observation(harness, tool_name)

    run_classify(tool_name, tool_input, cwd, session_id, fmt, harness,
                 perm_mode)
