# bouncer

![bouncer](bouncer.png)

LLM-powered permission classifier for AI coding agents. Reviews pending
approval requests against plain-English policy so routine, policy-compliant
actions can proceed without repeatedly asking the user.

Bouncer is **opt-in per project**: it only activates when `.bouncer/config.yaml`
exists somewhere in the directory tree above the working directory.

---

## What it's for

The philosophy is "manage risk without nagging" — not "lock down the agent."
Most coding harnesses already ask before operations they consider risky, and
that conservative default is useful but exhausting. Bouncer is pre-triage for
those approval prompts: write your project's norms in plain English once, and
let an LLM apply that context to each pending tool call.

Bouncer is not meant to make AI coding more restrictive than the harness would
be on its own. It is meant to make the existing approval loop more efficient:
policy-compliant actions can be approved automatically, policy-forbidden
actions can be denied with an explanation, and genuinely ambiguous actions
still go to the user when the harness supports an ask path.

That matters for one-off commands that do not fit useful glob rules. A blanket
approval for `pkill *` or arbitrary inline Python would be reckless, but a
classifier can inspect the specific command, infer intent, compare that intent
to policy, and avoid asking the user when the answer is already clear.

Good fits:

- **Work that resists sandboxing** — infrastructure, system scripts,
  network-reaching tasks, anything that touches outside the repo.
- **Multi-harness workflows** — Claude Code, Codex CLI, opencode, and any
  shell-invoking agent (via the universal shim) share one policy, one log,
  one activity strip.
- **Local-first setups** — defaults to Ollama; also supports OpenAI,
  Anthropic, and any OpenAI-compatible endpoint.
- **Rules that depend on intent, not just the command text** — "migrations
  are fine in dev," "the deploy script should only run in CI," "stay out of
  `~/Documents`." Conditions like these are easy to state in plain English but
  awkward to capture as path globs or regex allowlists.

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
| **UNSURE** | LLM couldn't decide — internal ASK if available; otherwise delivered outward as a deny |

If the LLM is unreachable, the `on_unavailable` fallback applies (default: ask).

Every decision is logged and shown in the statusline activity strip.

### Harness auto-approval vs. bouncer

Some harnesses have their own auto-approval mode. Codex has an auto-review mode
that routes approval prompts to its built-in reviewer; Claude Code has an
auto-accept ("auto") mode that stops prompting for edits and commands it
considers safe. In both cases the harness decides with its own built-in logic.

Bouncer fills a different role: it is a local, policy-controlled reviewer. The
decision context comes from your user policy, project policy, local-only policy
overrides, selected LLM provider/model, fallback settings, logs, activity
indicators, and notifier hooks.

The practical distinction:

- **Harness auto-approval:** "Does the harness's built-in reviewer think this
  is okay?"
- **Bouncer:** "Does this action match my policy for this project, using my
  chosen reviewer and feedback channels?"

The two can overlap. When testing bouncer, leave the harness's own
auto-approval off unless you are intentionally comparing both reviewers;
otherwise you may see extra latency, token use, or confusing approval behavior.

Bouncer's Codex integration is tested working in both the Codex CLI and the
Codex GUI.

### Escalation mechanism

If the harness has ASK available and the agent receives a DENY it believes is
wrong, it retries the exact same command prefixed with `# ESCALATE:`:

```sh
# ESCALATE: clearing build artifacts before release
rm -rf dist/ build/
```

Bouncer skips the LLM and forwards the request to the user with the stated
reason. This is a retry mechanism — the agent should submit normally first and
only escalate after a denial, not preemptively.

`ESCALATE` is a request, not an override: bouncer still defers to the user.
Harnesses that do not have ASK available (the shell shim and opencode, for
instance) deliver the escalation outward as a denial instead.

---

## Setup

**Requirements:** Python 3.11+ (no third-party Python dependencies) **and** an
LLM backend — either a local [Ollama](https://ollama.com) model or an API key
for a hosted provider (OpenAI, Anthropic, or any OpenAI-compatible endpoint).
The Python package has zero dependencies, but bouncer still needs a model to
classify against; see step 2.

### 1. Install the CLI

Recommended — `uv tool install` puts `bouncer` on your PATH globally, which the
harness hooks need:

```sh
uv tool install --editable /path/to/bouncer
bouncer --help        # verify
command -v bouncer    # confirm it's on PATH
```

> **Why global PATH matters:** harness hooks invoke a bare `bouncer` command in
> a fresh subprocess. If you install into a virtualenv that isn't active when
> your agent launches, the hooks silently fail to find `bouncer` and everything
> passes through. `uv tool install` (or `pipx install`) avoids this. A plain
> `pip install -e .` only works if the agent is started from that same
> activated environment.

### 2. Pick an LLM backend

Bouncer ships with the Ollama provider configured by default, but there is no
built-in default *model* — you choose one.

**Local (Ollama) — no API key:**

```sh
brew install ollama        # or see https://ollama.com/download
ollama serve &             # if it isn't already running
ollama pull qwen3:32b      # the shipped default model
```

`qwen3:32b` gives good judgment but is a large (~20 GB) download. A smaller
instruct model such as `qwen3:4b` pulls faster and works fine for a first
try — just set `llm.model` to match (step 3).

**Hosted (OpenAI / Anthropic / OpenAI-compatible):** skip Ollama entirely. Set
the `llm:` section to your provider and supply an API key via env var,
`llm.api_key`, or a provider section in `~/.config/bouncer/config.yaml`. See
[docs/configuration.md](docs/configuration.md#llm-providers).

### 3. One-time user setup

Creates `~/.config/bouncer/` and wires harness hooks:

```sh
bouncer -g init       # detect harnesses + offer to wire hooks (incl. shim)
bouncer -g config     # set your LLM provider + model
bouncer -g policy     # add any personal norms
bouncer status -v     # confirm config + LLM reachability
```

### 4. Per-project setup

```sh
cd your-project
bouncer init --harness=auto   # create .bouncer/ + wire detected harnesses
bouncer policy                # describe the project for the LLM
bouncer status                # confirm it's active
```

`--harness=auto` detects whichever AI coding harnesses are installed and wires
them automatically. Pass a specific name (`claude_code`, `codex`, `opencode`,
`shim`) to target one harness, or omit `--harness` entirely to skip hook
wiring. `--harness=all` installs every known target, including the universal
shim. See [docs/integrations.md](docs/integrations.md) for per-harness details.

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

- [docs/design.md](docs/design.md) — design philosophy: bouncer as a
  permissive reviewer for existing approval prompts, not an extra lockdown
  layer.
- [docs/alpha-quickstart.md](docs/alpha-quickstart.md) — short setup path
  for trusted alpha users using shared local config.
- [docs/configuration.md](docs/configuration.md) — `config.yaml` schema,
  LLM providers, tools filter, fallback behavior, merge order, custom
  system prompt.
- [docs/integrations.md](docs/integrations.md) — per-harness install and
  manual setup (Claude Code, Codex, opencode, shell shim).
- [docs/operations.md](docs/operations.md) — command reference, file
  layout, log format, internals, running tests.

---

## Releases

### 0.1.0 (2026-05-07)

Initial alpha release.

- LLM-powered ALLOW/DENY/UNSURE classification for Bash and other tools
- Claude Code, Codex, opencode, and universal shell shim integrations
- Ollama, OpenAI, Anthropic, and OpenAI-compatible provider support
- Per-project and user-level policy with `append`/`replace` modes
- `bouncer log`, `bouncer review`, `bouncer abort`, `bouncer check`
- Activity strip for Claude Code and opencode statuslines
- Zero third-party dependencies
