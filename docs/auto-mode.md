# bouncer and Claude Code auto-mode

Claude Code's **auto-mode** (a research-preview permission mode) runs tools
without prompting, vetting each call with an internal safety classifier that
blocks catastrophic actions (mass deletion, data exfiltration, malicious code)
and otherwise lets work proceed. bouncer is a `PreToolUse` hook. This document
records how the two interact — established empirically against Claude Code
2.1.167, not from documentation.

## The permission pipeline

A tool call passes through these gates:

```
model's own judgment  →  PreToolUse hook (bouncer)
                             ├─ ALLOW   → runs
                             ├─ DENY    → blocked
                             ├─ ASK     → prompts you (auto-mode NOT consulted)
                             └─ ABSTAIN → emits nothing → falls through ↓

auto-mode classifier  →  handles abstains + tools bouncer doesn't intercept
```

1. **Model judgment.** Claude may decline to emit a call at all (e.g. it
   refused a bare `rm -rf` of a home directory until given explicit context).
   This sits above everything and is **nondeterministic** — the same prompt
   ran in one session and was refused in another.
2. **bouncer (the hook).** Every bouncer verdict — `ALLOW`, `DENY`, *and*
   `ASK` — is authoritative for the call it decides. An `ALLOW` or `DENY` is
   the permission decision; an `ASK` (`on_unsure: ask`) emits
   `permissionDecision: "ask"`, which **interrupts auto-mode and prompts you**,
   surfacing bouncer's reason. bouncer does *not* hand ambiguous calls to
   auto-mode — `ask` goes to the human, not to auto-mode's classifier.
3. **auto-mode classifier.** Governs only calls bouncer does not decide — a tool
   not in bouncer's intercept list, or one bouncer **abstains** on
   (`on_unsure: abstain` / `on_unavailable: abstain`, which emit *no*
   `permissionDecision`). It does **not** backstop a bouncer `ASK`, and note
   `on_unsure: allow` does *not* defer here — it emits a real allow that
   overrides auto-mode.

## Precedence: bouncer is authoritative

When bouncer returns a decision, **auto-mode is never consulted.** Verified from
session transcripts: no auto-mode decision event appears for any call bouncer
decided.

- **bouncer DENY** → the call is blocked at the hook; auto-mode never runs.
- **bouncer ALLOW** → the call executes; auto-mode gets **no veto**, even for a
  command whose shape matches auto-mode's own block rules (tested with an
  explicitly-allowed `rm -rf` of a non-existent path — bouncer logged ALLOW and
  the command ran, with no auto-mode involvement).
- **bouncer ASK** → auto-mode's hands-off flow **stops** and you get a normal
  permission prompt carrying bouncer's reason; auto-mode does not auto-resolve
  it (tested with a policy that returns `UNSURE` on an out-of-tree
  `touch ~/NOTE` — Claude Code halted with `Hook PreToolUse:Bash requires
  confirmation … Do you want to proceed? 1. Yes / 2. No`, and the file was not
  created until a human chose).

## The interaction matrix

| bouncer says | auto-mode | Result |
|---|---|---|
| **DENY** | *not consulted* | Blocked by bouncer |
| **ALLOW** | *not consulted* | Runs — even if auto-mode would have blocked it |
| **UNSURE → ask** | *not consulted* | **Prompts you** — auto-mode autonomy pauses |
| **UNSURE → abstain** | **decides** | Auto-mode classifies it (`Allowed by auto mode classifier`) |

All four rows are confirmed empirically (Claude Code 2.1.167–2.1.168). The
earlier expectation that a `UNSURE → ask` would fall through to auto-mode and
"mostly auto-approve" was **wrong**: a hook `ask` is a real user prompt that
breaks auto-mode's no-interruption flow. To keep auto-mode running without
prompts, either bouncer reaches a confident `ALLOW`/`DENY`, or it **abstains**
(`on_unsure: abstain`) — only then does the call fall through to auto-mode.
`UNSURE → ask` stops the run and asks you.

(The `abstain` row was confirmed by re-running the same `touch ~/NOTE` trigger
with `on_unsure: abstain`: bouncer logged `UNSURE`, emitted no decision, and the
command ran with no prompt — auto-mode auto-approved it.)

## What this means in practice

Because a bouncer **ALLOW is authoritative**, bouncer can both *tighten* and
*loosen* relative to auto-mode. A permissive bouncer policy will pass actions
auto-mode would otherwise block, since auto-mode is bypassed whenever bouncer
has an opinion. **They are not additive defense-in-depth on the allow side** —
where bouncer decides, bouncer's judgment replaces auto-mode's.

Pick the composition you want:

- **Want auto-mode's catastrophic-action floor to stay in force?** Leave those
  tools *out* of bouncer's intercept list (or use `on_unsure: allow`), so the
  calls reach auto-mode instead of bouncer. A bouncer `UNSURE → ask` will **not**
  defer to auto-mode — it stops and asks you.
- **Want bouncer fully in charge?** A confident, specific policy means bouncer
  decides most calls and auto-mode rarely matters.
- **Want unattended auto-mode runs?** Set the session profile to `solo`
  (`bouncer profile solo`), which does exactly this and also refuses
  escalation, so an agent cannot open a prompt nobody will answer. `solo`
  reads the payload's `permission_mode` and only defers to the floor
  described here when the session is actually in `auto`; in any other mode
  it denies instead of abstaining into a prompt. See
  [`configuration.md`](configuration.md#session-profiles-live--solo). The
  underlying keys, if you want them without the profile: `on_unsure: ask`
  (the default) will pause
  the run for a human on any `UNSURE`. Set **`on_unsure: abstain`** (and
  **`on_unavailable: abstain`** so a flaky LLM backend doesn't stall the run):
  bouncer then emits no decision on those calls and hands them straight to
  auto-mode's classifier, instead of nagging you or guessing `allow`/`deny`.
  bouncer still enforces its confident `ALLOW`/`DENY` verdicts; only the
  ambiguous and unavailable cases defer.

The roles are complementary: bouncer provides **fine-grained, natural-language,
per-project boundaries** (with a local/private model, cross-harness, fully
logged); auto-mode provides a generic, zero-config floor plus a keep-going
nudge. Running them together works well — bouncer encodes your project's
specific rules; what it confidently decides it owns, what it leaves `UNSURE`
comes back to you, and only tools it never intercepts fall through to auto-mode.

## How this was determined

The interaction was tested by driving a fresh Claude Code session in a detached
`tmux` window (auto-mode on) inside a disposable project with a deterministic
bouncer policy, then observing three signals: the TUI (`tmux capture-pane`),
bouncer's decision log (`.bouncer/log.jsonl`), and the filesystem. A control
session with bouncer inactive isolated auto-mode's behavior. The trigger must be
an action the harness actually escalates (an out-of-tree write, not an
in-project edit), or it never reaches the hook at all.
