import json
from datetime import datetime

from . import config as _config


DOCUMENTED_TOOL_CATALOG = {
    "claude_code": {
        "tools": [
            "Bash",
            "Read",
            "Write",
            "Edit",
            "MultiEdit",
            "Glob",
            "Grep",
            "Task",
            "WebFetch",
            "WebSearch",
            "TodoWrite",
            "NotebookRead",
            "NotebookEdit",
        ],
        "notes": [
            "MCP tools may be observed as names like mcp__server__tool.",
            "Tool availability depends on the active Claude Code configuration.",
        ],
    },
    "codex": {
        "tools": ["Bash"],
        "notes": [
            "The recommended PermissionRequest integration currently gates Bash approval requests.",
            "Other Codex tools are not exposed through bouncer's recommended integration.",
        ],
    },
    "opencode": {
        "tools": ["bash", "read", "edit", "write", "apply_patch"],
        "notes": [
            "Names come from opencode's tool.execute.before hook.",
            "MCP tools may appear if opencode routes them through the same hook; record the observed name if so.",
        ],
    },
    "shim": {
        "tools": ["Bash"],
        "notes": [
            "The shell shim only gates bash -c invocations.",
        ],
    },
}


def _read_observed_catalog() -> dict:
    try:
        return json.loads(tool_catalog_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "harnesses": {}}


def _write_observed_catalog(data: dict) -> None:
    try:
        path = tool_catalog_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def record_tool_observation(harness: str, tool_name: str) -> None:
    harness = (harness or "unknown").strip().replace("-", "_") or "unknown"
    tool_name = (tool_name or "unknown").strip() or "unknown"
    now = datetime.now().isoformat()

    data = _read_observed_catalog()
    data.setdefault("version", 1)
    harnesses = data.setdefault("harnesses", {})
    entry = harnesses.setdefault(harness, {})
    tools = entry.setdefault("tools", {})
    tool = tools.setdefault(tool_name, {"count": 0, "first_seen": now})
    tool["count"] = int(tool.get("count", 0)) + 1
    tool["last_seen"] = now
    _write_observed_catalog(data)


def tool_catalog_file():
    return _config.USER_LOG_FILE.with_name("tools.json")


def merged_tool_catalog() -> dict:
    merged = {
        harness: {
            "documented": list(info.get("tools", [])),
            "observed": [],
            "tools": list(info.get("tools", [])),
            "notes": list(info.get("notes", [])),
        }
        for harness, info in DOCUMENTED_TOOL_CATALOG.items()
    }

    observed = _read_observed_catalog().get("harnesses", {})
    for harness, info in observed.items():
        target = merged.setdefault(
            harness,
            {"documented": [], "observed": [], "tools": [], "notes": []},
        )
        for tool_name in info.get("tools", {}):
            if tool_name not in target["observed"]:
                target["observed"].append(tool_name)
            if tool_name not in target["tools"]:
                target["tools"].append(tool_name)
        target.setdefault("notes", []).append(f"Local observations from {tool_catalog_file()}")

    return merged
