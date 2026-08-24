# Integrations

Bouncer supports four integration targets. The hook/plugin assets ship inside
the package under `bouncer/integrations/<harness>/`, and the universal shell
shim under `bouncer/shim/bash`, so a plain install can wire them.
`bouncer init --harness=<name>` handles installation automatically; the details
below are for manual setup or reference.

## Claude Code

`bouncer init --harness=claude_code` does the following:

1. Writes `~/.claude/hooks/bouncer_hook.py` (the wrapper script)
2. Patches `~/.claude/settings.json` with two hooks:
   - `PreToolUse` on `Bash` — core classification hook
   - `UserPromptSubmit` — logs a `·` break marker (a prompt boundary) into
     the project decision log, which the activity strip renders

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

### Statusline

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

The statusline script reads `cwd` from its stdin JSON and passes it to
`bouncer activity` (the strip is scoped to the project, so `cwd` is all it
needs):

```bash
input=$(cat)
cwd=$(echo "$input" | jq -r '.cwd // empty')
bouncer activity --cwd "$cwd" --as ansi 2>/dev/null
```

Activity indicator — one character per recent decision, newest on the left:

| Char | Color | Meaning |
|------|-------|---------|
| Tool initial (B/W/E/R/G…) | green | ALLOW |
| Tool initial | red | DENY |
| Tool initial | magenta | UNSURE |
| Tool initial | cyan | ESCALATE |
| Tool initial | black on magenta | TIMEOUT (LLM too slow) |
| Tool initial | black on red | LLM_ERROR (unreachable, auth, etc.) |
| `·` | dim | Prompt boundary |
| `○` | dim | Bouncer active, no decisions yet |

Tool characters: `B`=Bash, `W`=Write, `E`=Edit, `R`=Read, `G`=Glob/Grep,
`T`=Task, `F`=WebFetch, `S`=WebSearch, `?`=unknown.

`bouncer activity` supports four output formats via `--as <format>`:

- `plain` (default) — bare ASCII, no color codes; safe for any context
- `ansi` — ANSI escape codes; use this for Claude Code's `statusline.sh`
- `json` — structured JSON array `[{"c":"B","d":"allow"},…]`; use this for
  opencode's `commandStrip`, which maps decision values to theme colors
- `tmux` — tmux style segments (`#[fg=green]B#[default]`), suitable for
  `status-left` / `status-right`

The Claude Code statusline and the tmux indicator render from the same source
(the project decision log), so they always show the same thing; the `ansi`
and `tmux` formats just draw it with the same configurable color map:

```yaml
activity:
  colors:
    ALLOW: green
    DENY: red
    UNSURE: magenta
    ESCALATE: cyan
```

Because the strip reads the log, any harness gets the decision strip in tmux —
including Codex, which has no statusline hook. Codex simply shows no `·` break
markers, since it has no `UserPromptSubmit` hook to log them:

```tmux
set -g status-interval 2
set -g status-right '#(bouncer activity --cwd "#{pane_current_path}" --as tmux --width 6 2>/dev/null) #[fg=blue]#{window_width}'
```

## What an abstain reaches

Bouncer can emit **no decision at all** (`on_unsure: abstain`,
`on_unavailable: abstain`), deferring the call to whatever the harness does
on its own. What that actually reaches differs per harness — and, on Claude
Code, per permission mode. The difference matters: it is what decides whether
the `solo` [session profile](configuration.md#session-profiles-live--solo)
can honour a configured `abstain` or has to resolve it to `deny` instead.

| Integration | ASK channel | An abstain reaches | Verified? |
|---|---|---|---|
| Claude Code, `--permission-mode auto` | yes — `permissionDecision: "ask"` | **auto-mode's safety classifier**, which decides without a human | **verified** — see [`auto-mode.md`](auto-mode.md), whose interaction matrix was established empirically, and which has been observed blocking a real call |
| Claude Code, any other mode | yes | **not established** — treated as no floor | not claimed; covered by the allowlist default below |
| opencode (plugin, json) | yes — by abstaining | opencode's **native permission prompt**: a human | inferred from the plugin (a `skip` result leaves the request for the user) |
| Codex `PermissionRequest` | yes — by abstaining | Codex's **normal approval prompt**: a human | inferred from the integration contract |
| Codex legacy `PreToolUse` | no | **nothing** — the call simply runs | inferred from the hook protocol (exit 0, no output) |
| Shell shim (`plain`) | no | **nothing** — the shim prints `allow` | verified from `format_hook_response` |

Read the third column as "who decides if bouncer says nothing". Exactly one
row is a machine. Everywhere else, abstaining either asks a person (fine when
one is there, an ASK by another name when nobody is) or lets the call through
unexamined — which would silently turn "the classifier was unreachable" into
"everything is allowed".

So `solo` does not abstain everywhere:

- **on Claude Code in `auto`**, `solo` resolves `ask` to `abstain` and keeps a
  configured `abstain`, deferring to auto-mode's floor;
- **everywhere else** — Claude Code in any other permission mode, opencode,
  Codex, the shim, and any harness bouncer cannot identify — `solo` resolves
  both to `deny`, and the agent gets the denial back immediately with guidance
  to work around it and report blocked at the end.

Under `solo` bouncer never produces an ASK: not itself, and not by abstaining
into something that would produce one on its behalf.

### How the mode is matched

`auto` is matched from an **allowlist** (`CLASSIFIER_PERMISSION_MODES` in
`bouncer/profile.py`), never against a list of modes to exclude. It is the
one value whose floor was actually observed deciding a call, so it is the one
value that earns an abstain. Everything else falls to the default branch and
denies, including:

- the other modes Claude Code ships today (`default`, `acceptEdits`,
  `bypassPermissions`, `plan`) — bouncer makes **no claim** about where their
  abstain lands, and does not need to, because it never abstains there;
- a mode a future Claude Code version adds, which fails safe rather than
  silently re-opening the stall;
- a payload with no `permission_mode` key at all.

That last branch is load-bearing rather than defensive. `permission_mode` was
present on every Claude Code `PreToolUse` payload captured during the
2026-08-19 recon — across all five `--permission-mode` values and a flagless
headless `claude -p` — but it is not a documented guarantee, an older Claude
Code may not send it, and bouncer's other entry points (the Codex and
opencode bridges, which build their own payloads) never do. All of those
resolve to `deny` under `solo`.

None of this applies under `live`, where the configured `on_unsure` /
`on_unavailable` are used exactly as written in every permission mode. The
mode narrows what `solo` may abstain into; it does not change `abstain`
itself.

### How the harness is identified

Codex and opencode stamp a `harness` field into the payload themselves.
Claude Code does not, and is recognised by the `json` hook format plus a
native `PreToolUse` event. An unidentified harness is treated as floorless.

`bouncer profile` shows what the last classified call in a project reported,
e.g. `harness: claude_code/auto  (abstain → classifier)`.

## OpenAI Codex CLI

`bouncer init --harness=codex` does the following:

1. Copies `bouncer/integrations/codex/bouncer_hook.py` to `~/.codex/hooks/`
2. Patches `~/.codex/hooks.json` with a `PermissionRequest` entry for Bash
3. Removes bouncer's older default `PreToolUse` entry if present

Codex `PermissionRequest` runs when Codex is already about to ask the user for
approval. That matches bouncer's primary purpose: pre-triage approval prompts,
auto-approve policy-compliant actions, deny policy-forbidden actions, and
abstain on UNSURE so Codex shows its normal approval prompt.

To make Codex send more approval requests through bouncer, start Codex with the
stricter approval policy:

```sh
codex --ask-for-approval untrusted
```

To make that persistent for Codex CLI/TUI sessions, add this to
`~/.codex/config.toml`:

```toml
approval_policy = "untrusted"
```

In that mode Codex still runs its trusted command set directly, but non-trusted
shell commands become approval requests that the `PermissionRequest` hook can
pre-triage. This increases observed Codex `Bash` traffic in `bouncer tools`; it
does not expose Codex-internal tools that are not part of the approval request
payload.

The `PermissionRequest` integration is tested working in both the Codex CLI and
the Codex GUI. (The one GUI difference is cosmetic — see the `systemMessage`
note below.)

This is intentionally more permissive than adding a second mandatory approval
layer. Bouncer should save human review for commands that are not obvious to a
simple harness rule but are clear to an LLM with the project policy. See
[`docs/design.md`](design.md) for the general philosophy.

The default Codex wrapper uses `bouncer classify --hook --format
codex-permission`:

- ALLOW → auto-approve the Codex approval request
- DENY → deny the Codex approval request with bouncer's reason
- UNSURE/unavailable/ESCALATE → emit no decision so Codex asks the user

The hook also emits a short `systemMessage` such as `bouncer: ALLOW - ...` as
experimental feedback. Codex Desktop / GUI may not surface that message in the
chat transcript, especially for auto-approved or denied requests. For visible
GUI feedback, configure [`notify.command`](configuration.md#notify) to call a
local notifier, sound, status app, or script.

Codex `PreToolUse` is intentionally not part of the recommended bouncer setup.
It runs before Codex decides whether approval is needed, so bouncer would review
commands that may never have interrupted the user. More importantly, Codex
`PreToolUse` cannot ask the user: `UNSURE`, unavailable, and `ESCALATE` outcomes
cannot use Codex's normal approval prompt from that hook. Codex Desktop / GUI
may also differ from the TUI in whether it runs `PreToolUse`, which makes the
behavior confusing across clients.

Use `PermissionRequest` for Codex. The legacy `codex_pretool` installer remains
available only for experiments or hard-guard debugging.

There is no Codex equivalent of `UserPromptSubmit` or the statusline, so Codex
produces no `·` break markers. The decision strip itself still works in tmux
(it reads the project log) — see the tmux snippet under Claude Code above.

Manual `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "PermissionRequest": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "~/.codex/hooks/bouncer_hook.py",
            "timeout": 30,
            "statusMessage": "Bouncer reviewing approval"
          }
        ]
      }
    ]
  }
}
```

## opencode

`bouncer init --harness=opencode` does the following:

1. Copies `bouncer/integrations/opencode/bouncer_plugin.ts` to `~/.config/opencode/plugin/bouncer.ts`
2. Adds `"bouncer"` to the `plugin` list in `~/.config/opencode/opencode.jsonc`
   if present, otherwise `~/.config/opencode/opencode.json`. If both files
   exist, bouncer warns and leaves the config unchanged so you can merge them
   intentionally.

**Coverage:** opencode's native permission prompts are reviewed through the
plugin event stream. The plugin also observes `tool.execute.before` so it can
attach full tool arguments to the later permission event when opencode includes
the tool call ID. Run `bouncer tools --harness=opencode` for the observed list.

**ASK availability:** opencode ASK is available by abstaining from the native
permission prompt. ALLOW and DENY decisions are answered through opencode's
permission API; UNSURE, unavailable-with-ask, and `# ESCALATE:` outcomes leave
the prompt for the user.

**Reply delay:** by default, bouncer replies as soon as classification
finishes. To leave a short window for the user to answer first, configure a
plugin delay. The delay starts when opencode emits the permission request: if
classification finishes before the delay, bouncer waits out the remainder; if
classification takes longer, bouncer replies immediately when ready.

```json
{
  "plugin": [["bouncer", { "replyDelayMs": 10000 }]]
}
```

Manual `~/.config/opencode/opencode.jsonc` or `opencode.json`:

```json
{
  "plugin": ["bouncer"]
}
```

### Activity strip

opencode's layout system supports a `commandStrip` option that runs a shell
command on an interval and displays the output in the bottom-right of the input
area. Use `--as json` so opencode can render each character in the correct theme
color:

```jsonc
// in your layout .jsonc file
"commandStrip": {
  "command": "bouncer activity --cwd {cwd} --as json",
  "intervalMs": 3000
}
```

The JSON format emits `[{"c":"B","d":"allow"},…]`; opencode maps decision
values to theme colors (`allow`→success, `deny`→error, `unsure`→warning,
`escalate`→info, `break`/`muted`→textMuted).

## Shell shim (universal gate)

For AI coding agents without a native hook system (Cline, Roo Code, Continue,
Gemini CLI, etc.), bouncer ships a small bash shim that intercepts `bash -c
"<command>"` invocations and routes them through `bouncer classify`.

### How it works

The shim intercepts `bash -c "<command>"`, passes the command to `bouncer
classify` (plain-text protocol), and runs the real `/bin/bash` only when
bouncer returns `allow`. It also:

- **Walks up** from `$PWD` looking for `.bouncer/config.yaml` — if no project
  config is found, the shim passes through without spawning bouncer.
- **Recursion guard** via `BOUNCER_INTERNAL_ACTIVE`: once a command has been
  classified, the sub-shells it spawns are not re-intercepted.

The shim does not have ASK available — it cannot prompt the user mid-command.
Both the internal UNSURE→ASK path and the `# ESCALATE:` path are delivered to
the agent as denials with the reason on stderr.

### Install

```sh
bouncer -g init --harness=shim
```

This copies the shim to `~/.local/share/bouncer/shim/bash` and prints the
activation instructions.

For manual install:

```sh
mkdir -p ~/.local/share/bouncer/shim
cp bouncer/shim/bash ~/.local/share/bouncer/shim/bash
chmod +x ~/.local/share/bouncer/shim/bash
```

### Activate

Prefix the agent's launch command so the shim is first on PATH for that
process only. Nothing global to your login shell needs to change.

```sh
PATH="$HOME/.local/share/bouncer/shim:$PATH" <your-agent-command>
```

For a VS Code extension (Cline, Roo Code, Continue), launch VS Code itself
with the prefixed PATH so the extension inherits it:

```sh
PATH="$HOME/.local/share/bouncer/shim:$PATH" code
```

Some extensions let you set the "Shell Path" directly. Point it at
`~/.local/share/bouncer/shim/bash` if that's cleaner for your setup.

### Verify

```sh
# 1. Ensure bouncer is active
bouncer status

# 2. Try a risky command
PATH="$HOME/.local/share/bouncer/shim:$PATH" bash -c "rm -rf /"
```

Expected:

```
Bouncer DENIED: <reason>. This harness does not have ASK available. Find
another way or suggest a policy change. Run 'bouncer --agent-help' if you
haven't already.
```

### Limitations

- **Bash-only.** Only `bash -c "<command>"` invocations are gated. An agent
  that writes files directly via its own tool machinery (not through a
  shell) won't go through the shim — use a native integration for broader
  coverage.
- **ASK is not available.** The shim cannot interactively prompt the user.
  Internal ASK outcomes are delivered outward as denials, so the agent should
  find another way or suggest a policy change.
- **Not a replacement for harness-level approval.** YOLO-mode agents
  benefit most: the shim substitutes bouncer for the (absent) user approval.
  In ask-first mode, the shim is a redundant second gate.
