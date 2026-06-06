# bouncer and Claude Code auto-mode

Claude Code's **auto-mode** (a research-preview permission mode) runs tools
without prompting, vetting each call with an internal safety classifier that
blocks catastrophic actions (mass deletion, data exfiltration, malicious code)
and otherwise lets work proceed. bouncer is a `PreToolUse` hook. This document
records how the two interact — established empirically against Claude Code
2.1.167, not from documentation.

## The permission pipeline

A tool call passes through three gates, in order:

```
model's own judgment  →  PreToolUse hook (bouncer)  →  auto-mode classifier
```

1. **Model judgment.** Claude may decline to emit a call at all (e.g. it
   refused a bare `rm -rf` of a home directory until given explicit context).
   This sits above everything and is **nondeterministic** — the same prompt
   ran in one session and was refused in another.
2. **bouncer (the hook).** If bouncer returns an explicit `ALLOW` or `DENY`,
   that *is* the permission decision for the call.
3. **auto-mode classifier.** Only consulted when bouncer **abstains**
   (`UNSURE` → ask, or the tool isn't in bouncer's intercept list).

## Precedence: bouncer is authoritative

When bouncer returns a decision, **auto-mode is never consulted.** Verified from
session transcripts: no auto-mode decision event appears for any call bouncer
decided.

- **bouncer DENY** → the call is blocked at the hook; auto-mode never runs.
- **bouncer ALLOW** → the call executes; auto-mode gets **no veto**, even for a
  command whose shape matches auto-mode's own block rules (tested with an
  explicitly-allowed `rm -rf` of a non-existent path — bouncer logged ALLOW and
  the command ran, with no auto-mode involvement).

## The interaction matrix

| bouncer says | auto-mode | Result |
|---|---|---|
| **DENY** | *not consulted* | Blocked by bouncer |
| **ALLOW** | *not consulted* | Runs — even if auto-mode would have blocked it |
| **UNSURE → ask** | auto-approves | Runs (no prompt) |
| **UNSURE → ask** | blocks | Blocked by auto-mode |

The first two rows are confirmed. The `UNSURE → ask` handoff is not yet pinned
down empirically; auto-mode is by design somewhat more permissive than a tuned
bouncer policy, so deferred-ambiguous calls are expected to mostly auto-approve.

## What this means in practice

Because a bouncer **ALLOW is authoritative**, bouncer can both *tighten* and
*loosen* relative to auto-mode. A permissive bouncer policy will pass actions
auto-mode would otherwise block, since auto-mode is bypassed whenever bouncer
has an opinion. **They are not additive defense-in-depth on the allow side** —
where bouncer decides, bouncer's judgment replaces auto-mode's.

Pick the composition you want:

- **Want auto-mode's catastrophic-action floor to stay in force?** Keep
  bouncer's ALLOW set conservative and let genuinely ambiguous calls fall
  through (`UNSURE` → ask) so auto-mode still gets to weigh in.
- **Want bouncer fully in charge?** A confident, specific policy means bouncer
  decides most calls and auto-mode rarely matters.

The roles are complementary: bouncer provides **fine-grained, natural-language,
per-project boundaries** (with a local/private model, cross-harness, fully
logged); auto-mode provides a generic, zero-config floor plus a keep-going
nudge. Running them together works well — bouncer encodes your project's
specific rules, and anything it leaves ambiguous defers to auto-mode.

## How this was determined

The interaction was tested by driving a fresh Claude Code session in a detached
`tmux` window (auto-mode on) inside a disposable project with a deterministic
bouncer policy, then observing three signals: the TUI (`tmux capture-pane`),
bouncer's decision log (`.bouncer/log.jsonl`), and the filesystem. A control
session with bouncer inactive isolated auto-mode's behavior. The trigger must be
an action the harness actually escalates (an out-of-tree write, not an
in-project edit), or it never reaches the hook at all.
