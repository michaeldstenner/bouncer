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
    "tools": ["Bash"],
    "policy_mode": "append",
    "activity_width": 10,
    "llm": {
        "provider": "ollama",
        "url": "http://localhost:11434",
        "timeout": 25,
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
# Commented-out settings inherit from user-level defaults.

enabled: true

# Tools to intercept. Use a list of tool names, or the string "all".
tools:
  - Bash

# How this project's policy.md combines with user-level policy.md:
#   append  (default): project policy appended after user policy
#   replace          : project policy fully replaces user policy
policy_mode: append

# LLM backend — uncomment to override user defaults
# provider: ollama | openai | openai_compatible | anthropic
#llm:
#  provider: ollama
#  model: qwen3:32b              # required — no default; set in ~/.config/bouncer/config.yaml
#  url: http://localhost:11434   # ollama / openai_compatible base URL
#  timeout: 25
#  api_key: ...                  # openai / anthropic (or use env var)

# Fallback behavior when LLM is uncertain or unreachable (ask | allow | deny)
#on_unsure: ask
#on_unavailable: ask

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

USER_CONFIG_YAML_TEMPLATE = """\
# bouncer user config — applies to all projects

enabled: true

# Tools to intercept. Use a list of tool names, or the string "all".
tools:
  - Bash

# LLM backend
# provider: ollama | openai | openai_compatible | anthropic
llm:
  provider: ollama
  model: qwen3:32b              # required — no default; pick a model you have installed
  url: http://localhost:11434   # ollama / openai_compatible base URL
  timeout: 25
  # api_key: ...                # openai / anthropic (or OPENAI_API_KEY / ANTHROPIC_API_KEY env var)

# Fallback behavior when LLM is uncertain or unreachable (ask | allow | deny)
on_unsure: ask
on_unavailable: ask

# Logging
log:
  verbosity: all
  max_entries: 10000
  llm_debug: false
"""

USER_POLICY_MD_TEMPLATE = """\
# User Policy

Global context for the LLM classifier:
- Working on a personal Mac as a developer
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
    policy_mode = config.get("policy_mode", "append")
    if policy_mode == "replace" and proj_policy:
        return proj_policy
    parts = [p for p in (user_policy, proj_policy) if p]
    return "\n\n".join(parts) if parts else "(no policy configured)"


def project_log_file(cwd: Path | None = None) -> Path | None:
    d = _find_bouncer_dir(cwd)
    return (d / "log.jsonl") if d else None
