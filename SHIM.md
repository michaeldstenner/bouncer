# Bouncer Shell Shim

A transparent wrapper for `bash` that enables Bouncer protection for any AI
coding agent that executes commands via the shell (Cline, Roo Code, Continue,
Gemini CLI, etc.).

## How it works

The shim intercepts `bash -c "<command>"`, passes the command to `bouncer
classify` (plain-text protocol), and runs the real `/bin/bash` only when
bouncer returns `allow`. It also:

- **Walks up** from `$PWD` looking for `.bouncer/config.yaml` — if no project
  config is found, the shim passes through without spawning bouncer.
- **Recursion guard** via `BOUNCER_INTERNAL_ACTIVE`: once a command has been
  classified, the sub-shells it spawns are not re-intercepted.

The shim has no ASK channel — it cannot prompt the user mid-command. Both
the UNSURE→ASK path and the `# ESCALATE:` path surface to the agent as a
non-zero exit with the reason on stderr. Tune `on_unsure` / `on_unavailable`
in config to match the environment's risk posture.

## Install

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

## Activate

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

## Verify

```sh
# 1. Ensure bouncer is active
bouncer status

# 2. Try a risky command
PATH="$HOME/.local/share/bouncer/shim:$PATH" bash -c "rm -rf /"
```

Expected:

```
Bouncer DENIED: <reason>. To escalate to the user: prefix your command
with `# ESCALATE: <reason>` and retry. Run 'bouncer --agent-help' if you
haven't already.
```

## Limitations

- **Bash-only.** Only `bash -c "<command>"` invocations are gated. An agent
  that writes files directly via its own tool machinery (not through a
  shell) won't go through the shim — use a native integration for broader
  coverage.
- **No ASK channel.** The shim cannot interactively prompt the user. If your
  policy tends to produce UNSURE verdicts, configure `on_unsure: deny` (or
  `deny_with_message`) so the agent gets a clean denial plus an `ESCALATE`
  hint, and can then ask the user itself.
- **Not a replacement for harness-level approval.** YOLO-mode agents
  benefit most: the shim substitutes bouncer for the (absent) user approval.
  In ask-first mode, the shim is a redundant second gate.

## Why a shim?

While Bouncer has native integrations for Claude Code, Codex, and opencode,
many agents don't yet provide a hook system. The shim is a universal fallback
for any agent that runs commands through a standard shell.
