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
    text = response_text.strip()
    if not text:
        return "UNSURE", "LLM output does not match expected format (empty response)"

    decision, reason = None, None
    for line in text.splitlines():
        upper = line.upper()
        if upper.startswith("DECISION:"):
            val = upper.split(":", 1)[1].strip()
            if val in ("ALLOW", "DENY", "UNSURE"):
                decision = val
        elif upper.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()

    if decision is None or reason is None:
        return "UNSURE", "LLM output does not match expected format"

    return decision, reason


def _outcome_to_error(outcome: str, provider: str, llm_cfg: dict) -> str:
    timeout = int(llm_cfg.get("timeout", 25))
    label = "Ollama" if provider == "ollama" else provider.capitalize()
    if outcome == "aborted":
        return "user aborted"
    if outcome == "timeout:model_loaded_but_slow":
        return (f"Ollama busy: model loaded but did not respond within "
                f"{timeout}s — consider increasing llm.timeout")
    if outcome == "timeout:model_not_loaded":
        return ("Ollama timed out and model is not loaded — "
                "Ollama may be overloaded or restarting")
    if outcome == "timeout":
        return f"{label} timed out after {timeout}s"
    if outcome == "error:unreachable":
        return f"{label} unavailable"
    if outcome.startswith("http_"):
        return f"Classifier HTTP error {outcome[5:]}"
    if outcome.startswith("error:"):
        return f"Classifier error: {outcome[6:]}"
    return f"LLM error: {outcome}"


def call_llm(
    tool_name: str,
    tool_input: dict,
    cwd: Path,
    config: dict,
) -> tuple[str | None, str, int | None]:
    """Classify a tool call. Returns (decision, reason, prompt_chars).

    decision is ALLOW / DENY / UNSURE, or None if the backend was unreachable.
    prompt_chars is the combined length of system+user prompt text, or None on
    failure paths where the prompt was never built.
    """
    from ..llmclient import LLMClient, LLMConfig
    from .._abort import ABORT_EVENT
    from ..log import log_llm_debug

    llm_cfg  = config.get("llm", {})
    provider = llm_cfg.get("provider", "ollama")
    model    = llm_cfg.get("model")
    if not model:
        return None, "No LLM model configured — set llm.model in ~/.config/bouncer/config.yaml", None

    system_text, user_text = _build_prompt(tool_name, tool_input, cwd, config)

    extra: dict = {"max_tokens": 80, "num_predict": 80}
    extra.update(llm_cfg.get("extra_params", {}) or {})
    if llm_cfg.get("num_ctx"):
        extra["num_ctx"] = llm_cfg["num_ctx"]

    cfg = LLMConfig(
        provider=provider,
        model=model,
        url=llm_cfg.get("url", ""),
        timeout=int(llm_cfg.get("timeout", 25)),
        api_key=llm_cfg.get("api_key", ""),
        keep_alive=llm_cfg.get("keep_alive", "60m"),
        queue_mode="cooperative" if provider == "ollama" else "off",
        extra_params=extra,
    )
    client  = LLMClient(cfg, abort_event=ABORT_EVENT)
    result  = client.call(user_text, system=system_text)

    log_llm_debug(
        str(cwd), config, provider, model,
        {"prompt_chars": result.prompt_chars, "tokens": result.prompt_tokens},
        response_text=result.text,
        error=None if result.outcome == "success" else result.outcome,
        elapsed_s=result.call_s,
    )

    if result.outcome != "success":
        return None, _outcome_to_error(result.outcome, provider, llm_cfg), result.prompt_chars

    decision, reason = _parse_llm_text(result.text or "")
    return decision, reason, result.prompt_chars
