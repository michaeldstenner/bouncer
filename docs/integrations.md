# Integrations

Bouncer supports four integration targets. Harness-specific hooks live in
`integrations/<harness>/` in this repo; the universal shell shim lives in
`bouncer/shim/bash`. `bouncer init --harness=<name>` handles installation
automatically; the details below are for manual setup or reference.

## Claude Code

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

The statusline script must read `session_id` and `cwd` from its stdin JSON
and pass them to `bouncer activity`:

```bash
input=$(cat)
session_id=$(echo "$input" | jq -r '.session_id // empty')
cwd=$(echo "$input" | jq -r '.cwd // empty')
bouncer activity --session "$session_id" --cwd "$cwd" --as ansi 2>/dev/null
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

Colors for `ansi` and `tmux` output are driven by the same config map so the
Claude Code statusline and tmux indicator stay synchronized:

```yaml
activity:
  colors:
    ALLOW: green
    DENY: red
    UNSURE: magenta
    ESCALATE: cyan
```

For Codex, which does not currently expose a statusline hook, tmux can render
the project log for the current pane:

```tmux
set -g status-interval 2
set -g status-right '#(bouncer activity --cwd "#{pane_current_path}" --project --as tmux --width 6 2>/dev/null) #[fg=blue]#{window_width}'
```

## OpenAI Codex CLI

`bouncer init --harness=codex` does the following:

1. Copies `integrations/codex/bouncer_hook.py` to `~/.codex/hooks/`
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

There is no Codex equivalent of `UserPromptSubmit` or the statusline.

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

1. Copies `integrations/opencode/bouncer_plugin.ts` to `~/.config/opencode/plugin/bouncer.ts`
2. Adds `"bouncer"` to the `plugin` list in `~/.config/opencode/opencode.json`

**Coverage:** opencode's `tool.execute.before` fires for all built-in tools
(bash, read, edit, write, apply_patch) — broader than Codex's Bash-only scope.
The plugin passes opencode's tool name through as-is; run `bouncer tools
--harness=opencode` for the observed list.

**ASK availability:** opencode does not have ASK available. When bouncer's
internal decision is ASK (from an UNSURE LLM verdict or `# ESCALATE:`), the
plain-format hook delivers it outward as a deny with guidance to find another
way or suggest a policy change.

Manual `~/.config/opencode/opencode.json`:

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
  "command": "bouncer activity --session {session_id} --cwd {cwd} --as json",
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
