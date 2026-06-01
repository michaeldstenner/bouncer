# Operations

## Command reference

```
bouncer init                  create .bouncer/ in current project
bouncer -g init               create user config/policy and offer harness wiring
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
bouncer tools                 list documented and observed harness tool names
bouncer review                interactive UNSURE decision review
bouncer classify --hook                 internal: hook interface (stdin → stdout)
bouncer classify --hook --format plain  plain-text output (allow/deny/ask + reason)
bouncer classify --hook --format codex-permission  Codex PermissionRequest output

bouncer -g config             edit user-level config.yaml
bouncer -g policy             edit user-level policy.md
bouncer -g log                view user-level log
bouncer -g review             review user-level UNSURE decisions
```

`bouncer activity` options:
```
--session <id>    session ID (required for statusline use)
--cwd <path>      project dir (enables inactive ○ indicator)
--project         render recent decisions from the project log for --cwd
--width <n>       number of recent decisions to show (default: 10)
--as <format>     plain | ansi | json | tmux
```

For tmux status bars where the pane cwd usually matches the active project:

```tmux
set -g status-interval 2
set -g status-right '#(bouncer activity --cwd "#{pane_current_path}" --project --as tmux --width 6 2>/dev/null) #[fg=blue]#{window_width}'
```

## Files at a glance

```
~/.config/bouncer/
  config.yaml          user-level config (all projects)
  policy.md            user-level policy (all projects)
  system_prompt.txt    custom LLM system prompt (optional)

~/.local/share/bouncer/
  log.jsonl            global decision log
  tools.json           global observed harness/tool catalog
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

## Log format

Decisions are written as JSONL to two locations:
- `~/.local/share/bouncer/log.jsonl` — all decisions, all projects
- `.bouncer/log.jsonl` — project-scoped (gitignored)

Each entry:
```json
{
  "timestamp": "2026-04-17T09:01:34.123",
  "tool": "Bash",
  "cwd": "/Users/you/project",
  "input_summary": "{\"command\": \"git push origin main\"}",
  "decision": "ALLOW",
  "reason": "git push is within the approved scope",
  "request_id": 12345
}
```

`input_summary` is a JSON-encoded copy of the tool input (truncated to 2000
chars). `request_id` links a `PENDING` entry (LLM call in flight) with its
resolution; `bouncer log` uses this to display per-decision latency.

## Internals

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
  hook.py               format_hook_response(), resolve_fallback() — hot path
  classify.py           run_classify() — core gate logic (no I/O, testable)
  shim/
    bash                universal shell shim (installed by bouncer -g init)
  providers/
    __init__.py         call_llm() dispatcher; _build_prompt, _parse_llm_text
  llmclient/            vendored llmclient library used for provider calls
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
    abort.py            bouncer abort
```

### Vendored llmclient

`bouncer/llmclient/` is a vendored copy of the llmclient library. Bouncer uses
it for provider calls, API key / URL resolution, Ollama queue management, and
LLM call logging while keeping the bouncer package dependency-free.

Bouncer's own decision log remains separate from llmclient's provider-call log.

### Hot path

`bin/bouncer` → `commands/classify.py:cmd_classify` → `classify.py:run_classify`
→ `providers:call_llm` → `hook.py:_emit_hook_response`

`run_classify` has no stdin/stdout side effects — unit-testable without mocking.

### Running tests

```sh
python3 test_bouncer.py
```
