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
is needed. That can be useful as an optional hard guard in highly trusted or
YOLO-like modes, but it is not bouncer's default posture. It risks reviewing
commands that would never have bothered the user, and some harnesses cannot ask
from that hook, forcing UNSURE to become either pass-through or denial.

For Codex, this means `PermissionRequest` is the primary integration point.
`PreToolUse` remains optional for cases where the user deliberately wants a
hard guard in sessions that may otherwise run commands without approval.
