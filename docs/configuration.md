# Configuration

## `config.yaml`

```yaml
# Master on/off switch — disables bouncer without removing config
enabled: true

# Tools to intercept. List specific tool names, or the string "all".
tools:
  - Bash

# How this project's policy.md combines with user-level policy.md:
#   append  (default): project policy appended after user policy
#   replace          : project policy fully replaces user policy
policy_mode: append

# LLM backend
# provider: ollama | openai | openai_compatible | anthropic
llm:
  provider: ollama
  model: qwen2.5:14b
  url: http://localhost:11434   # ollama / openai_compatible base URL
  timeout: 25                   # seconds
  # api_key: ...                # openai / anthropic (or use env var)

# Fallback behavior when the LLM is uncertain or unreachable
on_unsure: ask              # ask | allow | deny | deny_with_message
on_unavailable: ask         # ask | allow | deny | deny_with_message

# Width of the activity strip (number of recent decisions to keep)
activity_width: 10

# Logging
log:
  verbosity: all            # all | deny_only | off
  max_entries: 10000        # prune log when it exceeds this many entries
```

All settings are optional at the project level; unset keys inherit from user
config or built-in defaults.

**Edit:** `bouncer config` opens `.bouncer/config.yaml` in `$EDITOR`.

## LLM providers

| `provider` | Default model | Notes |
|---|---|---|
| `ollama` | `qwen2.5:14b` | Local; model kept alive 60 min to avoid cold-start latency |
| `openai` | `gpt-4o-mini` | Requires `OPENAI_API_KEY` env var or `api_key:` in config |
| `openai_compatible` | `gpt-4o-mini` | Same as `openai`; set `url:` for Groq, LM Studio, Together, etc. |
| `anthropic` | `claude-haiku-4-5-20251001` | Requires `ANTHROPIC_API_KEY` env var or `api_key:` in config |

```yaml
# Ollama (default — local, no API key)
llm:
  provider: ollama
  model: qwen2.5:14b
  url: http://localhost:11434

# OpenAI
llm:
  provider: openai
  model: gpt-4o-mini
  # api_key: sk-...   # or export OPENAI_API_KEY

# Anthropic
llm:
  provider: anthropic
  model: claude-haiku-4-5-20251001
  # api_key: sk-ant-...   # or export ANTHROPIC_API_KEY

# OpenAI-compatible (Groq example)
llm:
  provider: openai_compatible
  model: llama-3.1-8b-instant
  url: https://api.groq.com/openai
  # api_key: gsk_...   # or export OPENAI_API_KEY
```

## `tools`

| Value | Meaning |
|---|---|
| `["Bash"]` | Only intercept Bash (default) |
| `["Bash", "Write"]` | Intercept Bash and Write |
| `all` | Intercept every tool |
| `[]` | Intercept nothing (bouncer inactive but config preserved) |

## `on_unsure` / `on_unavailable`

| Value | Meaning |
|---|---|
| `ask` | Request human approval if ASK is available; otherwise delivered outward as a deny (default) |
| `allow` | Pass through silently |
| `deny` | Block with the LLM's reason; ASK-capable harnesses include an `# ESCALATE:` hint, while no-ASK harnesses tell the agent to find another way or suggest a policy change |

## Config merge order

Later entries override earlier ones:

1. Built-in defaults
2. `~/.config/bouncer/config.yaml` (user-level)
3. `.bouncer/config.yaml` (project-level)
4. `.bouncer/config.local.yaml` (machine-local overrides, gitignored)

## Custom system prompt

Place a custom LLM system prompt at `~/.config/bouncer/system_prompt.txt`.
If absent, the built-in default is used — it instructs the LLM to classify as
ALLOW/DENY/UNSURE and hardcodes a short list of absolute-deny patterns
(`rm -rf` outside project, force-push, writes to `/etc/`, curl-to-shell).
