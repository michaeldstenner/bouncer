import sys
from pathlib import Path

from ..colors import RESET, BOLD, GREEN, RED, YELLOW, DIM
from ..config import (_find_bouncer_dir, load_yaml_config, USER_CONFIG_FILE,
                      expand_tools, uses_bare_all)
from ..profile import (BUILTIN_PROFILE_NAMES, NO_ASK_HARNESSES, _truthy,
                       harness_has_unattended_floor)
from .init import installed_harnesses

VALID_CONFIG_KEYS  = frozenset({"enabled", "tools", "groups", "policy_mode", "llm", "review",
                                 "on_unsure", "on_unavailable", "log",
                                 "escalation",
                                 "escalation_requires_attempt",
                                 "escalation_attempt_ttl",
                                 "default_profile", "profiles",
                                 "activity_width", "activity", "notify",
                                 # provider-keyed sections consumed by the
                                 # vendored llmclient for key/URL resolution
                                 "openai", "openai_compatible", "anthropic",
                                 "ollama"})
VALID_POLICY_MODES = ("append", "replace")
VALID_ACTIONS      = ("ask", "allow", "deny", "abstain")
VALID_VERBOSITIES  = ("all", "deny_only", "off")
VALID_REVIEW_PROVIDERS = (
    "ollama", "openai", "openai_compatible", "openrouter", "anthropic",
)

# Keys a profile fragment may carry. A profile changes the plumbing — what
# happens when bouncer cannot decide, and whether an agent may appeal — never
# the judgment, so policy_mode and the LLM backend are not on the list.
VALID_PROFILE_KEYS = frozenset({"escalation", "on_unsure", "on_unavailable",
                                 "escalation_requires_attempt",
                                 "escalation_attempt_ttl",
                                 "notify", "log"})


def _floorless_harnesses() -> list[str]:
    """Installed integrations that can never abstain into an unattended
    floor, in any session.

    Asked in the harness's best case (`auto`), because lint sees config, not
    a running session: whether a *given* Claude Code session's abstain
    reaches auto-mode's classifier depends on its permission mode, which only
    the hook payload carries."""
    return [h for h in installed_harnesses()
            if not harness_has_unattended_floor(h, "auto")]


def _no_ask_harnesses() -> list[str]:
    """Installed integrations with no way to reach a human at all."""
    return [h for h in installed_harnesses() if h in NO_ASK_HARNESSES]


def _lint_profiles(data: dict, errors: list, warnings: list) -> None:
    """Validate `default_profile` / `profiles:`, and warn about the two ways a
    profile can quietly not do what it says (design item 10)."""
    profiles = data.get("profiles")
    if profiles is not None and not isinstance(profiles, dict):
        errors.append(f"'profiles' must be a map of name -> settings, "
                      f"got: {profiles!r}")
        profiles = None

    known = list(BUILTIN_PROFILE_NAMES)
    for name in (profiles or {}):
        if str(name) not in known:
            known.append(str(name))

    default = data.get("default_profile")
    if default is not None:
        if not isinstance(default, str) or default not in known:
            errors.append(f"'default_profile' must name a known profile "
                          f"{tuple(known)}, got: {default!r}")
        else:
            print(f"  default_profile: {default}")

    if default is None and not profiles and "profiles" not in data:
        return

    listed = [str(k) for k in profiles] if profiles else known
    print(f"  profiles:      {', '.join(listed)}")

    for name, frag in (profiles or {}).items():
        if not isinstance(frag, dict):
            errors.append(f"'profiles.{name}' must be a map of settings, "
                          f"got: {frag!r}")
            continue
        for key in frag:
            if key not in VALID_PROFILE_KEYS:
                warnings.append(
                    f"'profiles.{name}.{key}' is not a profile setting — a "
                    f"profile changes the plumbing, not the judgment")
        for key in ("on_unsure", "on_unavailable"):
            if key in frag and frag[key] not in VALID_ACTIONS:
                errors.append(f"'profiles.{name}.{key}' must be one of "
                              f"{VALID_ACTIONS}, got: {frag[key]!r}")
        if "escalation" in frag and not isinstance(frag["escalation"], bool):
            errors.append(f"'profiles.{name}.escalation' must be on or off, "
                          f"got: {frag['escalation']!r}")
            continue

        # (b) a profile meant for unattended use that re-enables escalation.
        # The layering deliberately lets a later layer win, so this is the
        # place the mistake gets caught rather than being made impossible.
        if name == "solo" and _truthy(frag.get("escalation"), False):
            warnings.append(
                "'profiles.solo.escalation' is on — solo means no ASK is "
                "ever produced, so a session with nobody watching can stall "
                "waiting for an answer. Remove it, or use a differently-named "
                "profile.")

        floorless = _floorless_harnesses()
        if (not _truthy(frag.get("escalation"), True)
                and frag.get("on_unsure") == "abstain" and floorless):
            warnings.append(
                f"'profiles.{name}' abstains with escalation off, but "
                f"{', '.join(floorless)} "
                f"{'has' if len(floorless) == 1 else 'have'} no unattended "
                f"floor to abstain into — bouncer resolves those to deny "
                f"instead. See docs/integrations.md.")

    # (a) the configured profile cannot be honoured by the harness in use.
    if default == "live":
        no_ask = _no_ask_harnesses()
        if no_ask:
            warnings.append(
                f"default_profile is 'live', but {', '.join(no_ask)} cannot "
                f"ask a human at all — sessions on that harness run as "
                f"'solo' whatever the profile says. 'bouncer profile' shows "
                f"the effective state.")


def cmd_lint(args):
    if args.file:
        path = Path(args.file)
    elif getattr(args, "user", False):
        path = USER_CONFIG_FILE
        if not path.exists():
            print(f"{YELLOW}No global config found.{RESET} Run 'bouncer -g init' first.")
            sys.exit(1)
    else:
        d = _find_bouncer_dir()
        if d is None or not (d / "config.yaml").exists():
            print(f"{YELLOW}No project config found.{RESET} Run 'bouncer init' first.")
            sys.exit(1)
        path = d / "config.yaml"

    if not path.exists():
        print(f"{RED}Error:{RESET} File not found: {path}")
        sys.exit(1)

    data     = load_yaml_config(path)
    errors   = []
    warnings = []

    print(f"{BOLD}{path}{RESET}")

    for k in data:
        if k not in VALID_CONFIG_KEYS:
            warnings.append(f"Unknown key: '{k}'")

    enabled = data.get("enabled", True)
    print(f"  enabled:       {GREEN if enabled else DIM}{enabled}{RESET}")

    if "tools" in data:
        tools = data["tools"]
        if not isinstance(tools, (str, list)):
            errors.append(f"'tools' must be a string or a list of ±ops, got: {tools!r}")
        else:
            ops = expand_tools(tools)
            label = " ".join(f"{s}{sel}" for s, sel in ops) or f"{DIM}(empty){RESET}"
            print(f"  tools:         {label}")
            if uses_bare_all(tools):
                warnings.append(
                    "bare 'all' is deprecated — it now means '+@all -@internal' "
                    "(harness plumbing like ToolSearch is skipped). Write it "
                    "explicitly, e.g. tools: [+@all, -@internal], or use '@all' "
                    "if you really mean every tool including plumbing.")

    groups = data.get("groups")
    if groups is not None and not isinstance(groups, dict):
        errors.append(f"'groups' must be a map of name -> ±members, got: {groups!r}")
    elif groups:
        print(f"  groups:        {', '.join(str(k) for k in groups)}")

    pm = data.get("policy_mode", "append")
    if pm not in VALID_POLICY_MODES:
        errors.append(f"'policy_mode' must be one of {VALID_POLICY_MODES}, got: {pm!r}")
    else:
        print(f"  policy_mode:   {pm}")

    for key in ("on_unsure", "on_unavailable"):
        val = data.get(key, "ask")
        if val not in VALID_ACTIONS:
            errors.append(f"'{key}' must be one of {VALID_ACTIONS}, got: {val!r}")
        else:
            print(f"  {key:<16} {val}")

    _lint_profiles(data, errors, warnings)

    if "escalation_requires_attempt" in data:
        era = data["escalation_requires_attempt"]
        if not isinstance(era, bool):
            errors.append(
                f"'escalation_requires_attempt' must be true or false, got: {era!r}")
        else:
            print(f"  {'escalation_requires_attempt':<16} {era}")

    if "escalation_attempt_ttl" in data:
        ttl = data["escalation_attempt_ttl"]
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            errors.append(
                f"'escalation_attempt_ttl' must be a positive integer (seconds), got: {ttl!r}")
        else:
            print(f"  {'escalation_attempt_ttl':<16} {ttl}s")

    llm = data.get("llm", {})
    if llm:
        provider = llm.get("provider", "ollama")
        model    = llm.get("model")
        print(f"  llm:           provider={provider}, model={model or '?'}")
        if not model:
            warnings.append(
                "'llm.model' is not set — it is required and has no default. "
                "Set it here or in ~/.config/bouncer/config.yaml.")

    review = data.get("review")
    if review is not None:
        if not isinstance(review, dict):
            errors.append("'review' must be a map")
        else:
            review_llm = review.get("llm", {})
            if not isinstance(review_llm, dict):
                errors.append("'review.llm' must be a map")
            elif not review_llm.get("model"):
                errors.append("'review.llm.model' is required when 'review' is set")
            else:
                review_provider = review_llm.get("provider", llm.get("provider", "ollama"))
                print("  review.llm:    provider="
                      f"{review_provider}, "
                      f"model={review_llm['model']}")
                if review_provider not in VALID_REVIEW_PROVIDERS:
                    errors.append("'review.llm.provider' must be a direct model "
                                  f"provider in {VALID_REVIEW_PROVIDERS}, got: "
                                  f"{review_provider!r}")
            if path.parent.name == ".bouncer":
                errors.append("'review' is user-config only; move it to "
                              "~/.config/bouncer/config.yaml")

    log_cfg = data.get("log", {})
    if log_cfg:
        v = log_cfg.get("verbosity", "all")
        if v not in VALID_VERBOSITIES:
            errors.append(f"'log.verbosity' must be one of {VALID_VERBOSITIES}, got: {v!r}")
        me = log_cfg.get("max_entries")
        dbg = log_cfg.get("llm_debug")
        print(f"  log:           verbosity={v}"
              + (f", max_entries={me}" if me else "")
              + (f", llm_debug={dbg}" if dbg is not None else ""))

    notify_cfg = data.get("notify")
    if notify_cfg:
        if isinstance(notify_cfg, str):
            print("  notify:        command=<shell>")
        elif isinstance(notify_cfg, dict):
            command = notify_cfg.get("command")
            if not command:
                errors.append("'notify.command' is required when 'notify' is set")
            decisions = notify_cfg.get("decisions", "all")
            print(f"  notify:        decisions={decisions}")
        else:
            errors.append("'notify' must be a command string or map")

    for w in warnings:
        print(f"  {YELLOW}⚠{RESET}  {w}")
    for e in errors:
        print(f"  {RED}✗{RESET}  {e}")

    if not errors:
        print(f"  {GREEN}✓{RESET} config valid")
    if errors:
        sys.exit(1)
