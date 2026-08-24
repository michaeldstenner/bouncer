# Design Philosophy

Bouncer is a reviewer for existing approval prompts, not an extra lockdown
layer.

Most coding harnesses already stop before commands they cannot classify with
simple rules. That behavior is appropriately conservative, but it can also
pause useful work for commands that are safe in context and obvious to a
competent reviewer: shell pipelines, here-docs, inline scripts, generated
commands with quoting, or project-specific operations that do not fit a small
glob or regex allowlist.

Bouncer's job is to make that approval loop more permissive when policy and
context justify it:

- Do not ask an LLM about commands the harness would already run without
  interruption.
- Do ask an LLM about commands that would otherwise stop for human review.
- Auto-approve commands that are policy-compliant and clear in context.
- Deny commands that are clearly outside policy or too risky to run.
- Abstain when the LLM is unsure or unavailable, so the harness can ask the
  human through its normal approval path.

The goal is not to be more restrictive than the harness. The goal is to spend
human attention where it is actually useful. A human should not have to stop,
read, and approve every nontrivial but safe shell command just because a
regex-based safety check cannot understand it.

## Hook Selection

For harnesses that expose an approval-request hook, bouncer should prefer that
hook over a pre-tool hook.

An approval-request hook fires only after the harness has decided it would ask
the user. That is the cleanest point for bouncer to help: ALLOW can save a
needless interruption, DENY can block a request with a clear reason, and UNSURE
can defer to the harness's normal human review.

A pre-tool hook runs earlier, before the harness has decided whether approval
is needed. That is a poor fit for bouncer's main purpose: it risks reviewing
commands that would never have bothered the user, and some harnesses cannot ask
from that hook, forcing UNSURE to become either pass-through or denial.

For Codex, this means `PermissionRequest` is the primary integration point.
`PreToolUse` is not recommended for bouncer. It cannot route `UNSURE` or
`ESCALATE` through Codex's normal approval prompt, and Codex GUI/TUI behavior
may differ in whether that hook is actually run.

## Policy Evolution

Historical decisions can reveal repeated friction and unclear boundaries, but
they do not reveal what the user ultimately chose in every harness. Policy
review therefore treats prior decisions as classifier behavior, not ground
truth. An independent model clusters requests and recommends a disposition; the
user supplies the authoritative allow, deny, context-dependent, or one-off
judgment during review.

Analysis and authority remain separate. The review model has no tools, project
configuration cannot redirect it, log text is treated as untrusted data, and a
model response cannot directly update active policy. A candidate must pass
counterfactual replay, be saved through a temporary editor buffer, survive an
exact-byte drift check, and receive final confirmation on its visible diff.

These controls prevent policy refinement from being an intentional
self-authorization interface for agents. They do not turn Bouncer into a
same-user containment boundary; harness coverage and operating-system controls
still determine whether an agent can bypass the hook entirely.
