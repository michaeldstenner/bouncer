# Configuration

## `config.yaml`

All settings are optional at the project level. Unset keys inherit from
user config (`~/.config/bouncer/config.yaml`) or built-in defaults.

```yaml
# Master on/off switch — disables bouncer without removing config
#enabled: true

# Tools to intercept. List specific tool names, or the string "all".
# NOTE: setting this at the project level REPLACES the user-level list
# entirely — it does not append to it.
#tools:
#  - Bash

# How this project's policy.md combines with user-level policy.md:
#   append  (default): project policy appended after user policy
#   replace          : project policy fully replaces user policy
#policy_mode: append

# LLM backend
# provider: ollama | openai | openai_compatible | anthropic
llm:
  provider: ollama
  model: qwen3:32b              # required — no built-in default; set in ~/.config/bouncer/config.yaml
  url: http://localhost:11434   # ollama / openai_compatible base URL
  timeout: 25                   # seconds
  # api_key: ...                # openai / anthropic (or use env var)
  # extra_params:               # optional provider-specific request params
  #   max_tokens: 1000

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


**Edit:** `bouncer config` opens `.bouncer/config.yaml` in `$EDITOR`.
`bouncer config -e` / `-d` enable or disable bouncer without opening the editor.

## LLM providers

`model` is required — there is no built-in default. Set it in `~/.config/bouncer/config.yaml`
so all projects inherit it, then override per-project as needed.

| `provider` | Notes |
|---|---|
| `ollama` | Local; model kept alive 60 min to avoid cold-start latency |
| `openai` | Requires `OPENAI_API_KEY` env var or `api_key:` in config |
| `openai_compatible` | Same as `openai`; set `url:` for Groq, LM Studio, Together, etc. |
| `anthropic` | Requires `ANTHROPIC_API_KEY` env var or `api_key:` in config |

API keys and URLs can also be stored in `~/.config/bouncer/keys.yaml`
(preferred) or `~/.config/llmclient/keys.yaml` (shared with other
llmclient-based tools). The bouncer file takes precedence.

```yaml
# ~/.config/bouncer/keys.yaml
openai:
  api_key: sk-...
anthropic:
  api_key: sk-ant-...
ollama:
  url: http://localhost:11434
  parallel_slots: 4
```

```yaml
# Ollama (default — local, no API key)
llm:
  provider: ollama
  model: qwen3:32b
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
  # api_key: <groq-api-key>   # or export OPENAI_API_KEY
  # extra_params:
  #   max_tokens: 1000
```

## `tools`

| Value | Meaning |
|---|---|
| `["Bash"]` | Only intercept Bash (default) |
| `["Bash", "Write"]` | Intercept Bash and Write |
| `all` | Intercept every tool |
| `[]` | Intercept nothing (bouncer inactive but config preserved) |

**Replacement semantics:** setting `tools` at the project level replaces
the user-level list entirely. There is no append/merge — whatever you
write at the project level is the complete list for that project.

## `on_unsure` / `on_unavailable`

| Value | Meaning |
|---|---|
| `ask` | Request human approval if ASK is available; otherwise delivered outward as a deny or pass-through depending on the integration (default) |
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
