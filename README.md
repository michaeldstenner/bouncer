# bouncer

LLM-powered permission classifier for AI coding agents. Intercepts
requests for human approval to provide an LLM-driven risk analysis,
based on project scope and operation.

Bouncer is **opt-in per project**: it only activates when `.bouncer/config.yaml`
exists somewhere in the directory tree above the working directory.

---

## How it works

AI coding agents (Claude Code, Codex, opencode) ask the user before running
risky operations. Bouncer steps in as the reviewer: it passes the pending tool
call to a local or remote LLM along with your project policy, then relays the
verdict back to the harness.

The LLM returns one of three decisions:

| Decision | Meaning |
|---|---|
| **ALLOW** | Operation is within policy — harness approves without asking the user|
| **DENY** | Operation is out of scope — harness blocks with an explanation to the requesting agent without asking the user|
| **UNSURE** | LLM couldn't decide — escalated to the user to allow/deny |

If the LLM is unreachable, the `on_unavailable` fallback applies (default: ask).

Every decision is logged and shown in the statusline activity strip.

### Override mechanism

If the agent wants to escalate a request to the user, it repeats the
command and prefixes it with `# OVERRIDE:`:

```sh
# OVERRIDE: clearing build artifacts before release
rm -rf dist/ build/
```

In such cases, bouncer skips the LLM entirely and escalates directly
to the user. The override reason is shown and logged. This is how the
agent signals "I know this looks sketchy — here's why I need it"
without permanently widening the policy.

---

## Day-to-day use

```sh
bouncer status          # is bouncer active? what LLM? what tools?
bouncer status -v       # full config breakdown

bouncer log             # view decision log (opens in less)
bouncer log --tail      # follow in real time
bouncer log --filter deny   # filter by decision type
bouncer log --since 2h  # last 2 hours only

bouncer check 'git push origin main'        # what would bouncer decide?
bouncer check --llm 'git push origin main'  # ask the LLM directly

bouncer review          # interactive UNSURE decision review
```

Use `bouncer -g <cmd>` to operate on user-scope data instead of the project:

```sh
bouncer -g log     # global log (all projects)
bouncer -g review  # review user-level UNSURE decisions
```

---

## Policy (`policy.md`)

The policy file is plain markdown fed verbatim to the LLM as context.
Describe the project in terms that help the classifier make good decisions:

```markdown
# Project Policy

- Source of truth is in src/; tests are in tests/.
- The deploy script (scripts/deploy.sh) is expected to run in CI only.
- Database migrations run with `alembic upgrade head` — safe in dev.
- External services: AWS S3 (read-only), GitHub API.
- Never touch /etc/ or system Python.
```

The more specific the policy, the fewer UNSURE verdicts you'll see.

**Edit:** `bouncer policy` opens `.bouncer/policy.md` in `$EDITOR`.

**Scope:** project policy is appended to user-level policy by default
(`policy_mode: append`). Set `policy_mode: replace` if the project needs a
completely different risk profile.

**User-level policy** (`bouncer -g policy`) applies to all projects and is a
good place for personal norms ("never touch my dotfiles", "no force-push ever").

---

## Configuration (`config.yaml`)

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

### LLM providers

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

### `tools`

| Value | Meaning |
|---|---|
| `["Bash"]` | Only intercept Bash (default) |
| `["Bash", "Write"]` | Intercept Bash and Write |
| `all` | Intercept every tool |
| `[]` | Intercept nothing (bouncer inactive but config preserved) |

### `on_unsure` / `on_unavailable`

| Value | Meaning |
|---|---|
| `ask` | Escalate to the human (default) |
| `allow` | Pass through silently |
| `deny` | Block with the LLM's reason |
| `deny_with_message` | Block with reason plus an `# OVERRIDE:` hint (recommended for opencode) |

### Config merge order

Later entries override earlier ones:

1. Built-in defaults
2. `~/.config/bouncer/config.yaml` (user-level)
3. `.bouncer/config.yaml` (project-level)
4. `.bouncer/config.local.yaml` (machine-local overrides, gitignored)

### Custom system prompt

Place a custom LLM system prompt at `~/.config/bouncer/system_prompt.txt`.
If absent, the built-in default is used — it instructs the LLM to classify as
ALLOW/DENY/UNSURE and hardcodes a short list of absolute-deny patterns
(`rm -rf` outside project, force-push, writes to `/etc/`, curl-to-shell).

---

## Command reference

```
bouncer init                  create .bouncer/ in current project
bouncer init --harness=auto   also wire detected AI harness hooks
bouncer init --harness=NAME   wire a specific harness (claude_code | codex | opencode)
bouncer lint [file]           validate config.yaml
bouncer config                open config.yaml in $EDITOR
bouncer policy                open policy.md in $EDITOR
bouncer status                show active config (net behavior)
bouncer status -v             show full config breakdown
bouncer activity              print colored activity indicator
bouncer log                   view decision log (opens in less)
bouncer log --tail            follow log in real time
bouncer log --filter deny     filter by decision type
bouncer log --since 2h        show last 2 hours
bouncer log --break           append prompt separator (called by hook)
bouncer check <cmd>           dry-run: shows what bouncer would decide
bouncer check --llm <cmd>     dry-run: actually calls the LLM
bouncer review                interactive UNSURE decision review
bouncer classify --hook                 internal: hook interface (stdin → stdout)
bouncer classify --hook --format plain  plain-text output (allow/deny/ask + reason)

bouncer -g config             edit user-level config.yaml
bouncer -g policy             edit user-level policy.md
bouncer -g log                view user-level log
bouncer -g review             review user-level UNSURE decisions
```

`bouncer activity` options:
```
--session <id>    session ID (required for statusline use)
--cwd <path>      project dir (enables inactive ○ indicator)
--width <n>       number of recent decisions to show (default: 10)
```

---

## Files at a glance

```
~/.config/bouncer/
  config.yaml          user-level config (all projects)
  policy.md            user-level policy (all projects)
  system_prompt.txt    custom LLM system prompt (optional)

~/.local/share/bouncer/
  log.jsonl            global decision log
  activity/
    <session_id>.json  per-session activity strip data

<project>/
  .bouncer/
    config.yaml        project config (committed)
    policy.md          project policy (committed)
    config.local.yaml  local overrides (gitignored)
    log.jsonl          project decision log (gitignored)
    .gitignore         protects log + config.local.yaml
```

### Log format

Decisions are written as JSONL to two locations:
- `~/.local/share/bouncer/log.jsonl` — all decisions, all projects
- `.bouncer/log.jsonl` — project-scoped (gitignored)

Each entry:
```json
{
  "timestamp": "2026-04-17T09:01:34.123",
  "tool": "Bash",
  "cwd": "/Users/you/project",
  "input_summary": "{'command': 'git push origin main'}",
  "decision": "ALLOW",
  "reason": "git push is within the approved scope",
  "request_id": 12345
}
```

`request_id` links a `PENDING` entry (LLM call in flight) with its resolution;
`bouncer log` uses this to display per-decision latency.

---

## Setup

### 1. Install bouncer

**Requirements:** Python 3.11+, no third-party dependencies.

```sh
pip install -e /path/to/agent_tools
bouncer --help   # verify
```

Or run without installing: `python3 -m bouncer <command>`

### 2. Initialize a project

```sh
cd your-project
bouncer init --harness=auto   # init .bouncer/ + auto-detect and wire harness hooks
bouncer policy                # describe the project for the LLM
bouncer config                # adjust settings if needed
bouncer status                # confirm it's active
```

`--harness=auto` detects whichever AI coding harnesses are installed and wires
them automatically. Pass a specific name (`claude_code`, `codex`, `opencode`)
to target one harness, or omit `--harness` entirely to skip hook wiring.

### 3. User-level defaults (optional)

Settings and policy applied here apply to all bouncer-enabled projects:

```sh
bouncer -g config   # ~/.config/bouncer/config.yaml
bouncer -g policy   # ~/.config/bouncer/policy.md
```

---

## Integrations

Bouncer supports three AI coding harnesses. Integration files live in
`integrations/<harness>/` in this repo. `bouncer init --harness=<name>` handles
installation automatically; the details below are for manual setup or reference.

### Claude Code

`bouncer init --harness=claude_code` does the following:

1. Writes `~/.claude/hooks/bouncer_hook.py` (the wrapper script)
2. Patches `~/.claude/settings.json` with two hooks:
   - `PreToolUse` on `Bash` — core classification hook
   - `UserPromptSubmit` — appends a `·` break marker between interaction bursts

Manual `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/bouncer_hook.py",
            "timeout": 30000
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bouncer log --break"
          }
        ]
      }
    ]
  }
}
```

Hook output protocol (stdout JSON):
- ALLOW → `{"hookSpecificOutput": {"permissionDecision": "allow", ...}}`
- DENY → exit code 2, reason on stderr
- UNSURE/unavailable → `{"hookSpecificOutput": {"permissionDecision": "ask", ...}}`

#### Statusline

`bouncer init --harness=claude_code` does not configure the statusline
(it requires a custom `statusline.sh` that varies per setup). To add it,
set `statusLine` in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "/path/to/statusline.sh"
  }
}
```

The statusline script must read `session_id` and `cwd` from its stdin JSON
and pass them to `bouncer activity`:

```bash
input=$(cat)
session_id=$(echo "$input" | jq -r '.session_id // empty')
cwd=$(echo "$input" | jq -r '.cwd // empty')
bouncer activity --session "$session_id" --cwd "$cwd" 2>/dev/null
```

Activity indicator — one character per recent decision, newest on the left:

| Char | Color | Meaning |
|------|-------|---------|
| Tool initial (B/W/E/R/G…) | green | ALLOW |
| Tool initial | black on red | DENY |
| Tool initial | yellow | UNSURE |
| Tool initial | cyan | OVERRIDE |
| `·` | dim | Prompt boundary |
| `○` | dim | Bouncer active, no decisions yet |

Tool characters: `B`=Bash, `W`=Write, `E`=Edit, `R`=Read, `G`=Glob/Grep,
`T`=Task, `F`=WebFetch, `S`=WebSearch, `?`=unknown.

### OpenAI Codex CLI

`bouncer init --harness=codex` does the following:

1. Copies `integrations/codex/bouncer_hook.py` to `~/.codex/hooks/`
2. Patches `~/.codex/hooks.json` with a `PreToolUse` entry for Bash

**Limitation:** Codex only fires `PreToolUse` for Bash; file-write tools are
not intercepted. There is no Codex equivalent of `UserPromptSubmit` or the
statusline.

Manual `~/.codex/hooks.json`:

```json
{
  "hooks": [
    {
      "matcher": "tool == \"Bash\"",
      "command": "~/.codex/hooks/bouncer_hook.py",
      "timeout": 30
    }
  ]
}
```

### opencode

`bouncer init --harness=opencode` does the following:

1. Copies `integrations/opencode/bouncer_plugin.ts` to `~/.config/opencode/plugins/bouncer.ts`
2. Adds `"bouncer"` to the `plugin` list in `~/.config/opencode/opencode.json`

**Coverage:** opencode's `tool.execute.before` fires for all built-in tools
(bash, read, edit, write, apply_patch) — broader than Codex's Bash-only scope.
`apply_patch` maps to `Write` for the bouncer tools filter.

**`ask` behavior:** opencode has no mid-execution escalation UI. When bouncer
returns `ask` (UNSURE), the plugin blocks with a message asking the agent to
re-run with `# OVERRIDE: <reason>`. For a plain deny, set `on_unsure: deny`;
to include the OVERRIDE hint in the deny, set `on_unsure: deny_with_message`.

Manual `~/.config/opencode/opencode.json`:

```json
{
  "plugin": ["bouncer"]
}
```

---

## Source layout

Bouncer is a zero-dependency Python package. No PyYAML, no click, no httpx —
only stdlib. Run with `python3 -m bouncer` or via the `bouncer` console script.

```
bouncer/
  __init__.py           empty
  __main__.py           argparse wiring + dispatch
  yaml.py               MicroYAML — embedded YAML subset parser (no deps)
  colors.py             ANSI color constants; DECISION_COLORS map
  config.py             path constants, CONFIG_DEFAULTS, config load/merge,
                        policy context, project discovery, templates
  log.py                log_decision(), pruning, verbosity filter
  activity.py           activity strip: tool-char map, update, render
  hook.py               _emit_hook_response(), _handle_fallback() — hot path
  classify.py           run_classify() — core gate logic (no I/O, testable)
  providers/
    __init__.py         call_llm() dispatcher; _build_prompt, _parse_llm_text
    ollama.py           Ollama /api/generate
    openai.py           OpenAI chat completions (also openai_compatible)
    anthropic.py        Anthropic /v1/messages
  commands/
    __init__.py         empty
    init.py             bouncer init [--harness]
    lint.py             bouncer lint
    config.py           bouncer config / policy
    status.py           bouncer status [-v]
    activity.py         bouncer activity
    log.py              bouncer log
    check.py            bouncer check
    classify.py         bouncer classify --hook (thin stdin wrapper)
    review.py           bouncer review
```

### Hot path

`bin/bouncer` → `commands/classify.py:cmd_classify` → `classify.py:run_classify`
→ `providers:call_llm` → `hook.py:_emit_hook_response`

`run_classify` has no stdin/stdout side effects — unit-testable without mocking.

### Adding a provider

1. Create `bouncer/providers/<name>.py` with a `call_<name>(tool_name, tool_input, cwd, config)` that returns `(decision | None, reason)`.
2. Add a branch in `bouncer/providers/__init__.py:call_llm`.
3. Add the provider name to the `llm.provider` docs and `cmd_lint`.

### Running tests

```sh
python3 test_bouncer.py
```
