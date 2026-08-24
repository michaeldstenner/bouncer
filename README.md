# bouncer

![bouncer](bouncer.png)

LLM-powered permission classifier for AI coding agents. Reviews pending
approval requests against plain-English policy so routine, policy-compliant
actions can proceed without repeatedly asking the user.

Bouncer is **opt-in per project**: it only activates when `.bouncer/config.yaml`
exists somewhere in the directory tree above the working directory.
Without that, the harness just follows its default behavior.

---

## Quickstart

Happy path — Claude Code, Codex, or opencode with a local Ollama model:

```sh
uv tool install .               # from a clone; puts `bouncer` on PATH
bouncer -g init                 # wire detected harnesses + write user config
cd my-project && bouncer init   # turn bouncer on for this project
```

Restart your agent in that project. Done — bouncer now reviews the commands
your harness would have stopped to ask you about.

**One prerequisite: an LLM backend.** The user config that `bouncer -g init`
writes points at local [Ollama](https://ollama.com) with `qwen3:32b`:

```sh
brew install ollama        # or see https://ollama.com/download
ollama serve &             # if it isn't already running
ollama pull qwen3:32b
```

Prefer a smaller model, or OpenAI / Anthropic / an OpenAI-compatible endpoint?
See [Customize](#customize).

## Customize

Everything above runs on defaults. The common adjustments:

- **Describe the project for the classifier** (recommended). `bouncer policy`
  opens `.bouncer/policy.md` in `$EDITOR`. The more specific the policy, the
  fewer `UNSURE` verdicts — see the **Policy** section below.
- **Change the model or provider.** `bouncer -g config` opens the user config.
  `qwen3:32b` gives good judgment but is a large (~20 GB) download; a smaller
  instruct model such as `qwen3:4b` pulls faster and is fine for a first try.
  Set `llm.model` to match. OpenAI, Anthropic, and OpenAI-compatible endpoints
  are also supported — see [docs/configuration.md](docs/configuration.md).
- **Wire a specific harness.** `bouncer -g init` detects and offers installed
  harnesses interactively; pass `bouncer init --harness=<name>` to add one
  explicitly. See [docs/integrations.md](docs/integrations.md).

Requirements: Python 3.11+ and an LLM backend; the package itself has zero
third-party dependencies. Harness hooks need `bouncer` on `PATH` in fresh
subprocesses — `uv tool install` or `pipx install` handles that. A plain
`pip install -e .` only works if the agent starts from that same activated
environment.

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
  shell-invoking agent (via the universal shim) share one policy and one log,
  with activity output you can render in a statusline, tmux, notifier, or
  custom script.
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

Every decision is logged. Optional activity output can be rendered wherever it
fits your workflow: a Claude Code statusline, opencode command strip, tmux
status bar, local notifier, or custom script.

### Choosing which tools bouncer reviews

The `tools:` setting controls which tool calls bouncer classifies; anything
outside the set is **skipped** — bouncer expresses no opinion and the harness's
own permission flow decides. It is an **ordered list of `±` operations** folded
left-to-right over a running set:

- `+X` — review tools matching `X`; `-X` — skip them. The **last matching op
  wins**.
- `X` may be a tool name (`Bash`), a glob (`mcp__google_workspace__*`), `@all`
  (every tool), or a **group** such as `@internal`.
- `all` is shorthand for `+@all -@internal` — every tool *except* harness
  plumbing.

```yaml
tools: [+@all, -@internal]   # the default: everything except plumbing
tools: [-Read]               # default set, but stop reviewing Read
tools: [-@all, +Bash]        # only Bash (absolute)
```

The configs layer **user → project `config.yaml` → `config.local.yaml`**, each
folding onto the one before (a list of bare names like `[Bash]` is legacy
shorthand for `[-@all, +Bash]`, i.e. "only Bash").

**`@internal` is harness plumbing** — discovery/meta tools that have no side
effects and that the harness already auto-allows, so reviewing them only wastes
an LLM call (and risks a spurious denial). Claude Code's `ToolSearch` — the
deferred-tool schema loader — is the canonical member. Skipping it is free:
loading a tool's schema is not the same as calling it, and the actual call still
hits bouncer's gate on its own.

Groups are editable with the same `±` algebra, so you can re-scope what counts
as plumbing:

```yaml
groups:
  internal: -ToolSearch        # re-gate ToolSearch everywhere
  internal: [+TaskGet, +TaskList]   # also treat these as skippable
```

`bouncer lint` prints the resolved op list and flags the deprecated bare `all`.

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

The two can overlap, and for **Claude Code auto-mode** we tested exactly how.
bouncer's `PreToolUse` hook runs *upstream of* auto-mode and is authoritative:
when bouncer returns ALLOW or DENY, auto-mode's classifier is never consulted;
auto-mode only weighs in when bouncer abstains (UNSURE → ask). A practical
consequence — since a bouncer ALLOW is authoritative, a permissive policy can
pass actions auto-mode would otherwise block; they are not additive
defense-in-depth on the allow side. The two compose well in practice: bouncer
carries your project's specific, plain-English boundaries and anything it leaves
ambiguous defers to auto-mode. Full pipeline, matrix, and guidance:
[docs/auto-mode.md](docs/auto-mode.md).

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
Harnesses that do not have ASK available (the shell shim, for instance)
deliver the escalation outward as a denial instead — and so does a session
running under the `solo` profile, below.

### Session profiles (`live` / `solo`)

Escalation only makes sense if somebody is there to answer. The session
profile says whether anybody is:

```sh
bouncer profile          # show the effective profile for this project
bouncer profile solo     # this agent is running alone
bouncer profile live     # a human is on the line
```

Under **`live`** (the default) escalation works and `on_unsure` /
`on_unavailable` are used as written. Under **`solo`** no ASK is ever
produced — not by bouncer, and not by deferring to something that would ask
on its behalf. Escalation is refused up front, so an agent gets its denial
back immediately instead of hanging on a prompt nobody will answer; and the
unsure/unavailable fallbacks defer only where the fall-through is known to
decide without a human. In practice that is Claude Code running in `auto`
permission mode, whose floor is auto-mode's own classifier. Everything else
denies.

A profile changes the plumbing, not the judgment: `policy.md` remains the only
thing that decides ALLOW/DENY. The profile is per-project file state, so it
can be changed mid-session and read by a status line:

```tmux
set -g status-right '#(bouncer profile --cwd "#{pane_current_path}" --as tmux) #(bouncer activity --cwd "#{pane_current_path}" --as tmux --width 6)'
```

Green `live`, amber `solo`, and inverted amber when a `live` profile is
degraded because the harness has no way to ask anyone. See
[docs/configuration.md](docs/configuration.md#session-profiles-live--solo).



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
bouncer tools                               # documented + observed harness tool names

bouncer review          # cluster new decisions and draft policy improvements
bouncer review --since 14d      # override the review cursor
bouncer review --all-history    # review the full retained project log

bouncer profile         # show the effective session profile (live / solo)
bouncer profile solo    # switch this project to solo (no ASK is produced)
```

Use `bouncer -g <cmd>` to operate on user-scope data instead of the project:

```sh
bouncer -g log     # global log (all projects)
bouncer -g review  # review cross-project evidence for user policy
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

**Review:** `bouncer review` sends new detailed decision-log requests to a
separately configured, tool-less model. It semantically clusters the requests,
asks you whether each cluster should be allowed, denied, or treated as a
one-off, then opens a proposed policy revision in `$EDITOR`. Saving the draft
does not immediately apply it: bouncer shows the exact final diff and asks once
more before writing. See [Policy review](docs/operations.md#policy-review).

---

## Documentation

- [docs/design.md](docs/design.md) — design philosophy: bouncer as a
  permissive reviewer for existing approval prompts, not an extra lockdown
  layer.
- [docs/configuration.md](docs/configuration.md) — `config.yaml` schema,
  LLM providers, tools filter, fallback behavior, session profiles, merge
  order, custom system prompt.
- [docs/integrations.md](docs/integrations.md) — per-harness install and
  manual setup (Claude Code, Codex, opencode, shell shim), and what an
  abstain reaches on each.
- [docs/auto-mode.md](docs/auto-mode.md) — how bouncer composes with Claude
  Code's auto-mode: the permission pipeline, precedence, and the full
  interaction matrix.
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
