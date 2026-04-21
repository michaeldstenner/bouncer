# bouncer

LLM-powered permission classifier for AI coding agents. Intercepts
requests for human approval to provide an LLM-driven risk analysis,
based on project scope and operation.

![bouncer demo](docs/demo.gif)

Bouncer is **opt-in per project**: it only activates when `.bouncer/config.yaml`
exists somewhere in the directory tree above the working directory.

---

## What it's for

The philosophy is "manage risk without nagging" — not "lock down the agent."
Per-action prompts are safe but exhausting; blanket auto-approve is blind;
sandboxing is provably safe but rules out real work. Bouncer aims for a
middle: write your project's norms in plain English once, and let an LLM
apply that context to each tool call as it arrives.

Good fits:

- **Work that resists sandboxing** — infrastructure, system scripts,
  network-reaching tasks, anything that touches outside the repo.
- **Multi-harness workflows** — Claude Code, Codex CLI, opencode, and any
  shell-invoking agent (via the universal shim) share one policy, one log,
  one activity strip.
- **Local-first setups** — defaults to Ollama; also supports OpenAI,
  Anthropic, and any OpenAI-compatible endpoint.
- **Context-dependent risk** — the deploy script is CI-only, migrations
  are fine in dev, `~/Documents` is off-limits. Easier to describe in
  markdown than to encode as regex allowlists.

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

### Escalation mechanism

If the agent wants to bypass the LLM and send a request straight to the user,
it repeats the command prefixed with `# ESCALATE:`:

```sh
# ESCALATE: clearing build artifacts before release
rm -rf dist/ build/
```

Bouncer skips the LLM and forwards the request to the user with the stated
reason. This is how the agent signals "I know this looks sketchy — here's
why I need it" without permanently widening the policy.

`ESCALATE` is a request, not an override: bouncer still defers to the user,
and harnesses that have no ASK channel (the shell shim, for instance) will
surface the escalation back as a denial for the agent to relay.

---

## Setup

**Requirements:** Python 3.11+, no third-party dependencies.

```sh
pip install -e /path/to/bouncer
bouncer --help   # verify
```

Or run without installing: `python3 -m bouncer <command>`.

Then, in your project:

```sh
cd your-project
bouncer init --harness=auto   # init .bouncer/ + auto-detect and wire harness hooks
bouncer policy                # describe the project for the LLM
bouncer config                # adjust settings if needed
bouncer status                # confirm it's active
```

`--harness=auto` detects whichever AI coding harnesses are installed and wires
them automatically. Pass a specific name (`claude_code`, `codex`, `opencode`,
`shim`) to target one harness, or omit `--harness` entirely to skip hook
wiring. `--harness=all` installs every known target, including the universal
shim. See [docs/integrations.md](docs/integrations.md) for per-harness details.

### User-level defaults (optional)

Settings and policy applied here apply to all bouncer-enabled projects:

```sh
bouncer -g config   # ~/.config/bouncer/config.yaml
bouncer -g policy   # ~/.config/bouncer/policy.md
```

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

Full command reference: [docs/operations.md](docs/operations.md).

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

## Documentation

- [docs/configuration.md](docs/configuration.md) — `config.yaml` schema,
  LLM providers, tools filter, fallback behavior, merge order, custom
  system prompt.
- [docs/integrations.md](docs/integrations.md) — per-harness install and
  manual setup (Claude Code, Codex, opencode, shell shim).
- [docs/operations.md](docs/operations.md) — command reference, file
  layout, log format, internals, running tests.
