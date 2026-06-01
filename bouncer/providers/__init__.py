import json
from pathlib import Path

from ..config import _build_policy_context, USER_SYSTEM_PROMPT

DEFAULT_SYSTEM_PROMPT = """\
You are a permission scope enforcer for an AI coding assistant running on a developer's personal workstation.

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


def _outcome_to_error(
    outcome: str,
    provider: str,
    llm_cfg: dict,
    queue_snapshot: list[dict] | None = None,
) -> str:
    timeout    = int(llm_cfg.get("timeout", 30))
    ftt        = llm_cfg.get("first_token_timeout", 5)
    gt         = llm_cfg.get("generation_timeout") or timeout
    qt         = llm_cfg.get("queue_timeout")        # None if not configured
    qst        = llm_cfg.get("queue_stall_timeout")  # None if not configured
    label      = "Ollama" if provider == "ollama" else provider.capitalize()
    if outcome == "aborted":
        return "user aborted"
    if outcome == "circuit_open":
        return "LLM circuit open — skipping after repeated timeouts"
    if outcome == "timeout:queue_wait":
        if qt is not None:
            return f"Ollama queue full — no slot within {qt}s"
        return "Ollama queue full — no slot acquired"
    if outcome == "timeout:queue_stall":
        if qst is not None:
            return f"Ollama queue stalled — no completion within {qst}s"
        return "Ollama queue stalled — no inference progress detected"
    if outcome == "timeout:first_token":
        return f"Ollama busy — no response start within {ftt}s"
    if outcome == "timeout:generation":
        return f"Ollama slow — inference exceeded {gt}s"
    # legacy outcomes
    if outcome == "timeout:model_loaded_but_slow":
        return (f"Ollama busy: model loaded but did not respond within "
                f"{timeout}s")
    if outcome == "timeout:model_not_loaded":
        return "Ollama timed out and model is not loaded"
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
) -> tuple[str | None, str, int | None, list[dict] | None]:
    """Classify a tool call. Returns (decision, reason, prompt_chars, queue_snapshot).

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
        return ("LLM_ERROR",
                "No LLM model configured — set llm.model in ~/.config/bouncer/config.yaml",
                None, None)

    system_text, user_text = _build_prompt(tool_name, tool_input, cwd, config)

    # Ollama uses num_predict and does not need a large completion budget for
    # the strict two-line classifier response. OpenAI-compatible reasoning
    # models may spend hidden tokens before emitting final text, so keep their
    # default max_tokens high enough to avoid empty completions.
    extra: dict = {"num_predict": 80}
    if provider in ("openai", "openai_compatible"):
        extra["max_tokens"] = 1024
    else:
        extra["max_tokens"] = 80
    extra.update(llm_cfg.get("extra_params", {}) or {})
    if llm_cfg.get("num_ctx"):
        extra["num_ctx"] = llm_cfg["num_ctx"]

    _fail_fast_triggers = (
        "timeout:queue_wait",
        "timeout:queue_stall",
        "timeout:first_token",
        "error:unreachable",
    )
    raw_triggers = llm_cfg.get("circuit_triggers")
    circuit_triggers = (
        tuple(raw_triggers) if raw_triggers is not None else _fail_fast_triggers
    )

    cfg = LLMConfig(
        provider=provider,
        model=model,
        url=llm_cfg.get("url", ""),
        timeout=int(llm_cfg.get("timeout", 30)),
        api_key=llm_cfg.get("api_key", ""),
        keep_alive=llm_cfg.get("keep_alive", "60m"),
        queue_mode="cooperative" if provider == "ollama" else "off",
        queue_timeout=llm_cfg.get("queue_timeout"),
        queue_stall_timeout=llm_cfg.get("queue_stall_timeout"),
        priority=int(llm_cfg.get("priority", 80)),
        caller_max=int(llm_cfg.get("caller_max", 4)),
        first_token_timeout=llm_cfg.get("first_token_timeout"),
        generation_timeout=llm_cfg.get("generation_timeout"),
        circuit_n=int(llm_cfg.get("circuit_n", 2)),
        circuit_key="|".join((
            "bouncer",
            provider,
            str(model),
            str(llm_cfg.get("url", "")),
        )),
        circuit_cooldown_s=float(llm_cfg.get("circuit_cooldown_s", 120.0)),
        circuit_triggers=circuit_triggers,
        log_caller="bouncer",
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
        queue_snapshot=result.queue_snapshot,
    )

    if result.outcome != "success":
        is_timeout = (result.outcome.startswith("timeout") or
                      result.outcome == "circuit_open")
        # TIMEOUT and LLM_ERROR are both "couldn't get a verdict" outcomes that
        # resolve via on_unavailable; the distinct labels exist so the log and
        # activity strip show *what* went wrong (slow vs. unreachable/auth/etc).
        return (
            "TIMEOUT" if is_timeout else "LLM_ERROR",
            _outcome_to_error(result.outcome, provider, llm_cfg,
                              result.queue_snapshot),
            result.prompt_chars,
            result.queue_snapshot,
        )

    decision, reason = _parse_llm_text(result.text or "")
    return decision, reason, result.prompt_chars, None
