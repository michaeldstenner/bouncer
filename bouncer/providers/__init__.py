import json
from pathlib import Path

from ..config import _build_policy_context, USER_SYSTEM_PROMPT

DEFAULT_SYSTEM_PROMPT = """\
You are a permission scope enforcer for an AI coding assistant running on a personal Mac.

Your job is NOT to decide if a command is generically safe. Your job is to decide
if it falls WITHIN the scope of what the user has already explicitly approved.

Classify the tool call as exactly one of:
ALLOW  - within the approved permission envelope, OR a clearly safe read/inspect/test
         operation with no destructive side effects
DENY   - outside the approved envelope; destructive; irreversibly modifies live system
         state; exfiltrates data; runs untrusted remote code; touches paths not covered
         by approved rules
UNSURE - genuinely cannot determine from context

Always DENY regardless of envelope:
- rm -rf on directories outside the project
- force-pushing git branches
- writing to /etc/, /usr/, /System/, or other system paths
- curl/wget piped directly to bash/sh

Respond with EXACTLY this format, nothing else:
DECISION: <ALLOW|DENY|UNSURE>
REASON: <one sentence>"""


def _load_system_prompt() -> str:
    if USER_SYSTEM_PROMPT.exists():
        return USER_SYSTEM_PROMPT.read_text(encoding="utf-8").strip()
    return DEFAULT_SYSTEM_PROMPT


def _build_prompt(tool_name: str, tool_input: dict, cwd: Path, config: dict) -> tuple[str, str]:
    system_prompt  = _load_system_prompt()
    policy_context = _build_policy_context(cwd, config)
    command        = tool_input.get("command", "")
    tool_desc      = (f"Tool: {tool_name}\nCommand: {command}"
                      if command
                      else f"Tool: {tool_name}\nInput: {json.dumps(tool_input)[:400]}")
    system_text = "\n\n---\n\n".join([system_prompt, "Policy context:\n" + policy_context])
    return system_text, tool_desc


def _parse_llm_text(response_text: str) -> tuple[str, str]:
    decision, reason = "UNSURE", "No reason provided"
    for line in response_text.splitlines():
        upper = line.upper()
        if upper.startswith("DECISION:"):
            val = upper.split(":", 1)[1].strip()
            if val in ("ALLOW", "DENY", "UNSURE"):
                decision = val
        elif upper.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    return decision, reason


def call_llm(
    tool_name: str,
    tool_input: dict,
    cwd: Path,
    config: dict,
) -> tuple[str | None, str]:
    """Classify a tool call. Returns (decision, reason).

    decision is ALLOW / DENY / UNSURE, or None if the backend was unreachable.
    """
    provider = config.get("llm", {}).get("provider", "ollama")
    if provider == "ollama":
        from .ollama import call_ollama
        return call_ollama(tool_name, tool_input, cwd, config)
    if provider in ("openai", "openai_compatible"):
        from .openai import call_openai
        return call_openai(tool_name, tool_input, cwd, config)
    if provider == "anthropic":
        from .anthropic import call_anthropic
        return call_anthropic(tool_name, tool_input, cwd, config)
    return None, f"Unknown LLM provider: {provider!r}"
