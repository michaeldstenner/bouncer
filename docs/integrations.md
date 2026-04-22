# Integrations

Bouncer supports four integration targets. Harness-specific hooks live in
`integrations/<harness>/` in this repo; the universal shell shim lives in
`bouncer-shim/`. `bouncer init --harness=<name>` handles installation
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
bouncer activity --session "$session_id" --cwd "$cwd" 2>/dev/null
```

Activity indicator — one character per recent decision, newest on the left:

| Char | Color | Meaning |
|------|-------|---------|
| Tool initial (B/W/E/R/G…) | green | ALLOW |
| Tool initial | black on red | DENY |
| Tool initial | yellow | UNSURE |
| Tool initial | cyan | ESCALATE |
| `·` | dim | Prompt boundary |
| `○` | dim | Bouncer active, no decisions yet |

Tool characters: `B`=Bash, `W`=Write, `E`=Edit, `R`=Read, `G`=Glob/Grep,
`T`=Task, `F`=WebFetch, `S`=WebSearch, `?`=unknown.

## OpenAI Codex CLI

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

## opencode

`bouncer init --harness=opencode` does the following:

1. Copies `integrations/opencode/bouncer_plugin.ts` to `~/.config/opencode/plugin/bouncer.ts`
2. Adds `"bouncer"` to the `plugin` list in `~/.config/opencode/opencode.json`

**Coverage:** opencode's `tool.execute.before` fires for all built-in tools
(bash, read, edit, write, apply_patch) — broader than Codex's Bash-only scope.
`apply_patch` maps to `Write` for the bouncer tools filter.

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
cp bouncer-shim/bash ~/.local/share/bouncer/shim/bash
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
