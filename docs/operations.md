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
bouncer review                guided clustering and policy refinement
bouncer review --deny         review only DENY decisions
bouncer review --since 14d    ignore cursor; review a recent window
bouncer review --all-history  ignore cursor; review the retained log
bouncer abort                 abort the pending LLM classification → ALLOW
bouncer escalate [reason]     send your last denied tool call to the user,
                              then re-issue that exact call (the out-of-band
                              escalation path for non-Bash tools)
bouncer classify --hook                 internal: hook interface (stdin → stdout)
bouncer classify --hook --format plain  plain-text output (allow/deny/ask + reason)
bouncer classify --hook --format codex-permission  Codex PermissionRequest output

bouncer -g config             edit user-level config.yaml
bouncer -g policy             edit user-level policy.md
bouncer -g log                view user-level log
bouncer -g review             review cross-project evidence for user policy
```

`bouncer activity` options:
```
--cwd <path>      project dir; the strip renders this project's recent log
--width <n>       number of recent decisions to show
                  (default: config activity_width, or 10)
--as <format>     plain | ansi | json | tmux
--session <id>    accepted for backward compatibility; no longer used
--project         accepted for backward compatibility; no longer needed
```

The strip always renders from the project's decision log, so the Claude Code
statusline and the tmux indicator are the same view. Break markers (the dim
`·` separators) appear wherever a harness hook logs them — see
[integrations](integrations.md). `--session`/`--project` are kept so existing
statusline scripts and tmux configs keep working unchanged.

For tmux status bars where the pane cwd usually matches the active project:

```tmux
set -g status-interval 2
set -g status-right '#(bouncer activity --cwd "#{pane_current_path}" --as tmux --width 6 2>/dev/null) #[fg=blue]#{window_width}'
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
  reviews/             private review cursors and reports (mode 0700/0600)

<project>/
  .bouncer/
    config.yaml        project config (committed)
    policy.md          project policy (committed)
    config.local.yaml  local config overrides (gitignored)
    policy.local.md    local policy additions (gitignored)
    log.jsonl          project decision log (gitignored)
    .gitignore         protects logs + config.local.yaml + policy.local.md
```

## Log format

Decisions are written as JSONL to two locations:
- `~/.local/share/bouncer/log.jsonl` — all decisions, all projects
- `.bouncer/log.jsonl` — project-scoped (gitignored)

Each entry:
```json
{
  "event_id": "e3d8bb1f09f04cc89d93cb2a22a3e7d2",
  "timestamp": "2026-04-17T09:01:34.123",
  "tool": "Bash",
  "cwd": "/Users/you/project",
  "input_summary": "{\"command\": \"git push origin main\"}",
  "decision": "ALLOW",
  "reason": "git push is within the approved scope",
  "request_id": 12345
}
```

`input_summary` is a JSON-encoded copy of the tool input bounded to 2000
characters. Oversized values are truncated while preserving valid JSON and
priority fields such as `file_path` and `command`. `event_id` is the stable
review-cursor identity. `request_id` links a `PENDING` entry (LLM call in flight)
with its resolution; `bouncer log` uses this to display per-decision latency.

## Policy review

Configure a separate model under `review.llm` in
`~/.config/bouncer/config.yaml`, then run `bouncer review` directly from an
interactive terminal. The workflow is:

1. Read detailed project-log events not present in the local review cursor.
2. Exclude operational failures and report compact rows lacking request data.
3. Send current source-separated policy plus untrusted request/decision records
   to the independent reviewer.
4. Semantically cluster requests and validate that every event appears exactly
   once; oversized inputs use batch clustering plus consolidation.
5. Show each cluster with local decision counts and representative raw requests.
6. Record your disposition: agree, allow, deny, one-off, comment, or skip.
7. Ask the reviewer for the smallest policy revision consistent with those
   dispositions.
8. Replay representative history and fixed negative canaries against current
   and proposed policy using the normal classifier model.
9. Open the proposed committed/local policy in a temporary `$EDITOR` buffer.
10. After save, re-check source bytes, show the exact diff, ask for confirmation,
    re-check for drift, and atomically stage the policy writes.

The reviewer has no tools and its output is inert until the editor and final
confirmation complete. A failed canary blocks the editor. If no policy is
applied, bouncer asks separately whether to advance the cursor; declining leaves
the requests for the next run. Reports and cursor state are outside the project
under `~/.local/share/bouncer/reviews/` with private permissions.

Project review can propose only `.bouncer/policy.md` and
`.bouncer/policy.local.md`. Global review can propose only the user policy and
includes project/cwd provenance so project-specific behavior is not silently
generalized. Project config cannot override the reviewer endpoint.

Direct agent invocations of `bouncer review`, `bouncer policy`, mutating
`bouncer config`, and `bouncer init` are deterministically denied when they pass
through an active Bouncer hook. Native Write/Edit-style calls targeting policy
or config files are denied before the configured tool filter. This prevents the
review workflow from becoming an obvious self-authorization path, but Bouncer
is not an OS sandbox: an agent that can bypass harness hooks or write files as
the user remains outside this guarantee.

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
  review.py             review ingestion, model clustering/synthesis, replay
  activity.py           activity strip: tool-char map, update, render
  hook.py               format_hook_response(), resolve_fallback() — hot path
  classify.py           run_classify() — core gate logic (no I/O, testable)
  shim/
    bash                universal shell shim (installed by bouncer -g init)
  integrations/         packaged harness assets copied out by bouncer init
    codex/              PermissionRequest + legacy PreToolUse hooks
    opencode/           bouncer_plugin.ts
    notifiers/          example notify.command scripts
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

## Reporting a problem

When something misbehaves, the useful diagnostics are:

```sh
bouncer status -v                          # effective config + LLM reachability
bouncer check --llm 'pwd'                  # does a real classification succeed?
tail -n 20 .bouncer/log.jsonl             # recent project decisions
tail -n 20 ~/.local/share/bouncer/log.jsonl   # recent decisions, all projects
```

Also note which harness was in use (Claude Code, Codex, opencode, or the
shell shim) — behavior around ASK and abstain differs by integration.
