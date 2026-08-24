"""Session profiles — what bouncer does when it cannot decide, or when the
agent asks for a human.

A profile changes the **plumbing, not the judgment**. `policy.md` remains the
only thing that decides ALLOW/DENY. A profile changes what happens when
bouncer *cannot* decide (`on_unsure` / `on_unavailable`) and whether an agent
may appeal a denial to a human at all (`escalation`).

Two profiles ship:

    live   a human is on the line and can be asked
    solo   the agent runs alone — **no ASK is ever produced**

State is a plain JSON file keyed on the **project** (the resolved `.bouncer/`
dir) — a sibling of the escalation grant state, keyed by the same function,
for the same reason: the project is universal, unlike `session_id`, which
several harnesses omit. It is a file rather than an environment variable
because the profile must be changeable mid-session, and because a second
process (the tmux indicator) has to be able to read it.

Effective capability is **profile ∧ harness**. `solo`'s invariant is
honorable on every harness; `live` is not — a harness with no ASK channel
cannot ask however the profile is set, so it degrades to `solo`'s behaviour.
That is the fail-safe direction, but the indicator must not lie about it, so
`effective_profile()` reports the degradation separately from the choice.
"""

import json
import time
from pathlib import Path

from . import config as _config

PROFILE_DIR = _config.HOME / ".local" / "share" / "bouncer" / "profile"

# The two profiles that ship. Their behaviour lives in CONFIG_DEFAULTS
# ("profiles"); this is the name set the CLI and lint validate against,
# widened by any `profiles:` block in a config layer.
BUILTIN_PROFILE_NAMES = ("live", "solo")


# --- harness capability -------------------------------------------------------
#
# What an ABSTAIN (no decision emitted) actually reaches on each harness. This
# is the per-harness truth item 9 of the design turns on, and the reason
# `abstain` is not a universally safe unattended fallback:
#
#   "classifier"  a non-interactive floor that will decide the call itself.
#                 Only Claude Code has one (auto-mode's safety classifier,
#                 observed firing). Safe to defer to with nobody watching.
#   "prompt"      the harness asks a human instead. Fine when someone is
#                 there; under `solo` it is just an ASK by another name.
#   "passthrough" nothing to defer to — the call simply runs. Under `solo`
#                 that would silently turn "classifier unreachable" into
#                 "everything allowed", which is the one outcome to avoid.
#   "unknown"     we do not know, so we do not defer to it.
#
# Keyed on the harness, not the hook format, because `json` covers both Claude
# Code and opencode and those two abstain into different places.
HARNESS_ABSTAIN_FLOOR = {
    "claude_code":   "classifier",   # in `auto` only — see abstain_floor()
    "opencode":      "prompt",
    "codex":         "prompt",
    "codex_pretool": "passthrough",
    "shim":          "passthrough",
}

# Claude Code's abstain does not always land in the same place: only its
# `auto` permission mode runs the auto-mode safety classifier. Every other
# mode either asks a human or does something we have not established.
#
# This is an ALLOWLIST on purpose. `auto` is the one value that earns an
# abstain — the one mode whose floor was actually observed firing. Anything
# else, including a mode a future Claude Code adds and a payload with no
# `permission_mode` at all, falls to "no floor" and therefore to deny. A
# denylist would silently re-open the stall the first time the value changed.
CLASSIFIER_PERMISSION_MODES = frozenset({"auto"})

# Integrations with no channel to a human at all, whatever the profile says.
# These are the ones where `live` cannot be honoured and degrades to `solo`.
NO_ASK_HARNESSES = frozenset({"shim", "codex_pretool"})


def abstain_floor(harness: str, permission_mode: str | None = None) -> str:
    """Where an abstain lands for this harness in this permission mode.

    Returns one of the four values above. The only way to get "classifier"
    is Claude Code in `auto`; a Claude Code session in any other mode
    reports "unknown", because we deliberately do not claim to know where
    its abstain lands and do not need to.
    """
    floor = HARNESS_ABSTAIN_FLOOR.get(harness, "unknown")
    if floor == "classifier":
        if permission_mode not in CLASSIFIER_PERMISSION_MODES:
            return "unknown"
    return floor


def harness_has_unattended_floor(harness: str,
                                 permission_mode: str | None = None) -> bool:
    """True if abstaining reaches something that decides the call without a
    human. Everything we have not established counts as floorless: `solo`
    then denies rather than guessing."""
    return abstain_floor(harness, permission_mode) == "classifier"


# --- profile semantics --------------------------------------------------------

def _truthy(value, default: bool = True) -> bool:
    """Config booleans, tolerantly. YAML already turns `on`/`off` into
    True/False; the string forms are accepted for hand-edited configs."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("on", "true", "yes", "1")
    return bool(value)


def profile_allows_ask(config: dict) -> bool:
    """Whether this profile may produce an ASK at all — the profile half of
    the `harness_can_ask(fmt) and profile_allows_ask(config)` conjunction.

    Kept as its own config key (`escalation: on|off`) rather than derived from
    the profile name, so a future `solo` variant can change one without the
    other."""
    return _truthy(config.get("escalation"), True)


def resolve_unattended_action(action: str, harness: str,
                              permission_mode: str | None = None) -> str:
    """Map a fallback action to one that cannot produce an ASK.

    Used for `on_unsure` / `on_unavailable` under a profile with no ASK
    channel. `allow` and `deny` already produce no ASK and pass through
    untouched. Everything else resolves to the harness's own floor if that
    floor decides without a human, and to `deny` if it does not — a `prompt`
    floor is an ASK by another name, and a `passthrough` floor would turn an
    unreachable classifier into a blanket allow.

    Under `solo`, bouncer must never produce an ASK — not itself, and not by
    abstaining into something that will produce one on its behalf. That is
    why the permission mode is part of this decision and not just the
    harness."""
    if action in ("allow", "deny"):
        return action
    if harness_has_unattended_floor(harness, permission_mode):
        return "abstain"
    return "deny"


def effective_profile(nominal: str,
                      harness_can_ask: bool | None) -> tuple[str, bool]:
    """(effective_name, degraded) for a nominal profile on a given harness.

    `harness_can_ask=None` means the harness is not known yet (no call has
    been classified in this project), in which case the nominal profile is
    reported as-is: showing a degradation that may not apply would be its own
    kind of lie."""
    if nominal == "solo" or harness_can_ask is None or harness_can_ask:
        return nominal, False
    # A profile that wants a human, on a harness with no way to reach one.
    return "solo", True


# --- state --------------------------------------------------------------------

def _state_file(project_dir: Path) -> Path:
    return PROFILE_DIR / f"profile-{_config.project_key(project_dir)}.json"


def _load(project_dir: Path) -> dict:
    try:
        data = json.loads(_state_file(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _store(project_dir: Path, data: dict) -> None:
    try:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _state_file(project_dir).write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def get_profile(project_dir: Path | None) -> str | None:
    """The profile explicitly set for this project, or None to fall back to
    `default_profile`. Absence of state never means "hardcoded behaviour"."""
    if project_dir is None:
        return None
    name = _load(project_dir).get("profile")
    return name if isinstance(name, str) and name else None


def set_profile(project_dir: Path, name: str) -> None:
    data = _load(project_dir)
    data["profile"] = name
    data["set_at"] = time.time()
    _store(project_dir, data)


def note_harness(project_dir: Path | None, harness: str, can_ask: bool,
                 permission_mode: str | None = None) -> None:
    """Record which harness last classified a call in this project, and in
    which permission mode, so the indicator can show *effective* capability
    rather than the nominal profile.

    Writes only when the answer changes, so the steady state is a read."""
    if project_dir is None:
        return
    data = _load(project_dir)
    prev = data.get("harness")
    if (isinstance(prev, dict) and prev.get("name") == harness
            and prev.get("can_ask") is can_ask
            and prev.get("permission_mode") == permission_mode):
        return
    data["harness"] = {
        "name": harness,
        "can_ask": bool(can_ask),
        "permission_mode": permission_mode,
        "floor": abstain_floor(harness, permission_mode),
        "seen_at": time.time(),
    }
    _store(project_dir, data)


def last_harness(project_dir: Path | None) -> dict | None:
    """The harness record written by `note_harness`, or None if this project
    has not been classified yet."""
    if project_dir is None:
        return None
    rec = _load(project_dir).get("harness")
    return rec if isinstance(rec, dict) else None
