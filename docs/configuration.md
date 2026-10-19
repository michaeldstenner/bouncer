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

## Policy review model

`bouncer review` uses an independent model configuration. It does not silently
reuse or fall back to the hot-path classifier model:

```yaml
# ~/.config/bouncer/config.yaml only
review:
  llm:
    provider: anthropic
    model: claude-sonnet-4-20250514
    timeout: 120
    # fallback_on: [timeout*, error:unreachable, http_5*]
    # fallbacks:
    #   - provider: openai
    #     model: gpt-4.1
    # extra_params:
    #   max_tokens: 8192
  max_batch_chars: 80000
  max_replay_events: 8
```

The `review` section is **user-config only**. Project config cannot choose the
review provider, model, URL, or credentials, because review sends aggregated
policy and log data rather than one request at a time. Direct model providers
(`ollama`, `openai`, `openai_compatible`, `openrouter`, and `anthropic`) are
supported. Agent-shaped providers such as `claude_code` are rejected because
they can load project hooks, memory, and MCP configuration even with tools
disabled.

`review.llm.model` is always explicit and never inherited from the classifier.
When the review provider is the same as the user-level `llm.provider`, its
`provider`, `url`, `api_key`, and `key_name` connection settings are inherited;
the model, timeout, fallbacks, and generation parameters remain review-specific.
A different review provider does not inherit the classifier endpoint or key.

Review payloads contain paths, command text, tool arguments, and current policy.
Choosing a remote provider therefore authorizes a larger disclosure than normal
single-request classification. Use a local provider if that aggregation should
not leave the machine.

`max_batch_chars` bounds each semantic-clustering call; larger review sets are
clustered in batches and consolidated. `max_replay_events` limits historical
examples in the current-versus-proposed classifier replay. Four fixed negative
canaries are always replayed, and every proposed canary result must be `DENY`
before the policy editor can open.

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
harness and its permission mode before being applied: `ask` becomes an
abstain where the fall-through is known to decide without a human, and `deny`
where it is not; a configured `abstain` is honoured on the same condition.
In practice that condition is Claude Code in `auto` and nothing else. See
[Session profiles](#session-profiles-live--solo).

## The hard guard, and uninitialized trees (`uninitialized`)

**Agents never edit bouncer config.** That rule is code, not policy: the
LLM never sees it, a policy cannot loosen it, and `# ESCALATE:` cannot reach
it. It runs on every tool call, in every directory — armed, opted out with
`enabled: false`, or with no `.bouncer/` at all.

It protects every project's `.bouncer/{config,config.local}.yaml` and
`.bouncer/{policy,policy.local}.md`, the `.bouncer/` directory itself
(removing it disarms the project), all of `~/.config/bouncer/`, and the
session-profile state under `~/.local/share/bouncer/profile/`. The decision
log is not protected.

It judges a shell command by **effect**, not by its command word. A segment
is refused when it redirects output into a protected location; when it
runs code — `python3 -c`, a heredoc fed to an interpreter, a script file, a
`helper run` script, a patch — that writes one; or when an argument names
one (directly, through a glob, a symlink, an earlier `cd`, or a variable
bound to one) and the command is not a known reader. Reading stays open:
`cat`, `sed -n`, `grep`, `diff`, `cp` *out of* a policy, `git add`.
Command substitutions — `$(…)`, backticks, and those in the body of an
unquoted heredoc — are read as commands in their own right; a quoted
heredoc's body is text, so an apostrophe or a backtick in a message is
never mistaken for code.

Code is judged by the **target** of each write: the path a `write_text`,
`open(…, "w")`, `shutil.copy`, `os.rename` or `writeFileSync` actually
writes, followed through variables assigned from a protected path. Merely
containing a protected path as text is not a write. That is what lets
bouncer be developed under its own guard: an edit to `classify.py` whose
new text says `.bouncer/policy.md` writes `classify.py`. Shell scripts are
judged line by line.

A command bouncer cannot parse (an unbalanced quote) is refused only when
it names a protected path or a bouncer management verb (`config`, `init`,
`policy`, `review`, `profile <name>`) — not for containing the word
"bouncer", which every command in the bouncer repo does.

There is no carve-out for the bouncer repo, by design: a directory-based
exemption is exactly the hole an agent would reach for. Bouncer's tests
work on fixtures in temp dirs and never touch a real protected path. A
human at the terminal is never subject to the guard: it is a hook on the
agent's tool calls, and sees nothing typed into an ordinary shell.

What it cannot see: a program whose own text does not name the location it
writes — a path built at run time, read from a file, or reached through a
build tool. Where the LLM runs, it is the backstop for those.

A tree with no `.bouncer/config.yaml` (no `bouncer init` yet) is
**uninitialized**. User-scope `uninitialized:` says what it gets beyond the
guard:

| Value | Meaning |
|---|---|
| `disabled` | Treated exactly like a project with `enabled: false`: the guard alone. No policy, no LLM call; everything else goes to the harness's own permission check. (default) |
| `blocked` | Every tool call bouncer would intercept is refused, with a message saying the user must run `bouncer init` there. Harness plumbing outside `tools:` (e.g. ToolSearch) is left alone, as in any project. |

An opt-out (`enabled: false` in a project) opts out of the **classifier**:
no project policy, no user-wide policy, no LLM call. It does not opt out of
the guard. A rule that must bind an opted-out or uninitialized tree has to
be a guard, not policy text.

## `enabled`: `true`, `false`, `bypass`

| Value | Classifier | Hard guard | Log | Meaning |
|---|---|---|---|---|
| `true` | yes | yes | yes | Normal. (default) |
| `false` | no | **yes** | guard denials | Opted out of the classifier only. |
| `bypass` | no | **no** | **no** | No hook at all, as if bouncer were not installed. |

`bypass` is the real off switch. The hook exits before doing anything: no
guard, no policy, no LLM call, no log line (the per-prompt break marker
included), no state, no output.

**Plain YAML, no exceptions.** `bypass` is a string, read like any other
value (`bypass`, `"bypass"`, `BYPASS`). `off`, `no` and `false` are all
YAML false and mean `enabled: false`: classifier off, guard **on**.

**Where, and precedence.** In `~/.config/bouncer/config.yaml`, bypass
applies **everywhere**, whatever any project says. In a project's
`.bouncer/config.yaml` or `config.local.yaml`, bypass covers that tree only
(the nearest `.bouncer/`), and nothing outside it.

**Who.** Only a person: agents can write no bouncer config. But while a
tree is bypassed, nothing guards it — an agent working there could write
any file, bouncer's config included, here or in another project, or the
global switch. That is the risk `bypass` takes on; use it where you are
watching.

**Never silent.** With the SessionStart hook installed (`bouncer -g init`
wires `bouncer session-start`), a session that starts in a bypassed tree
shows one line: `bouncer is BYPASSED here (enabled: bypass in <file>)`, and
the agent is told the same. Setting it mid-session is announced at the next
session. At any time `bouncer status` prints `bouncer BYPASSED`, and
`bouncer profile` reports `bypass` (`--as json`: `{"profile": "bypass",
"bypass": true, "bypass_source": …}`; `--as tmux` / `ansi`: white on red).

**Before this existed** (bouncer before 2026-10-15), `enabled: bypass` was
an unknown value and, being a non-empty string, counted as **true**: the
classifier ran. Upgrade bouncer before setting it.

## Approved policy edits (`propose` / `apply` / `revert`)

A working agent never edits a policy — its own or anyone's. The
**orchestrator** may change *another* project's `policy.md`, but only a
change the user approved in the moment, and only that exact change.

```
bouncer propose <project> <diff|->   # store it; print an 8-char code + diff
# the orchestrator shows the user the diff and the code; the user says yes
bouncer apply <code>                 # write exactly that change
bouncer revert <code>                # put back what was there before
```

Enable it by naming the orchestrator in the **user** config (a project
config cannot name itself):

```yaml
orchestrators: [~/Code/hobbes]
```

**Who may apply.** The process doing the write must descend from a Claude
Code session (a process named `claude`) whose launch directory is an
orchestrator, with no second `claude` above it. A session's launch
directory is the `claude` process's own cwd, which the agent inside cannot
change — `cd` moves only its shell. So neither the cwd nor the hook payload
(which an agent can forge by piping JSON into the hook script) is trusted;
the process tree is. Refused: a worker's session, a session nested inside
another (`claude -p` started by an agent in the orchestrator's directory),
and any process with no session above it — a person at a terminal, who
edits the policy directly instead, or a process detached from its session.
The check is made in the code that writes, so the CLI, a Python import and
a forged payload all meet it; the hook also refuses `bouncer apply|revert`
from a non-orchestrator before it runs.

**The limit.** A session launched fresh in the orchestrator's directory
*outside* any agent's process tree (through tmux, say) is
indistinguishable from the orchestrator. Approval travels through the
orchestrator by design, as with the airlock at home; a confirmation channel
the orchestrator cannot author (a Polly button) is the planned upgrade, and
`_approval_ok` in `bouncer/proposals.py` is where it plugs in.

**What may be written.** Only `<project>/.bouncer/policy.md` of an
initialized project that is not an orchestrator, as a regular file. Never
`policy.local.md` (the user's private layer), any `config*.yaml`,
`~/.config/bouncer/`, profile state, or the proposal store. `bouncer
review` stays user-only.

**Integrity.** The code is a fingerprint of the target, the text it starts
from, the text it ends at, and the diff the user was shown. `apply`
refuses: a code whose stored proposal no longer matches it (altered after
proposing), a code already used (one code, one approval, one apply), and a
policy that changed since it was proposed. `revert` works once per code,
and only while the policy still holds what `apply` wrote. Every apply and
revert is appended to `~/.local/share/bouncer/proposals/log.jsonl` (time,
code, target, hashes, and the orchestrator that did it). The store is in
the guard's protected set: no agent may write it.

## Arming a new project (`seed`)

`bouncer init` is refused to every agent. `bouncer seed <dir>` is the one
exception: it gives a tree with no bouncer at all the same stock
`config.yaml`, `policy.md` and `.gitignore` that `init` writes. That makes
the tree stricter, never looser — an uninitialized tree has the guard and
nothing else. Only the orchestrator may run it, when it stands up a new
project.

It is allowed only when all of these hold:

- the caller is the orchestrator: the same process-tree check as `bouncer
  apply` (see "Who may apply" above). A worker, a nested session and a
  person at a terminal are refused; the person runs `bouncer init`;
- `<dir>` is an existing directory, and not the home directory or above it;
- neither `<dir>` nor any directory above it has a `.bouncer/` (of any
  content). This is the walk bouncer makes to find a project, so a seeded
  tree can never hide an armed parent's policy;
- `<dir>` is not the caller's own project, in either direction. The
  caller's project is the nearest directory with a `.git` at or above
  where it runs (or that directory itself), judged from the hook's cwd, the
  session's launch directory and the process cwd;
- the argument is one literal path and nothing else: no options, no
  `$VAR`, globs or backticks. Seeding takes no content.

The hook checks this before the command runs; `bouncer/seed.py` checks it
again where it writes. After seeding, policy text for the new project goes
through `propose` / `apply` like any other.

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

`solo` is honorable on every harness, but what it resolves *to* depends on
where an abstain would land, because "abstain" means *fall through to the
harness's own floor* and not every harness has one. See
[`integrations.md`](integrations.md#what-an-abstain-reaches) for the full
table.

In short: `solo` abstains **only** on Claude Code running in `auto`
permission mode, whose floor is auto-mode's safety classifier — a machine.
Every other case resolves to `deny`: Claude Code in any other permission
mode, opencode and Codex (whose abstain asks a human, which under `solo` is
an ASK by another name), the shell shim (whose abstain lets the call
through), and any harness bouncer cannot identify.

The permission mode is read from the hook payload and matched from an
allowlist, so a mode a future Claude Code adds — or a payload that omits the
key — denies rather than silently re-opening the stall. Under `live` none of
this applies: `on_unsure` and `on_unavailable` are used exactly as written.

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

`review` is the exception: it is loaded directly from user config and is never
merged from either project layer.

## Custom system prompt

Place a custom LLM system prompt at `~/.config/bouncer/system_prompt.txt`.
If absent, the built-in default is used — it instructs the LLM to classify as
ALLOW/DENY/UNSURE and hardcodes a short list of absolute-deny patterns
(`rm -rf` outside project, force-push, writes to `/etc/`, curl-to-shell).
