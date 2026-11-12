# bouncer

## Never edit in the main checkout

The live PreToolUse hook runs bouncer straight from this repo's
working tree (~/.claude/hooks/bouncer_hook.py -> `bouncer` on PATH ->
bin/bouncer, which puts this checkout on sys.path). An uncommitted
edit here is live for every agent lane at once.

Work in a git worktree outside this checkout (`git worktree add
<scratchpad>/wt -b <branch>`), test there with PYTHONPATH=<worktree>,
and leave this tree on main. Merging to main is deploying: it needs
Michael's approval for any classifier or policy-behaviour change.
