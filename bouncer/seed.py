"""`bouncer seed <dir>` — arm a new, unarmed project with the stock templates.

`bouncer init` is refused to every agent, so standing up a new project
stopped at a human. Seeding is the one narrow exception: an agent (in
practice the orchestrator) may give a tree that has no bouncer at all the
same starter files `init` writes. That makes the tree stricter, never
looser: an unarmed tree is ungated today.

It is allowed only when all of these hold (see `refusal`):

- the target is an existing directory, not the home directory or above it;
- neither the target nor any directory above it has a `.bouncer/`. That is
  the walk bouncer itself makes to find a project, so a seeded child can
  never hide an armed parent's policy;
- the target is not the caller's own project, in either direction;
- only the stock templates are written. Seeding takes no policy text, no
  options, nothing that changes what is written.

Every check is made here, in the code that writes, and again in the hook
before the command runs. Policy text still goes through propose/apply.
"""
from pathlib import Path

from .config import HOME, PROJECT_DIR_NAME, _find_bouncer_dir


class Refused(Exception):
    pass


def _resolve(target: Path, cwd: Path) -> Path:
    target = target.expanduser()
    if not target.is_absolute():
        target = cwd / target
    return target.resolve()


def caller_project(cwd: Path) -> Path:
    """The caller's own project: the nearest directory at or above `cwd`
    that holds a `.git`, or `cwd` itself when there is none."""
    cwd = cwd.resolve()
    for current in (cwd, *cwd.parents):
        if (current / ".git").exists():
            return current
        if current == HOME.resolve():
            break
    return cwd


def refusal(target: Path, cwd: Path,
            caller_cwd: Path | None = None) -> str | None:
    """Why seeding `target` (read from `cwd`) is refused, or None.
    `caller_cwd` is where the caller's own project is judged from, when
    that is not `cwd`: a `cd` in the command moves `cwd`, not the caller."""
    try:
        resolved = _resolve(target, cwd)
    except (OSError, RuntimeError):
        return f"Refused: cannot resolve {target}."
    if not resolved.is_dir():
        return f"Refused: {resolved} is not an existing directory."
    home = HOME.resolve()
    if resolved == home or resolved in home.parents:
        return (f"Refused: {resolved} is the home directory or above it. "
                "Seeding arms one project, not every tree under it.")
    armed = _find_bouncer_dir(resolved)
    if armed is not None:
        if armed.parent == resolved:
            return (f"Refused: {resolved} already has {PROJECT_DIR_NAME}/. "
                    "Seeding is only for a tree with no bouncer at all; "
                    "policy changes go through bouncer propose.")
        return (f"Refused: {armed.parent} above it is already a bouncer "
                f"project. A {PROJECT_DIR_NAME}/ in {resolved} would hide "
                "that project's policy from everything run inside it.")
    own = caller_project(caller_cwd or cwd)
    if resolved == own or own in resolved.parents \
            or resolved in own.parents:
        return (f"Refused: {resolved} is your own project ({own}). A "
                "session may not arm its own tree; seed another project, "
                "or ask the user to run bouncer init.")
    return None


def _launch_dir() -> Path | None:
    """The launch directory of the agent session this process runs in, or
    None for a person at a terminal. An agent's `cd` cannot move it."""
    from .proposals import calling_session
    launch, _nested = calling_session()
    return launch


def seed(target: Path, cwd: Path | None = None) -> Path:
    """Write the stock templates into `target`/.bouncer/. Returns that
    directory, or raises Refused."""
    cwd = cwd or Path.cwd()
    # The caller's project is judged from both places it could be: where
    # the session was launched, and where this process runs.
    for caller in filter(None, (_launch_dir(), cwd)):
        reason = refusal(target, cwd, caller)
        if reason:
            raise Refused(reason)
    from .commands.init import write_project_templates
    bouncer_dir = _resolve(target, cwd) / PROJECT_DIR_NAME
    write_project_templates(bouncer_dir, fresh=True)
    return bouncer_dir
