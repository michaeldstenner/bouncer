import fnmatch
import hashlib
import sys
from pathlib import Path
from .yaml import MicroYAML, MicroYAMLError

HOME               = Path.home()
USER_CONFIG_DIR    = HOME / ".config" / "bouncer"
USER_CONFIG_FILE   = USER_CONFIG_DIR / "config.yaml"
USER_POLICY_FILE   = USER_CONFIG_DIR / "policy.md"
USER_SYSTEM_PROMPT = USER_CONFIG_DIR / "system_prompt.txt"
USER_LOG_FILE      = HOME / ".local" / "share" / "bouncer" / "log.jsonl"
USER_REVIEW_DIR    = HOME / ".local" / "share" / "bouncer" / "reviews"
PROJECT_DIR_NAME   = ".bouncer"

CONFIG_DEFAULTS: dict = {
    "enabled": True,
    "tools": "all",
    "policy_mode": "append",
    "activity_width": 10,
    "activity": {
        "colors": {
            "ALLOW": "green",
            "DENY": "red",
            "BLOCK": "red",
            "UNSURE": "magenta",
            "TIMEOUT": "black_on_magenta_bold",
            "LLM_ERROR": "black_on_red_bold",
            "ESCALATE": "cyan",
        },
    },
    "notify": {},
    "llm": {
        "provider":           "ollama",
        "url":                "http://localhost:11434",
        "timeout":            30,
        "caller_max":         4,
        "circuit_cooldown_s": 120,
        "circuit_mode":       "futility",
        "grace_s":            8,
        "deadline_s":         180,
        "ps_probe":           True,
    },
    "on_unsure":      "ask",
    "on_unavailable": "ask",
    "escalation": True,
    "escalation_requires_attempt": True,
    "escalation_attempt_ttl":      300,
    # Session profiles. A profile changes the plumbing (what happens when
    # bouncer cannot decide, and whether an agent may appeal to a human), never
    # the judgment — policy.md alone decides ALLOW/DENY. All profiles are
    # explicit: with no profile state for a project, `default_profile` names
    # the one in force, so the indicator can always name a profile.
    "default_profile": "live",
    "profiles": {
        "live": {
            "escalation":     True,
            "on_unsure":      "ask",
            "on_unavailable": "ask",
        },
        "solo": {
            "escalation":     False,
            "on_unsure":      "abstain",
            "on_unavailable": "abstain",
        },
    },
    "log": {
        "verbosity":   "all",
        "max_entries": 10000,
        "llm_debug":   False,
    },
}

# Built-in group memberships, the seed for the per-layer group fold. `internal`
# is harness "plumbing" — discovery/meta tools the harness natively auto-allows,
# so skipping them is free. Claude's `ToolSearch` is the canonical member (it
# lives in Claude Code's read-only tool set); the entry is inert under harnesses
# that expose no such tool. Editable via a `groups:` block in any config layer.
DEFAULT_GROUPS: dict[str, frozenset] = {
    "internal": frozenset({"ToolSearch"}),
}

CONFIG_YAML_TEMPLATE = """\
# bouncer project config
# All settings are optional — uncomment to override user-level defaults.

# Enable or disable bouncer for this project.
#enabled: true

# Tools to intercept. An ordered list of ±ops folded onto the inherited set
# (user config -> this file -> config.local.yaml). `+X` intercepts, `-X` skips;
# X may be a tool name, a glob (mcp__google_workspace__*), `@all`, or a group
# such as `@internal` (harness plumbing — e.g. Claude's ToolSearch — skipped by
# default). The last matching op wins.
#   tools: [-Read]              # stop gating Read in this project
#   tools: [+ToolSearch]        # do gate plumbing here
#   tools: [-@all, +Bash]       # absolute: only Bash
# A bare list like [Bash] is legacy shorthand for [-@all, +Bash] ("only Bash").
#tools:
#  - -Read

# Define or edit groups (same ±fold over the membership set). e.g. drop a tool
# from @internal to re-gate it everywhere:
#groups:
#  internal: -ToolSearch

# How this project's policy.md combines with user-level policy.md:
#   append  (default): project policy appended after user policy
#   replace          : project policy fully replaces user policy
#policy_mode: append

# LLM backend — uncomment to override user defaults
# provider: ollama | openai | openai_compatible | anthropic
#llm:
#  provider: ollama
#  model: qwen3:32b              # required — no default
#  url: http://localhost:11434   # ollama / openai_compatible base URL
#  timeout: 30                   # fallback / non-streaming timeout (s)
#  caller_max: 4                 # max concurrent bouncer calls in the queue
#  circuit_cooldown_s: 120       # seconds open before probe attempt
#  circuit_mode: futility        # futility | count (futility = leaky-LLR breaker)
#  grace_s: 8                    # min wait before stall bail may fire
#  deadline_s: 180               # pencils-down ceiling — owns the call end to
#                                #   end under futility: bounds the queue wait
#                                #   AND the active call (null = no ceiling).
#                                #   queue_timeout / first_token_timeout /
#                                #   generation_timeout are ignored under
#                                #   futility; circuit_n is count-mode only.
#  ps_probe: true                # cheap /api/ps liveness probe (ollama only)
#  api_key: ...                  # openai / anthropic (or use env var)
#  fallback_on: [timeout*, error:unreachable, http_5*, circuit_open, circuit_futile]
#  fallbacks:                    # tried in order after llm.model
#    - model: gpt-oss-120b       # inherits provider/url/api_key/timeouts
#    - model: nemotron-3-ultra
#  extra_params:                 # optional provider-specific request params
#    max_tokens: 1000

# Independent policy-review model. This section is user-config only: project
# config cannot redirect aggregated policy/log data to another provider.
#review:
#  llm:
#    provider: anthropic
#    model: claude-sonnet-4-20250514
#    timeout: 120
#    extra_params:
#      max_tokens: 8192

# Fallback when the LLM is uncertain (on_unsure) or unreachable (on_unavailable):
#   ask      prompt the user (default)
#   allow    let the call through
#   deny     block the call
#   abstain  no opinion — defer to the harness's own permission flow (e.g.
#            Claude Code auto-mode, or its normal prompt). NOT the same as
#            allow: risky calls still hit the harness's own gate.
#on_unsure: ask
#on_unavailable: ask

# Session profiles. A profile changes the PLUMBING (what happens when bouncer
# cannot decide, and whether an agent may appeal a denial to a human) — never
# the judgment: policy.md alone decides ALLOW/DENY. Two ship:
#   live   a human is on the line and can be asked
#   solo   the agent runs alone — no ASK is ever produced
# Switch with `bouncer profile live` / `bouncer profile solo`; `bouncer
# profile` shows the effective state. With no profile set for a project,
# default_profile names the one in force.
#default_profile: live
#profiles:
#  solo:
#    escalation: off        # no ASK may be produced in this profile
#    on_unsure: abstain     # resolved per harness — see docs/integrations.md
#    on_unavailable: abstain
# A profile fragment beats base keys within its own layer, and later layers
# still win: user.base -> user.profiles[P] -> project.base ->
# project.profiles[P] -> local.base -> local.profiles[P].

# Escalation gating. An agent escalates a denied command by re-running it with
# a `# ESCALATE:` prefix. To curb agents that pre-emptively escalate commands
# they never tried, only honor an escalation if the same command was actually
# attempted within the last escalation_attempt_ttl seconds.
#escalation_requires_attempt: true
#escalation_attempt_ttl: 300

# Activity/statusline indicator
#activity_width: 10
#activity:
#  colors:
#    ALLOW: green
#    DENY: red
#    UNSURE: magenta
#    ESCALATE: cyan

# Optional post-decision notifier. Bouncer starts the command, writes one JSON
# object to stdin, closes stdin, and does not wait for it to finish.
#notify:
#  command: ~/bin/bouncer-notify
#  decisions: all       # all, or a list such as [DENY, ESCALATE, TIMEOUT, LLM_ERROR]

# Logging
#log:
#  verbosity: all         # all | deny_only | off  (deny_only still records a
#                         #   compact marker for allows so the activity strip
#                         #   stays complete; off empties the strip too)
#  max_entries: 10000     # prune log when it exceeds this many entries
#  llm_debug: false       # write redacted LLM request/response JSONL for debugging
"""

POLICY_MD_TEMPLATE = """\
# Project Policy

Describe this project for the LLM classifier:
- What paths are safe to touch?
- What external services are involved?
- What operations are routine vs. risky?
"""

# Sentinel that splits the unified policy editor tempfile into committed
# (policy.md) and local (policy.local.md) sections.
POLICY_LOCAL_SENTINEL = "<!-- bouncer:local -->"

POLICY_LOCAL_MD_TEMPLATE = """\
# Local Policy Additions (gitignored)

Add any local-only context here — paths, credentials, personal workflow
notes, or anything you don't want committed.
"""

USER_CONFIG_YAML_TEMPLATE = """\
# bouncer user config — applies to all projects

enabled: true

# Tools to intercept — an ordered list of ±ops. `+X` intercepts, `-X` skips;
# X may be a tool name, a glob, `@all`, or a group like `@internal` (harness
# plumbing such as Claude's ToolSearch, which is a no-op to skip). The default
# below means "every tool except plumbing". Project configs fold onto this set.
#   tools: [+@all, -@internal, -Read]   # also stop gating Read
#   tools: [-@all, +Bash]               # only Bash
# (The bare string `all` still works but is deprecated; it now resolves to
#  exactly [+@all, -@internal].)
tools: [+@all, -@internal]

# Define or edit tool groups, using the same ±fold over the membership set.
# @internal seeds with the harness plumbing set; add/remove members to change
# what `-@internal` skips:
#groups:
#  internal: [+TaskGet, +TaskList]   # also treat these as skippable plumbing

# LLM backend
# provider: ollama | openai | openai_compatible | anthropic
llm:
  provider: ollama
  model: qwen3:32b              # required — no default
  url: http://localhost:11434   # ollama / openai_compatible base URL
  timeout: 30                   # fallback / non-streaming timeout (s)
  caller_max: 4                 # max concurrent bouncer calls in the queue
  circuit_cooldown_s: 120       # seconds open before probe attempt
  circuit_mode: futility        # futility | count (futility = leaky-LLR breaker)
  grace_s: 8                    # min wait before stall bail may fire
  deadline_s: 180               # pencils-down ceiling — owns the call end to end
                                #   under futility: bounds the queue wait AND
                                #   the active call (null = no ceiling).
                                #   queue_timeout / first_token_timeout /
                                #   generation_timeout are ignored under
                                #   futility; circuit_n is count-mode only.
  ps_probe: true                # cheap /api/ps liveness probe (ollama only)
  # api_key: ...                # openai / anthropic (or env var)
  # fallback_on: [timeout*, error:unreachable, http_5*, circuit_open, circuit_futile]
  # fallbacks:                  # tried in order after llm.model
  #   - model: gpt-oss-120b     # inherits provider/url/api_key/timeouts
  #   - provider: anthropic     # cross-provider fallback clears url/api_key
  #     model: claude-haiku-4-5-20251001
  # extra_params:               # optional provider-specific request params
  #   max_tokens: 1000

# Independent policy-review model. It is deliberately separate from `llm` and
# has no implicit fallback to the classifier model.
#review:
#  llm:
#    provider: anthropic
#    model: claude-sonnet-4-20250514
#    timeout: 120
#    extra_params:
#      max_tokens: 8192

# Fallback when the LLM is uncertain (on_unsure) or unreachable (on_unavailable):
#   ask      prompt the user (default)
#   allow    let the call through
#   deny     block the call
#   abstain  no opinion — defer to the harness's own permission flow (e.g.
#            Claude Code auto-mode, or its normal prompt). NOT the same as
#            allow: risky calls still hit the harness's own gate.
on_unsure: ask
on_unavailable: ask

# Session profiles. A profile changes the PLUMBING (what happens when bouncer
# cannot decide, and whether an agent may appeal a denial to a human) — never
# the judgment: policy.md alone decides ALLOW/DENY. Two ship:
#   live   a human is on the line and can be asked
#   solo   the agent runs alone — no ASK is ever produced
# Switch with `bouncer profile live` / `bouncer profile solo`; `bouncer
# profile` shows the effective state. With no profile set for a project,
# default_profile names the one in force.
#default_profile: live
#profiles:
#  solo:
#    escalation: off        # no ASK may be produced in this profile
#    on_unsure: abstain     # resolved per harness — see docs/integrations.md
#    on_unavailable: abstain
# A profile fragment beats base keys within its own layer, and later layers
# still win: user.base -> user.profiles[P] -> project.base ->
# project.profiles[P] -> local.base -> local.profiles[P].

# Escalation gating. An agent escalates a denied command by re-running it with
# a `# ESCALATE:` prefix. To curb agents that pre-emptively escalate commands
# they never tried, only honor an escalation if the same command was actually
# attempted within the last escalation_attempt_ttl seconds.
#escalation_requires_attempt: true
#escalation_attempt_ttl: 300

# Activity/statusline indicator
activity_width: 10
#activity:
#  colors:
#    ALLOW: green
#    DENY: red
#    UNSURE: magenta
#    ESCALATE: cyan

# Optional post-decision notifier. Bouncer starts the command, writes one JSON
# object to stdin, closes stdin, and does not wait for it to finish.
#notify:
#  command: ~/bin/bouncer-notify
#  decisions: all       # all, or a list such as [DENY, ESCALATE, TIMEOUT, LLM_ERROR]

# Logging
log:
  verbosity: all            # all | deny_only | off  (deny_only keeps the
                            #   activity strip complete via compact rows;
                            #   off empties the strip too)
  max_entries: 10000
  llm_debug: false
"""

USER_POLICY_MD_TEMPLATE = """\
# User Policy

Global context for the LLM classifier:
- Working as a developer on a personal workstation
- Standard development tools (git, npm, make, python, uv) are generally safe
- Irreversible system changes and external data exfiltration require caution
"""

_YAML = MicroYAML()


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_yaml_config(path: Path) -> dict:
    """Load a config file, falling back to defaults if it is unusable.

    MicroYAML raises on malformed input rather than returning a
    plausible-looking partial parse. Falling back to defaults is right
    for a hook that must not crash the tool call it is gating, but a
    silent fallback would hide a typo that disables policy, so say so.
    """
    try:
        return _YAML.load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    except MicroYAMLError as exc:
        print(f"bouncer: ignoring malformed {path}: {exc}",
              file=sys.stderr)
        return {}


def load_review_config() -> dict:
    """Load user-only reviewer settings with same-provider connection reuse.

    The reviewer model never inherits from the classifier. Provider transport
    settings may: using another model on the same endpoint should not require a
    second copy of its URL or credential.
    """
    user_config = load_yaml_config(USER_CONFIG_FILE)
    review = user_config.get("review", {})
    if not isinstance(review, dict):
        return {}
    raw_review_llm = review.get("llm")
    if not isinstance(raw_review_llm, dict):
        return review
    classifier_llm = user_config.get("llm", {})
    classifier_llm = classifier_llm if isinstance(classifier_llm, dict) else {}
    classifier_provider = classifier_llm.get("provider", "ollama")
    review_provider = raw_review_llm.get("provider", classifier_provider)
    inherited = {"provider": review_provider}
    if review_provider == classifier_provider:
        for key in ("url", "api_key", "key_name"):
            if classifier_llm.get(key):
                inherited[key] = classifier_llm[key]
    inherited.update(raw_review_llm)
    result = dict(review)
    result["llm"] = inherited
    return result


def load_policy(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# --- intercept-set resolution -------------------------------------------------
#
# The set of tools bouncer classifies is an ordered, left-to-right fold of `±`
# ops over a running set, evaluated per tool (no universe is materialized).
# `+X` covers tools matching selector X; `-X` uncovers them; the last matching
# op wins. `all` is sugar for `+@all -@internal`. See scratch/tools-fold-design.md.

def _is_op(token: str) -> bool:
    return token.startswith(("+", "-"))


def _split_op(token: str) -> tuple[str, str]:
    """Split a `±selector` token into (sign, selector). A bare token (no
    prefix) is treated as `+`."""
    op = token.strip()
    if _is_op(op):
        return op[0], op[1:].strip()
    return "+", op


def _norm_selector(sel: str) -> str:
    """Normalize a selector: bare `all` -> `@all`, `none` -> `@all` (handled by
    sign), case preserved for tool names (matching is case-insensitive)."""
    return "@all" if sel.strip().lower() == "all" else sel.strip()


def expand_tools(value) -> list[tuple[str, str]]:
    """Expand a `tools:` config value into an ordered list of (sign, selector)
    ops. Tolerant by design — this runs on the classify hot path.

      - "all"  -> [+@all, -@internal]      (skip harness plumbing)
      - "none" -> [-@all]
      - other scalar S -> absolute single: [-@all, +S]
      - delta list (every item ±-prefixed) -> folded onto the inherited set
      - bare/mixed list -> legacy absolute: an implicit `-@all` is prepended
    """
    if value is None:
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, str):
        s = value.strip()
        low = s.lower()
        if low in ("all", "@all"):
            return [("+", "@all"), ("-", "@internal")]
        if low in ("none", "@none"):
            return [("-", "@all")]
        sign, sel = _split_op(s)
        if _is_op(s):
            return [(sign, _norm_selector(sel))]
        return [("-", "@all"), ("+", _norm_selector(s))]
    if not isinstance(value, (list, tuple)):
        return []
    items = [str(x).strip() for x in value if str(x).strip()]
    if not items:
        return [("-", "@all")]  # `tools: []` == intercept nothing
    ops: list[tuple[str, str]] = []
    if not all(_is_op(x) for x in items):
        ops.append(("-", "@all"))  # legacy absolute set: clear, then add
    for item in items:
        sign, sel = _split_op(item)
        ops.append((sign, _norm_selector(sel)))
    return ops


def _expand_member_ops(value) -> list[tuple[str, str]]:
    """Ops for a `groups.<name>:` value — like expand_tools but with no
    implicit `-@all` (bare names just add members) and no `all` sugar."""
    items = value if isinstance(value, (list, tuple)) else [value]
    ops: list[tuple[str, str]] = []
    for item in items:
        s = str(item).strip()
        if not s:
            continue
        ops.append(_split_op(s))
    return ops


def resolve_groups(group_layers: list[dict]) -> dict[str, frozenset]:
    """Fold group definitions from each layer (in order) over DEFAULT_GROUPS.
    Members are concrete tool names; `-all` clears a group."""
    groups: dict[str, set] = {k: set(v) for k, v in DEFAULT_GROUPS.items()}
    for layer in group_layers:
        if not isinstance(layer, dict):
            continue
        for name, val in layer.items():
            key = str(name).strip().lstrip("@").lower()
            members = set(groups.get(key, set()))
            for sign, sel in _expand_member_ops(val):
                if sel.lower() in ("all", "@all"):
                    if sign == "-":
                        members.clear()
                    continue
                if sign == "+":
                    members.add(sel)
                else:
                    members = {m for m in members if m.lower() != sel.lower()}
            groups[key] = members
    return {k: frozenset(v) for k, v in groups.items()}


def _selector_matches(selector: str, tool_name: str, groups: dict) -> bool:
    sel = selector.strip()
    low = tool_name.lower()
    if sel.lower() in ("all", "@all"):
        return True
    if sel.startswith("@"):
        members = groups.get(sel[1:].lower(), frozenset())
        return any(low == m.lower() for m in members)
    if "*" in sel or "?" in sel:
        return fnmatch.fnmatch(low, sel.lower())
    return low == sel.lower()


def _intercepted(tool_name: str, ops: list[tuple[str, str]], groups: dict) -> bool:
    state = False
    for sign, sel in ops:
        if _selector_matches(sel, tool_name, groups):
            state = sign == "+"
    return state


def tool_intercepted(tool_name: str, config: dict) -> bool:
    """Whether bouncer should classify this tool (vs. skip and defer to the
    harness). Uses the cross-layer fold stashed by `_merged_config`; falls back
    to a single-layer fold for directly-built configs (tests, pure callers)."""
    ops = config.get("_tools_ops")
    groups = config.get("_groups")
    if ops is None or groups is None:
        groups = resolve_groups([config.get("groups", {})])
        ops = expand_tools(CONFIG_DEFAULTS["tools"]) + expand_tools(config.get("tools"))
    return _intercepted(tool_name, ops, groups)


def resolve_intercept(raw_layers: list[dict]) -> tuple[list[tuple[str, str]], dict]:
    """Fold the raw per-layer `tools`/`groups` (user -> project -> local) into
    an ordered op stream and a resolved group table, seeded with the harness
    default base (`all`) and DEFAULT_GROUPS."""
    group_layers = [l.get("groups", {}) for l in raw_layers if isinstance(l, dict)]
    groups = resolve_groups(group_layers)
    ops = expand_tools(CONFIG_DEFAULTS["tools"])
    for layer in raw_layers:
        if isinstance(layer, dict) and "tools" in layer:
            ops += expand_tools(layer["tools"])
    return ops, groups


def uses_bare_all(value) -> bool:
    """True if a `tools:` value uses the deprecated bare `all` (scalar "all"
    or "all"/"@all" appearing un-prefixed in a list). Used by management
    commands to emit a deprecation notice; never called on the hot path."""
    if isinstance(value, str):
        return value.strip().lower() in ("all", "@all")
    if isinstance(value, (list, tuple)):
        return any(str(x).strip().lower() in ("all", "@all") for x in value)
    return False


def _find_bouncer_dir(cwd: Path | None = None) -> Path | None:
    current = (cwd or Path.cwd()).resolve()
    home = HOME.resolve()
    while True:
        candidate = current / PROJECT_DIR_NAME
        if candidate.is_dir():
            return candidate
        if current == home or current.parent == current:
            break
        current = current.parent
    return None


def project_has_bouncer(cwd: Path | None = None) -> bool:
    d = _find_bouncer_dir(cwd)
    return d is not None and (d / "config.yaml").exists()


def project_key(project_dir: Path) -> str:
    """Stable per-project key for file state under ~/.local/share/bouncer/.

    Shared by the escalation grant and the session profile so both are keyed
    on the same thing: the resolved project dir, which every harness gives us
    (unlike `session_id`, which several omit)."""
    return hashlib.sha1(str(project_dir.resolve()).encode("utf-8")).hexdigest()[:16]


def known_profile_names(raw_layers: list[dict]) -> list[str]:
    """Every profile name a config layer defines, plus the two that ship.

    The accepted-word set is closed: `bouncer profile <token>` only sets a name
    that appears here, because an unrecognised token is never an action."""
    from .profile import BUILTIN_PROFILE_NAMES
    names = list(BUILTIN_PROFILE_NAMES)
    for layer in raw_layers:
        if not isinstance(layer, dict):
            continue
        block = layer.get("profiles")
        if isinstance(block, dict):
            for name in block:
                if str(name) not in names:
                    names.append(str(name))
    return names


def _config_layers(cwd: Path | None = None) -> list[dict]:
    """The raw config layers in fold order: user -> project -> local."""
    layers = [load_yaml_config(USER_CONFIG_FILE)]
    d = _find_bouncer_dir(cwd)
    if d:
        layers.append(load_yaml_config(d / "config.yaml"))
        layers.append(load_yaml_config(d / "config.local.yaml"))
    return layers


def _fold_layers(raw_layers: list[dict], profile: str | None) -> dict:
    """Fold the layers, applying each layer's own profile fragment right after
    its base keys:

        defaults -> defaults.profiles[P]
          -> user.base -> user.profiles[P]
          -> project.base -> project.profiles[P]
          -> local.base -> local.profiles[P]

    So the profile fragment beats base keys **within its own layer**, and later
    layers still win — the existing mental model, extended rather than
    replaced. (Merging the profile dead-last would let it override every
    project, but it would break that model to solve a lint problem; `bouncer
    lint` flags the case instead.)"""
    config: dict = {}
    for layer in [CONFIG_DEFAULTS] + list(raw_layers):
        if not isinstance(layer, dict) or not layer:
            continue
        base = {k: v for k, v in layer.items() if k != "profiles"}
        if base:
            config = _deep_merge(config, base)
        if profile:
            block = layer.get("profiles")
            frag = block.get(profile) if isinstance(block, dict) else None
            if isinstance(frag, dict):
                config = _deep_merge(config, frag)
    return config


def _intercept_layers(raw_layers: list[dict], profile: str | None) -> list[dict]:
    """The layer list `resolve_intercept` folds, with each layer's profile
    fragment inserted directly after it (mirroring `_fold_layers`)."""
    out: list[dict] = []
    for layer in raw_layers:
        if not isinstance(layer, dict):
            continue
        out.append(layer)
        if profile:
            block = layer.get("profiles")
            frag = block.get(profile) if isinstance(block, dict) else None
            if isinstance(frag, dict):
                out.append(frag)
    return out


def resolve_profile(cwd: Path | None = None,
                    raw_layers: list[dict] | None = None) -> str:
    """The profile in force for this project.

    Project state names it if set; otherwise `default_profile` does. Absence of
    state means "use `default_profile`", never "use hardcoded behaviour", so
    there is always a profile to name."""
    from . import profile as _profile
    if raw_layers is None:
        raw_layers = _config_layers(cwd)
    d = _find_bouncer_dir(cwd)
    name = _profile.get_profile(d)
    if name and name in known_profile_names(raw_layers):
        return name
    # A stale name (its `profiles:` entry was removed) falls back the same way
    # as no state at all.
    default = _fold_layers(raw_layers, None).get("default_profile")
    if isinstance(default, str) and default:
        return default
    return CONFIG_DEFAULTS["default_profile"]


def _merged_config(cwd: Path | None = None, profile: str | None = None) -> dict:
    raw_layers = _config_layers(cwd)  # ordered: user -> project -> local
    if profile is None:
        profile = resolve_profile(cwd, raw_layers)
    config = _fold_layers(raw_layers, profile)
    config["_profile"] = profile
    # The fold needs the raw per-layer tools/groups; _deep_merge replaces them,
    # so resolve the intercept set separately and stash it for classify. Each
    # layer contributes its base then its profile fragment, in the same order
    # _fold_layers uses, so a `tools:` written into a profile lands where the
    # rest of that profile does.
    try:
        config["_tools_ops"], config["_groups"] = resolve_intercept(
            _intercept_layers(raw_layers, profile))
    except Exception:
        config["_tools_ops"] = expand_tools(CONFIG_DEFAULTS["tools"])
        config["_groups"] = dict(DEFAULT_GROUPS)
    return config


def _build_policy_context(cwd: Path | None = None, config: dict | None = None) -> str:
    if config is None:
        config = _merged_config(cwd)
    user_policy = load_policy(USER_POLICY_FILE)
    d = _find_bouncer_dir(cwd)
    proj_policy = load_policy(d / "policy.md") if d else ""
    proj_local = load_policy(d / "policy.local.md") if d else ""
    policy_mode = config.get("policy_mode", "append")
    if policy_mode == "replace" and (proj_policy or proj_local):
        parts = [p for p in (proj_policy, proj_local) if p]
        return "\n\n".join(parts)
    parts = [p for p in (user_policy, proj_policy, proj_local) if p]
    return "\n\n".join(parts) if parts else "(no policy configured)"


def split_policy_tempfile(text: str) -> tuple[str, str]:
    """Split a unified editor tempfile into (committed, local) policy text."""
    sentinel = POLICY_LOCAL_SENTINEL
    if sentinel in text:
        before, _, after = text.partition(sentinel)
        return before.strip(), after.strip()
    return text.strip(), ""


def build_policy_tempfile(committed: str, local: str) -> str:
    """Build the unified tempfile content from committed and local policy text."""
    committed_block = committed if committed else POLICY_MD_TEMPLATE.strip()
    local_block = local if local else POLICY_LOCAL_MD_TEMPLATE.strip()
    return f"{committed_block}\n\n{POLICY_LOCAL_SENTINEL}\n{local_block}\n"


def project_log_file(cwd: Path | None = None) -> Path | None:
    d = _find_bouncer_dir(cwd)
    return (d / "log.jsonl") if d else None
