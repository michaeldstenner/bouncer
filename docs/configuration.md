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
  timeout: 30                   # overall request timeout (seconds)
  # api_key: ...                # openai / anthropic (or use env var)

  # Ollama queue management (ignored for non-ollama providers)
  # queue_timeout: 30           # max seconds to wait for a queue slot
  # queue_stall_timeout: 15     # bail if nothing has completed within this window
  # priority: 80                # higher = served first (default: 80)
  # caller_max: 4               # max concurrent bouncer calls in the queue

  # Two-phase timeouts (Ollama streaming)
  # first_token_timeout: 8      # seconds to wait for first response token
  # generation_timeout: 30      # seconds for full response after first token

  # Circuit breaker — prevents hammering an unresponsive LLM
  # circuit_n: 2                # consecutive failures before opening circuit
  # circuit_cooldown_s: 60      # seconds before attempting recovery
  # circuit_triggers:           # which outcomes count as failures
  #   - timeout:queue_wait
  #   - timeout:queue_stall
  #   - timeout:first_token
  #   - error:unreachable

  # extra_params:               # optional provider-specific request params
  #   max_tokens: 1000

# Fallback behavior when the LLM is uncertain or unreachable
on_unsure: ask              # ask | allow | deny
on_unavailable: ask         # ask | allow | deny

# Width of the activity strip (number of recent decisions to keep)
activity_width: 10

# Activity/statusline colors. One decision map applies to all display formats;
# bouncer translates each style name to ANSI or tmux syntax as needed.
# Supported styles: green, red, yellow, blue, magenta, cyan, white, black,
# dim, bold, black_on_red_bold, black_on_magenta_bold.
activity:
  colors:
    ALLOW: green
    DENY: red
    UNSURE: magenta
    ESCALATE: cyan

# Optional post-decision notifier. Bouncer starts the command, writes one JSON
# object to stdin, closes stdin, and does not wait for it to finish.
notify:
  command: ~/bin/bouncer-notify
  decisions: all             # all, or a list such as [DENY, ESCALATE, TIMEOUT, LLM_ERROR]

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

API keys and URLs do not have to live in the `llm:` block. When they are not
set there, bouncer resolves them through the vendored llmclient library, which
reads provider-keyed sections from a layered set of files. Resolution order
(first non-empty wins):

1. The `llm.api_key` / `llm.url` you set directly in your bouncer config
2. The standard env var (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`)
3. Provider sections in `~/.config/bouncer/config.yaml` (bouncer points
   llmclient here, so it overlays the global file)
4. Provider sections in `~/.config/llmclient/config.yaml` (shared with other
   llmclient-based tools)
5. `~/.config/llmclient/keys.yaml` (legacy filename, still read)

Provider sections live alongside the rest of your bouncer config and use the
provider name as the top-level key:

```yaml
# ~/.config/bouncer/config.yaml  (same file as your llm:/tools: settings)
openai:
  api_key: sk-...
anthropic:
  api_key: sk-ant-...
ollama:
  url: http://localhost:11434
  parallel_slots: 4
```

> Earlier alpha builds read `~/.config/bouncer/keys.yaml`; that bespoke path is
> gone. The capability now lives in llmclient via `llmclient.configure()`, and
> bouncer points it at `~/.config/bouncer/`. Move any provider sections from
> `keys.yaml` into `config.yaml` (or the global llmclient files above).

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

Tool names are harness-specific. Run this to see bouncer's documented catalog
merged with locally observed hook traffic:

```sh
bouncer tools
bouncer tools --harness=claude_code
```

MCP tools only appear to bouncer when the harness exposes them through its hook
payload. Claude Code MCP tools may be observed with names like
`mcp__server__tool`; other harnesses may differ.

For Codex, the recommended integration sees `PermissionRequest` events. To make
Codex ask for approval more often, and therefore send more Bash requests through
bouncer, start Codex with:

```sh
codex --ask-for-approval untrusted
```

This increases Codex approval traffic; it does not expose Codex-internal tools
that Codex does not include in `PermissionRequest` hook payloads.

## `notify`

`notify.command` is an optional post-decision command. When set, bouncer starts
it after each final decision, writes one JSON object to stdin, closes stdin, and
does not wait for it to finish. It may be a shell command string or an argv
list. Notifier failures, stderr, stdout, and runtime are ignored so
notifications cannot delay or break command approval.

```yaml
notify:
  command: ~/bin/bouncer-notify
  decisions:
    - DENY
    - ESCALATE
    - TIMEOUT
    - LLM_ERROR
```

For quick local experiments, `notify` may also be just a shell command string:

```yaml
notify: "jq -r '.decision + \": \" + (.command // \"\")' >> /tmp/bouncer.notify.log"
```

Payload fields:

| Field | Meaning |
|---|---|
| `version` | Payload schema version, currently `1` |
| `timestamp` | Local ISO timestamp for notification emission |
| `tool` | Harness tool name, such as `Bash` |
| `tool_input` | Original hook tool input |
| `command` | Convenience copy of `tool_input.command` for Bash, otherwise `null` |
| `cwd` | Working directory for the tool request |
| `project.cwd` | Same working directory, grouped for notifier UIs |
| `project.bouncer_dir` | Resolved `.bouncer` directory, if any |
| `project.log_file` | Project log file path, if available |
| `session_id` | Harness/session id when provided |
| `decision` | Bouncer result: `ALLOW`, `DENY`, `UNSURE`, `TIMEOUT`, `LLM_ERROR`, `ESCALATE` |
| `action` | Action returned to the harness: `ALLOW`, `DENY`, `ASK`, or `null` |
| `reason` | Bouncer reason text |
| `request_id` | Process id used to correlate `PENDING` and final log rows |
| `elapsed_s` | Classification latency, when measured |
| `prompt_chars` | Approximate LLM prompt size, when available |

Example macOS notifier:

```yaml
notify:
  command:
    - /absolute/path/to/bouncer/integrations/notifiers/macos_notification.py
  decisions:
    - DENY
    - ESCALATE
    - TIMEOUT
```

The example script is intentionally small; copy or wrap it if you want sounds,
terminal output, a menu-bar app, or per-decision routing.

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
