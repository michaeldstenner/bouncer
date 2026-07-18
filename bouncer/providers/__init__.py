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

Applying policy rules:
- Judge a read-only/inspect command (viewing or listing, not modifying) on its
  own. A policy rule against editing, modifying, or "touching" a path does not
  by itself forbid reading it. Honor explicit read prohibitions; do not invent
  them from edit rules.
- Apply each prohibition to the specific path AND action it names. A keyword in
  a path does not extend a rule to every path that merely contains that word —
  e.g. a source directory `bouncer/` is not the config directory `.bouncer/`,
  and "never edit X" still permits reading X.

Respond with EXACTLY this format, nothing else:
DECISION: <ALLOW|DENY|UNSURE>
REASON: <one sentence>"""


def _load_system_prompt() -> str:
    if USER_SYSTEM_PROMPT.exists():
        return USER_SYSTEM_PROMPT.read_text(encoding="utf-8").strip()
    return DEFAULT_SYSTEM_PROMPT


_INPUT_CHAR_BUDGET = 2000


def _render_tool_input(tool_input: dict) -> str:
    file_path = tool_input.get("file_path")
    if file_path is None:
        rest = tool_input
    else:
        rest = {k: v for k, v in tool_input.items() if k != "file_path"}

    rest_json = json.dumps(rest)
    if len(rest_json) > _INPUT_CHAR_BUDGET:
        overflow = len(rest_json) - _INPUT_CHAR_BUDGET
        rest_json = f"{rest_json[:_INPUT_CHAR_BUDGET]}…[+{overflow} chars truncated]"

    if file_path is None:
        return rest_json
    return f"file_path: {file_path}\nInput: {rest_json}"


def _build_prompt(tool_name: str, tool_input: dict, cwd: Path, config: dict) -> tuple[str, str]:
    system_prompt  = _load_system_prompt()
    policy_context = _build_policy_context(cwd, config)
    command        = tool_input.get("command", "")
    tool_desc      = (f"Tool: {tool_name}\nCommand: {command}"
                      if command
                      else f"Tool: {tool_name}\n{_render_tool_input(tool_input)}")
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
    llm_cfg,
    queue_snapshot: list[dict] | None = None,
) -> str:
    def cfg_get(key, default=None):
        if isinstance(llm_cfg, dict):
            return llm_cfg.get(key, default)
        return getattr(llm_cfg, key, default)

    timeout    = int(cfg_get("timeout", 30))
    ftt        = cfg_get("first_token_timeout", 5)
    gt         = cfg_get("generation_timeout") or timeout
    qt         = cfg_get("queue_timeout")        # None if not configured
    qst        = cfg_get("queue_stall_timeout")  # None if not configured
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


def _classifier_extra(provider: str, llm_cfg: dict) -> dict:
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
    return extra


def _merged_fallback_cfg(base: dict, fallback: dict) -> dict:
    merged = dict(base)
    merged.pop("fallbacks", None)
    merged.pop("fallback_on", None)

    base_provider = merged.get("provider", "ollama")
    fallback_provider = fallback.get("provider", base_provider)
    if fallback_provider != base_provider:
        if "url" not in fallback:
            merged["url"] = ""
        if "api_key" not in fallback:
            merged["api_key"] = ""

    merged.update(fallback)
    return merged


def _make_llm_config(llm_cfg: dict):
    from ..llmclient import LLMConfig

    provider = llm_cfg.get("provider", "ollama")
    model    = llm_cfg.get("model")
    if not model:
        raise ValueError(
            "No LLM model configured — set llm.model in ~/.config/bouncer/config.yaml"
        )

    _fail_fast_triggers = (
        "timeout:queue_stall",
        "timeout:first_token",
        "error:unreachable",
    )
    raw_triggers = llm_cfg.get("circuit_triggers")
    circuit_triggers = (
        tuple(raw_triggers) if raw_triggers is not None else _fail_fast_triggers
    )
    configured_key = llm_cfg.get("api_key", "")
    key_kwargs = {"api" + "_key": configured_key}

    return LLMConfig(
        provider=provider,
        model=model,
        url=llm_cfg.get("url", ""),
        timeout=int(llm_cfg.get("timeout", 30)),
        **key_kwargs,
        keep_alive=llm_cfg.get("keep_alive", "60m"),
        queue_mode="cooperative" if provider == "ollama" else "off",
        queue_timeout=llm_cfg.get("queue_timeout"),
        queue_stall_timeout=llm_cfg.get("queue_stall_timeout"),
        priority=int(llm_cfg.get("priority", 80)),
        caller_max=int(llm_cfg.get("caller_max", 4)),
        first_token_timeout=llm_cfg.get("first_token_timeout"),
        generation_timeout=llm_cfg.get("generation_timeout"),
        circuit_n=int(llm_cfg.get("circuit_n", 2)),
        circuit_cooldown_s=float(llm_cfg.get("circuit_cooldown_s", 120.0)),
        circuit_triggers=circuit_triggers,
        circuit_key=f"bouncer|{provider}|{model}|{llm_cfg.get('url', '')}",
        circuit_mode=llm_cfg.get("circuit_mode", "count"),
        grace_s=float(llm_cfg.get("grace_s", 0.0)),
        deadline_s=llm_cfg.get("deadline_s"),
        ps_probe=bool(llm_cfg.get("ps_probe", False)),
        ps_url=llm_cfg.get("ps_url", ""),
        log_caller="bouncer",
        extra_params=_classifier_extra(provider, llm_cfg),
    )


def _build_llm_configs(llm_cfg: dict) -> list:
    configs = [_make_llm_config(llm_cfg)]
    for fallback in llm_cfg.get("fallbacks", []) or []:
        configs.append(_make_llm_config(_merged_fallback_cfg(llm_cfg, fallback)))
    return configs


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
    from ..llmclient import FallbackLLMClient, LLMClient
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

    configs = _build_llm_configs(llm_cfg)
    fallback_on = llm_cfg.get("fallback_on")
    client = (
        FallbackLLMClient(configs, abort_event=ABORT_EVENT,
                          fallback_on=fallback_on)
        if len(configs) > 1 else
        LLMClient(configs[0], abort_event=ABORT_EVENT)
    )
    result  = client.call(user_text, system=system_text)
    attempts = getattr(client, "last_attempts", ()) or []
    final_cfg = attempts[-1].cfg if attempts else configs[0]
    provider = final_cfg.provider
    model    = final_cfg.model
    attempt_summary = [
        {"provider": a.cfg.provider, "model": a.cfg.model,
         "outcome": a.result.outcome}
        for a in attempts
    ]

    log_llm_debug(
        str(cwd), config, provider, model,
        {"prompt_chars": result.prompt_chars, "tokens": result.prompt_tokens,
         "fallback_attempts": attempt_summary},
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
            _outcome_to_error(result.outcome, provider, final_cfg,
                              result.queue_snapshot),
            result.prompt_chars,
            result.queue_snapshot,
        )

    decision, reason = _parse_llm_text(result.text or "")
    return decision, reason, result.prompt_chars, None
