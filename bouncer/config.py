from pathlib import Path
from .yaml import MicroYAML

HOME               = Path.home()
USER_CONFIG_DIR    = HOME / ".config" / "bouncer"
USER_CONFIG_FILE   = USER_CONFIG_DIR / "config.yaml"
USER_POLICY_FILE   = USER_CONFIG_DIR / "policy.md"
USER_SYSTEM_PROMPT = USER_CONFIG_DIR / "system_prompt.txt"
USER_LOG_FILE      = HOME / ".local" / "share" / "bouncer" / "log.jsonl"
ACTIVITY_DIR       = HOME / ".local" / "share" / "bouncer" / "activity"
PROJECT_DIR_NAME   = ".bouncer"

CONFIG_DEFAULTS: dict = {
    "enabled": True,
    "tools": "all",
    "policy_mode": "append",
    "activity_width": 10,
    "activity": {
        "colors": {
            "ALLOW": "green",
            "DENY": "red",
            "BLOCK": "red",
            "UNSURE": "magenta",
            "TIMEOUT": "black_on_magenta_bold",
            "LLM_ERROR": "black_on_red_bold",
            "ESCALATE": "cyan",
        },
    },
    "notify": {},
    "llm": {
        "provider":           "ollama",
        "url":                "http://localhost:11434",
        "timeout":            30,
        "queue_timeout":      8,
        "first_token_timeout": 30,
        "generation_timeout": 30,
        "circuit_n":          2,
        "circuit_cooldown_s": 120,
        "circuit_mode":       "futility",
        "grace_s":            8,
        "deadline_s":         90,
        "ps_probe":           True,
    },
    "on_unsure":      "ask",
    "on_unavailable": "ask",
    "log": {
        "verbosity":   "all",
        "max_entries": 10000,
        "llm_debug":   False,
    },
}

CONFIG_YAML_TEMPLATE = """\
# bouncer project config
# All settings are optional — uncomment to override user-level defaults.

# Enable or disable bouncer for this project.
#enabled: true

# Tools to intercept (default: all).
# NOTE: this REPLACES the user-level list entirely (no merging).
# Use the string "all", or a list of tool names.
#tools:
#  - Bash

# How this project's policy.md combines with user-level policy.md:
#   append  (default): project policy appended after user policy
#   replace          : project policy fully replaces user policy
#policy_mode: append

# LLM backend — uncomment to override user defaults
# provider: ollama | openai | openai_compatible | anthropic
#llm:
#  provider: ollama
#  model: qwen3:32b              # required — no default
#  url: http://localhost:11434   # ollama / openai_compatible base URL
#  timeout: 30                   # fallback / non-streaming timeout (s)
#  queue_timeout: 8              # max seconds to wait for an ollama slot
#  first_token_timeout: 5        # max seconds until ollama starts responding
#  generation_timeout: 30        # max seconds for full inference
#  circuit_n: 2                  # (count mode) trip after N consecutive failures
#  circuit_cooldown_s: 120       # seconds open before probe attempt
#  circuit_mode: futility        # futility | count (futility = leaky-LLR breaker)
#  grace_s: 8                    # min wait before stall bail may fire
#  deadline_s: 90                # pencils-down ceiling (null = no hard ceiling)
#  ps_probe: true                # cheap /api/ps liveness probe (ollama only)
#  api_key: ...                  # openai / anthropic (or use env var)
#  extra_params:                 # optional provider-specific request params
#    max_tokens: 1000

# Fallback when the LLM is uncertain (on_unsure) or unreachable (on_unavailable):
#   ask      prompt the user (default)
#   allow    let the call through
#   deny     block the call
#   abstain  no opinion — defer to the harness's own permission flow (e.g.
#            Claude Code auto-mode, or its normal prompt). NOT the same as
#            allow: risky calls still hit the harness's own gate.
#on_unsure: ask
#on_unavailable: ask

# Activity/statusline indicator
#activity_width: 10
#activity:
#  colors:
#    ALLOW: green
#    DENY: red
#    UNSURE: magenta
#    ESCALATE: cyan

# Optional post-decision notifier. Bouncer starts the command, writes one JSON
# object to stdin, closes stdin, and does not wait for it to finish.
#notify:
#  command: ~/bin/bouncer-notify
#  decisions: all       # all, or a list such as [DENY, ESCALATE, TIMEOUT, LLM_ERROR]

# Logging
#log:
#  verbosity: all         # all | deny_only | off
#  max_entries: 10000     # prune log when it exceeds this many entries
#  llm_debug: false       # write redacted LLM request/response JSONL for debugging
"""

POLICY_MD_TEMPLATE = """\
# Project Policy

Describe this project for the LLM classifier:
- What paths are safe to touch?
- What external services are involved?
- What operations are routine vs. risky?
"""

# Sentinel that splits the unified policy editor tempfile into committed
# (policy.md) and local (policy.local.md) sections.
POLICY_LOCAL_SENTINEL = "<!-- bouncer:local -->"

POLICY_LOCAL_MD_TEMPLATE = """\
# Local Policy Additions (gitignored)

Add any local-only context here — paths, credentials, personal workflow
notes, or anything you don't want committed.
"""

USER_CONFIG_YAML_TEMPLATE = """\
# bouncer user config — applies to all projects

enabled: true

# Tools to intercept. Use the string "all", or a list of tool names.
# (bouncer only ever sees calls the harness escalates for approval, so "all"
#  does not mean classifying every read — just every call that needs a decision.)
tools: all

# LLM backend
# provider: ollama | openai | openai_compatible | anthropic
llm:
  provider: ollama
  model: qwen3:32b              # required — no default
  url: http://localhost:11434   # ollama / openai_compatible base URL
  timeout: 30                   # fallback / non-streaming timeout (s)
  queue_timeout: 8              # max seconds to wait for an ollama slot
  first_token_timeout: 5        # max seconds until ollama starts responding
  generation_timeout: 30        # max seconds for full inference
  circuit_n: 2                  # (count mode) trip after N consecutive failures
  circuit_cooldown_s: 120       # seconds open before probe attempt
  circuit_mode: futility        # futility | count (futility = leaky-LLR breaker)
  grace_s: 8                    # min wait before stall bail may fire
  deadline_s: 90                # pencils-down ceiling (null = no hard ceiling)
  ps_probe: true                # cheap /api/ps liveness probe (ollama only)
  # api_key: ...                # openai / anthropic (or env var)
  # extra_params:               # optional provider-specific request params
  #   max_tokens: 1000

# Fallback when the LLM is uncertain (on_unsure) or unreachable (on_unavailable):
#   ask      prompt the user (default)
#   allow    let the call through
#   deny     block the call
#   abstain  no opinion — defer to the harness's own permission flow (e.g.
#            Claude Code auto-mode, or its normal prompt). NOT the same as
#            allow: risky calls still hit the harness's own gate.
on_unsure: ask
on_unavailable: ask

# Activity/statusline indicator
activity_width: 10
#activity:
#  colors:
#    ALLOW: green
#    DENY: red
#    UNSURE: magenta
#    ESCALATE: cyan

# Optional post-decision notifier. Bouncer starts the command, writes one JSON
# object to stdin, closes stdin, and does not wait for it to finish.
#notify:
#  command: ~/bin/bouncer-notify
#  decisions: all       # all, or a list such as [DENY, ESCALATE, TIMEOUT, LLM_ERROR]

# Logging
log:
  verbosity: all
  max_entries: 10000
  llm_debug: false
"""

USER_POLICY_MD_TEMPLATE = """\
# User Policy

Global context for the LLM classifier:
- Working as a developer on a personal workstation
- Standard development tools (git, npm, make, python, uv) are generally safe
- Irreversible system changes and external data exfiltration require caution
"""

_YAML = MicroYAML()


def _activity_file(session_id: str) -> Path:
    return ACTIVITY_DIR / f"{session_id}.json"


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_yaml_config(path: Path) -> dict:
    try:
        return _YAML.load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}


def load_policy(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _find_bouncer_dir(cwd: Path | None = None) -> Path | None:
    current = (cwd or Path.cwd()).resolve()
    home = HOME.resolve()
    while True:
        candidate = current / PROJECT_DIR_NAME
        if candidate.is_dir():
            return candidate
        if current == home or current.parent == current:
            break
        current = current.parent
    return None


def project_has_bouncer(cwd: Path | None = None) -> bool:
    d = _find_bouncer_dir(cwd)
    return d is not None and (d / "config.yaml").exists()


def _merged_config(cwd: Path | None = None) -> dict:
    config = dict(CONFIG_DEFAULTS)
    user_cfg = load_yaml_config(USER_CONFIG_FILE)
    if user_cfg:
        config = _deep_merge(config, user_cfg)
    d = _find_bouncer_dir(cwd)
    if d:
        proj_cfg = load_yaml_config(d / "config.yaml")
        if proj_cfg:
            config = _deep_merge(config, proj_cfg)
        local_cfg = load_yaml_config(d / "config.local.yaml")
        if local_cfg:
            config = _deep_merge(config, local_cfg)
    return config


def _build_policy_context(cwd: Path | None = None, config: dict | None = None) -> str:
    if config is None:
        config = _merged_config(cwd)
    user_policy = load_policy(USER_POLICY_FILE)
    d = _find_bouncer_dir(cwd)
    proj_policy = load_policy(d / "policy.md") if d else ""
    proj_local = load_policy(d / "policy.local.md") if d else ""
    policy_mode = config.get("policy_mode", "append")
    if policy_mode == "replace" and (proj_policy or proj_local):
        parts = [p for p in (proj_policy, proj_local) if p]
        return "\n\n".join(parts)
    parts = [p for p in (user_policy, proj_policy, proj_local) if p]
    return "\n\n".join(parts) if parts else "(no policy configured)"


def split_policy_tempfile(text: str) -> tuple[str, str]:
    """Split a unified editor tempfile into (committed, local) policy text."""
    sentinel = POLICY_LOCAL_SENTINEL
    if sentinel in text:
        before, _, after = text.partition(sentinel)
        return before.strip(), after.strip()
    return text.strip(), ""


def build_policy_tempfile(committed: str, local: str) -> str:
    """Build the unified tempfile content from committed and local policy text."""
    committed_block = committed if committed else POLICY_MD_TEMPLATE.strip()
    local_block = local if local else POLICY_LOCAL_MD_TEMPLATE.strip()
    return f"{committed_block}\n\n{POLICY_LOCAL_SENTINEL}\n{local_block}\n"


def project_log_file(cwd: Path | None = None) -> Path | None:
    d = _find_bouncer_dir(cwd)
    return (d / "log.jsonl") if d else None
