"""`bouncer profile` — show or set the session profile for this project.

One verb, values as arguments. `bouncer profile` shows, `bouncer profile
<name>` sets. There is deliberately no `bouncer solo` / `bouncer live`: the
accepted-word set stays closed, so an unrecognised token is never an action.
"""

import sys
from pathlib import Path

from ..colors import RESET, BOLD, GREEN, RED, YELLOW, DIM
from ..config import (_config_layers, _find_bouncer_dir, known_profile_names,
                      project_has_bouncer, resolve_profile, _merged_config)
from .. import profile as _profile

# One style per effective state, in each output vocabulary. Green `live`,
# amber `solo` — amber, not red, because `solo` is the normal state for most
# of the fleet most of the time, not an error. Degraded `solo` (the profile
# asked for a human the harness cannot reach) is inverted rather than
# recoloured, so it reads as "your request is not being honoured" while
# staying four characters wide like the others.
_STYLES = {
    "ansi": {
        "live":     ("\033[32m", "\033[0m"),
        "solo":     ("\033[33m", "\033[0m"),
        "degraded": ("\033[30;43;1m", "\033[0m"),
    },
    "tmux": {
        "live":     ("#[fg=green]", "#[default]"),
        "solo":     ("#[fg=yellow]", "#[default]"),
        "degraded": ("#[bg=yellow,fg=black,bold]", "#[default]"),
    },
}


def effective_state(cwd: Path) -> dict:
    """What the indicator should show: the profile actually in force, and
    whether that differs from the one asked for.

    `harness_can_ask` is None until some call has been classified in this
    project — the profile is then reported as chosen, because claiming a
    degradation that may not apply would be its own kind of lie."""
    nominal = resolve_profile(cwd)
    project_dir = _find_bouncer_dir(cwd)
    seen = _profile.last_harness(project_dir)
    can_ask = seen.get("can_ask") if isinstance(seen, dict) else None
    effective, degraded = _profile.effective_profile(nominal, can_ask)
    return {
        "profile":   effective,
        "nominal":   nominal,
        "degraded":  degraded,
        "harness":   (seen or {}).get("name") if seen else None,
        "chosen":    _profile.get_profile(project_dir) is not None,
    }


def _render(state: dict, as_format: str) -> str:
    name = state["profile"]
    if as_format == "json":
        import json as _json
        return _json.dumps(state)
    if as_format not in _STYLES:
        return name
    key = "degraded" if state["degraded"] else name
    open_, close = _STYLES[as_format].get(key, ("", ""))
    return f"{open_}{name}{close}"


def cmd_profile(args):
    cwd_arg = getattr(args, "cwd", None)
    cwd = Path(cwd_arg) if cwd_arg else Path.cwd()
    name = getattr(args, "name", None)
    as_format = getattr(args, "as_format", "plain")

    project_dir = _find_bouncer_dir(cwd)
    if project_dir is None or not project_has_bouncer(cwd):
        if name is None and as_format in ("plain", "ansi", "tmux"):
            # Nothing to say, and the indicator must not invent a profile for
            # a directory bouncer does not gate.
            return
        if name is None:
            print('{"profile": null}')
            return
        print(f"{YELLOW}bouncer profile:{RESET} no .bouncer project here.",
              file=sys.stderr)
        sys.exit(1)

    if name is None:
        state = effective_state(cwd)
        if as_format != "plain":
            print(_render(state, as_format), end="")
            return
        _print_show(cwd, state)
        return

    known = known_profile_names(_config_layers(cwd))
    if name not in known:
        print(f"{RED}bouncer profile:{RESET} unknown profile {name!r}. "
              f"Known: {', '.join(known)}", file=sys.stderr)
        sys.exit(2)

    _profile.set_profile(project_dir, name)
    state = effective_state(cwd)
    _print_show(cwd, state)


def _print_show(cwd: Path, state: dict) -> None:
    name = state["profile"]
    color = GREEN if name == "live" else YELLOW
    line = f"{color}{BOLD}{name}{RESET}"
    if state["degraded"]:
        line += (f"  {YELLOW}⚠{RESET} {state['nominal']} was requested, but "
                 f"{state['harness'] or 'this harness'} has no way to ask "
                 f"a human")
    elif not state["chosen"]:
        line += f"  {DIM}(default_profile){RESET}"
    print(f"{BOLD}profile:{RESET} {line}")

    config = _merged_config(cwd, state["profile"])
    allows_ask = _profile.profile_allows_ask(config)
    print(f"  escalation:     {'on' if allows_ask else 'off'}")
    for key in ("on_unsure", "on_unavailable"):
        configured = config.get(key, "ask")
        # Under a no-ASK profile the configured action is resolved through the
        # harness, so show where it actually lands — but only once the harness
        # is known, since "unknown" resolves to deny and saying so before any
        # call has been seen would overstate it.
        if allows_ask or not state["harness"]:
            print(f"  {key + ':':<15} {configured}")
            continue
        resolved = _profile.resolve_unattended_action(configured,
                                                      state["harness"])
        arrow = "" if resolved == configured else f" {DIM}→{RESET} {resolved}"
        print(f"  {key + ':':<15} {configured}{arrow}")
    if state["harness"]:
        floor = _profile.HARNESS_ABSTAIN_FLOOR.get(state["harness"], "unknown")
        print(f"  harness:        {state['harness']} "
              f"{DIM}(abstain → {floor}){RESET}")
    else:
        print(f"  harness:        {DIM}not seen yet in this project{RESET}")
