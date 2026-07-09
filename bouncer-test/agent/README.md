# Agent-based bouncer tests

These exercise the **harness ↔ bouncer protocol** end to end — the thing the
unit suite (`test_bouncer.py`) and the isolated smoke tests (`bouncer-test/`)
can't see: that a real agent's tool call actually reaches bouncer's hook and
that the harness honors the verdict (ALLOW runs, DENY blocks, plumbing skips).

## How it stays deterministic

Agents and LLMs are nondeterministic twice over. We remove the bouncer-layer
nondeterminism with a **stub LLM** (`stub_llm.py`): a tiny OpenAI-compatible
server that maps a sentinel token in the tool call to a fixed verdict.

| sentinel | verdict |
|---|---|
| `BNCR_DENY` | DENY |
| `BNCR_UNSURE` | UNSURE |
| `BNCR_ALLOW` | ALLOW |

A disposable project `.bouncer/config.yaml` points bouncer's `openai_compatible`
provider at the stub, so every verdict is scripted. The sentinel lives in the
command's path (e.g. `touch {OUT}/BNCR_ALLOW_ab12`), so it is simultaneously the
stub key and a filesystem ground-truth flag.

The agent's own judgment is still a gate above bouncer — keep prompts explicit
("run exactly this command") and re-run on an INCONCLUSIVE.

## Scenarios

One matrix in `scenarios.yaml` feeds both the automated runner and the manual
runbook, so they never drift. Slice scenarios: `allow`, `deny`, and
`internal_skip` (Claude-only — a `ToolSearch` call must be skipped, never
classified).

## Running

```sh
# Fast, deterministic, no agent: synthesizes payloads and drives
# `bouncer classify --hook`. Proves stub + tools-fold SKIP + allow/deny protocol.
uv run python bouncer-test/agent/run.py

# The real slice: drives a live Claude Code session in tmux and observes the
# same decisions. Needs `claude` and `tmux` on PATH.
uv run python bouncer-test/agent/run.py --agent claude

# Codex CLI slice: drives `codex exec` through the PermissionRequest hook.
# Needs `codex` authenticated and bouncer's Codex hook installed/trusted.
uv run python bouncer-test/agent/run.py --agent codex_cli

# Stub alone, for manual poking of any harness:
uv run python bouncer-test/agent/stub_llm.py --port 8900
```

Each run triangulates three channels (per the project's "driving CLI agents in
tmux" pattern): the bouncer **decision log** (authoritative), the
**filesystem** (did the command run?), and the captured **pane** (context).

## Coverage and the Codex GUI gap

| Harness | How |
|---|---|
| `classify --hook` | automated (`run.py`, hook mode) |
| Claude Code | automated (`run.py --agent claude`, headless `claude -p`) |
| Codex CLI | automated (`run.py --agent codex_cli`, headless `codex exec`) |
| Codex TUI | tmux driver (planned, `drivers/claude_tmux.py` pattern) |
| **Codex GUI** | **manual** — see below |

Headless/`-p` drivers cover the CLIs; the tmux pattern covers a TUI that has no
headless mode; only the **Codex GUI** (not a terminal program) needs a human.
For it, run the same matrix by hand:

## Live-run notes (`--agent claude`)

The Claude driver uses **`claude -p`** (headless), not a TUI — it's genuinely
non-interactive, surfaces hook output in full, and has no readiness/submit
races. The verified pipeline: bouncer's hook fires → the stub returns the
scripted verdict → ALLOW creates the flag (log shows `ALLOW`), DENY blocks it
(no flag), confirmed end to end with a direct `claude -p` run.

**Run `--agent` from a plain terminal, not nested inside another Claude Code
session.** Claude Code sandboxes the Bash commands it runs; a `claude -p` spawned
*underneath* a sandboxed Claude session inherits that sandbox, and its tool
writes get blocked by the harness's write-guard *before* the bouncer hook even
fires (the pane shows "blocked by the session sandbox … allowed working
directories", and no decision is logged). A direct `claude -p` from a normal
shell has no such outer sandbox and works. If you must run it nested, point
`--work-root` at a sandbox-allowed dir — but the cleaner answer is a real
terminal.

Other gotchas:

- **`--work-root`** sets the disposable project's parent (default: system
  temp). On macOS, system temp is `/var/folders/...`, which a surrounding
  sandbox usually disallows; `/tmp` is more often permitted.
- **The agent's own judgment is still a gate** above bouncer — keep prompts
  explicit ("run exactly this command") and re-run on an INCONCLUSIVE.
- **Debugging:** `run.py` dumps the agent output to `<work>/project/<id>.pane`
  on any inconclusive result.
- `drivers/claude_tmux.py` (interactive TUI driver) is kept for the harnesses
  that have no `-p` equivalent, e.g. **Codex TUI**.

### Codex GUI runbook (manual)

1. Start the stub: `uv run python bouncer-test/agent/stub_llm.py --port 8900`.
2. In a disposable project, create `.bouncer/config.yaml` with
   `llm: {provider: openai_compatible, model: stub-model, url: http://127.0.0.1:8900}`
   and `tools: [+@all, -@internal]`, plus a one-line `policy.md`.
3. Open that project in the Codex GUI with the bouncer Codex integration active.
4. For each row of `scenarios.yaml`, paste: *"Run exactly this shell command and
   nothing else: `touch /tmp/bncr-out/BNCR_ALLOW_gui1`"* (swap the sentinel per
   row).
5. Verify three channels:
   - **stub** consulted: `curl -s localhost:8900/requests`
   - **bouncer log**: `bouncer log` from the project — decision matches the row
   - **filesystem**: the flag file exists (ALLOW) or not (DENY)
6. Record pass/fail per row. (Give Codex's own LLM the same explicit
   "run exactly this command" instruction so its judgment doesn't interfere.)
