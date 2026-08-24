# Configuration

## `config.yaml`

All settings are optional at the project level. Unset keys inherit from
user config (`~/.config/bouncer/config.yaml`) or built-in defaults.

```yaml
# Master on/off switch — disables bouncer without removing config
#enabled: true

# Tools to intercept — an ordered list of ±ops folded onto the inherited set.
# `+X` intercepts, `-X` skips; X may be a tool name, a glob, `@all`, or a group
# like `@internal` (harness plumbing such as Claude's ToolSearch, skipped by
# default). `all` = `+@all -@internal`. See the `tools` section below.
#tools:
#  - -Read

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
  # priority: 80                # higher = served first (default: 80)
  # caller_max: 4               # max concurrent bouncer calls in the queue
  # queue_timeout: 30           # max wait for a queue slot — IGNORED under
  #                             #   circuit_mode: futility, where deadline_s
  #                             #   bounds the queue wait too. count mode only.
  # queue_stall_timeout: 15     # bail if nothing has completed within this window

  # Two-phase timeouts (Ollama streaming) — IGNORED under circuit_mode:
  # futility, where deadline_s owns the call end to end. Only used in
  # circuit_mode: count.
  # first_token_timeout: 8      # seconds to wait for first response token
  # generation_timeout: 30      # seconds for full response after first token

  # Circuit breaker — prevents hammering an unresponsive LLM
  # Two modes: "futility" (default, leaky-LLR) and "count" (consecutive-N).
  # circuit_mode: futility       # futility | count
  # circuit_cooldown_s: 120      # seconds open before probe attempt
  # grace_s: 8                   # min wait before stall bail may fire
  # deadline_s: 180              # pencils-down ceiling (null = no ceiling);
  #                              #   under futility it bounds the whole call
  # ps_probe: true               # cheap /api/ps liveness probe (ollama only)
  # circuit_n: 2                 # (count mode) failures before opening
  # circuit_triggers:            # (count mode) which outcomes count
  #   - timeout:queue_stall
  #   - timeout:first_token
  #   - error:unreachable

  # Model fallback chain. The top-level llm block is tried first; each fallback
  # inherits provider/url/api_key/timeouts unless it overrides them. If a
  # fallback changes provider and omits url/api_key, those are cleared so the
  # new provider can use its normal defaults/env vars.
  # fallback_on: [timeout*, error:unreachable, http_5*, circuit_open, circuit_futile]
  # fallbacks:
  #   - model: gpt-oss-120b       # same provider/url/api_key as primary
  #   - model: nemotron-3-ultra
  #   - provider: anthropic       # e.g. home fallback from Ollama to Haiku
  #     model: claude-haiku-4-5-20251001

  # extra_params:               # optional provider-specific request params
  #   max_tokens: 1000

# Fallback behavior when the LLM is uncertain or unreachable
on_unsure: ask              # ask | allow | deny
on_unavailable: ask         # ask | allow | deny

# Escalation gating. An agent escalates a denied command by re-running it with
# a `# ESCALATE:` prefix. Only honor an escalation if the same command was
# actually attempted (bare, without the prefix) within the TTL window — curbs
# agents that pre-emptively escalate commands they never tried.
escalation_requires_attempt: true   # true | false
escalation_attempt_ttl: 300         # seconds

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

`verbosity` controls how much each decision records:

- `all` — every decision in full detail.
- `deny_only` — denials in full; everything else (and prompt-boundary `·`
  break markers) as a compact row of just timestamp, tool, and decision. This
  keeps the audit log focused while leaving the activity strip complete.
- `off` — log nothing. The activity strip is empty too, since it renders from
  the log.


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
  # Set a key here or export OPENAI_API_KEY.
  # extra_params:
  #   max_tokens: 1000
```

## `tools`

`tools` is an **ordered list of `±` operations** folded left-to-right over a
running set, evaluated per tool. `+X` intercepts tools matching `X`; `-X` skips
them (bouncer abstains and the harness decides); the **last matching op wins**.
`X` may be a tool name (`Bash`), a glob (`mcp__google_workspace__*`), `@all`
(every tool), or a group such as `@internal`.

| Value | Meaning |
|---|---|
| `all` | `+@all -@internal` — every tool except harness plumbing (the default) |
| `[-Read]` | inherited set, minus Read |
| `[+ToolSearch]` | inherited set, plus the normally-skipped ToolSearch |
| `[-@all, +Bash]` | only Bash (absolute) |
| `[Bash, Write]` | legacy shorthand for `[-@all, +Bash, +Write]` ("only these") |
| `[]` | intercept nothing |

`@internal` is harness plumbing — discovery/meta tools the harness already
auto-allows, so classifying them only wastes an LLM call. Claude Code's
`ToolSearch` (a no-op deferred-tool schema loader) is the default member;
skipping it is free, because loading a tool's schema is not the same as calling
it, and the call still hits bouncer's gate on its own.

**Layering:** configs fold in order — user → project `config.yaml` →
`config.local.yaml` — each applying its ops to the set it inherits (seeded with
the default `all`). A bare list is "absolute" (it carries an implicit leading
`-@all`); a list of `±`-prefixed ops modifies the inherited set.

**Groups** are editable with the same `±` algebra via a `groups:` block, e.g.
re-gate a plumbing tool everywhere:

```yaml
groups:
  internal: -ToolSearch        # ToolSearch is now classified, not skipped
```

`bouncer lint` prints the resolved op list and warns on the deprecated bare
`all` (which now resolves to `+@all -@internal`).

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
| `abstain` | Emit no decision at all — defer to the harness's own permission flow. Not the same as `allow`: risky calls still hit the harness's own gate, where it has one |

Under a profile with no ASK channel (`solo`), these are resolved through the
harness before being applied: `ask` becomes the harness's own unattended floor
where one exists and `deny` where none does, and a configured `abstain` is
honoured only where abstaining reaches a floor that decides without a human.
See [Session profiles](#session-profiles-live--solo).

## Session profiles (`live` / `solo`)

A profile changes the **plumbing, not the judgment**. `policy.md` remains the
only thing that decides ALLOW/DENY. A profile changes what bouncer does when
it *cannot* decide, and whether an agent may appeal a denial to a human.

Two profiles ship, and the names are used everywhere — config key, CLI
argument, indicator text, and out loud:

| Profile | Meaning |
|---|---|
| `live` | A human is on the line and can be asked. `on_unsure`/`on_unavailable` are used as written, and escalation works. |
| `solo` | The agent runs alone. **No ASK is ever produced** — escalation, `on_unsure`, and `on_unavailable` all resolve to something else. |

```sh
bouncer profile          # show the effective profile for this project
bouncer profile solo     # switch this project to solo
bouncer profile live     # switch it back
```

One verb, values as arguments — there is deliberately no `bouncer solo`
command, and an unrecognised name is an error rather than an action.

### Where the profile lives

Profile state is a plain JSON file under
`~/.local/share/bouncer/profile/`, keyed on the project (the resolved
`.bouncer/` dir) — the same key the escalation grant state uses. It is a file
rather than an environment variable so the profile can be changed
mid-session, and so a second process (a tmux status line) can read it.

An agent may not set the profile of the project it is running in: bouncer
denies `bouncer profile <name>` when it sees it as a tool call. This is
footgun cover — an agent that notices the command exists and helpfully flips
itself to `live` — not a security boundary.

### Defining profiles in config

`default_profile` names the profile in force when a project has no profile
state. All profiles are explicit: absence of state means "use
`default_profile`", never "use hardcoded behaviour".

```yaml
default_profile: live

profiles:
  solo:
    escalation: off          # no ASK may be produced in this profile
    on_unsure: abstain
    on_unavailable: abstain
```

`escalation` is its own key rather than something derived from the profile
name, so a variant of `solo` can change one without the other.

A profile fragment beats base keys **within its own layer**, and later layers
still win:

```
user.base -> user.profiles[P] -> project.base -> project.profiles[P]
          -> local.base -> local.profiles[P]
```

Profile fragments carry plumbing keys only (`escalation`, `on_unsure`,
`on_unavailable`, the escalation-gating keys, `notify`, `log`). `bouncer
lint` warns about anything else in a fragment, about a `solo` profile that
re-enables escalation, and about a `live` default on a harness that cannot
ask.

### What `solo` resolves to

`solo` is honorable on every harness, but what it resolves *to* is
harness-dependent, because "abstain" means *fall through to the harness's own
floor* and not every harness has one. See
[`integrations.md`](integrations.md#what-an-abstain-reaches) for the
per-harness table. In short: on Claude Code, abstaining reaches auto-mode's
safety classifier, which decides without a human; everywhere else abstaining
reaches either a human prompt or nothing at all, so `solo` resolves to `deny`
instead.

### Effective vs nominal

Capability is **profile AND harness**. A harness with no ASK channel cannot
ask however the profile is set, so `live` degrades to `solo`'s behaviour
there. `bouncer profile` and `bouncer status` show the *effective* state, and
mark a degraded `solo` differently from a chosen one — a degraded `solo`
means your request is not being honoured, while a chosen `solo` is normal.

```tmux
set -g status-right '#(bouncer profile --cwd "#{pane_current_path}" --as tmux) #(bouncer activity --cwd "#{pane_current_path}" --as tmux --width 6)'
```

`bouncer profile --as tmux` prints a bare styled word: green `live`, amber
`solo`, and inverted amber for a degraded `solo`. Both names are four
characters, so the status line does not jitter when it flips.

## Escalation gating

An agent escalates a denied command by re-running it with a `# ESCALATE: <reason>`
prefix, which bypasses the LLM and forwards the request to the user. Some agents
abuse this by escalating pre-emptively — without ever trying the command first.

When `escalation_requires_attempt` is `true` (the default), an escalation is only
honored if the same command was actually run (bare, without the prefix) within
the last `escalation_attempt_ttl` seconds in this session. An escalation for a
command that was never attempted is denied with a hint to run it first. Commands
are matched after collapsing whitespace, so trivial reformatting still counts as
the same command.

| Key | Default | Meaning |
|---|---|---|
| `escalation_requires_attempt` | `true` | Require a recent bare attempt before honoring an escalation |
| `escalation_attempt_ttl` | `300` | How long (seconds) a bare attempt stays eligible to justify an escalation |

## Config merge order

Later entries override earlier ones:

1. Built-in defaults
2. `~/.config/bouncer/config.yaml` (user-level)
3. `.bouncer/config.yaml` (project-level)
4. `.bouncer/config.local.yaml` (machine-local overrides, gitignored)

Each layer's `profiles:` fragment for the active profile is applied
immediately after that layer's own base keys — see
[Session profiles](#session-profiles-live--solo).

## Custom system prompt

Place a custom LLM system prompt at `~/.config/bouncer/system_prompt.txt`.
If absent, the built-in default is used — it instructs the LLM to classify as
ALLOW/DENY/UNSURE and hardcodes a short list of absolute-deny patterns
(`rm -rf` outside project, force-push, writes to `/etc/`, curl-to-shell).
