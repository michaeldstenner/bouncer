import sys
from pathlib import Path

from ..colors import RESET, BOLD, GREEN, RED, YELLOW, DIM
from ..config import _find_bouncer_dir, load_yaml_config

VALID_CONFIG_KEYS  = frozenset({"enabled", "tools", "policy_mode", "llm",
                                 "on_unsure", "on_unavailable", "log",
                                 "activity_width", "activity", "notify",
                                 # provider-keyed sections consumed by the
                                 # vendored llmclient for key/URL resolution
                                 "openai", "openai_compatible", "anthropic",
                                 "ollama"})
VALID_POLICY_MODES = ("append", "replace")
VALID_ACTIONS      = ("ask", "allow", "deny", "abstain")
VALID_VERBOSITIES  = ("all", "deny_only", "off")


def cmd_lint(args):
    if args.file:
        path = Path(args.file)
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

    tools = data.get("tools", "all")
    if tools == "all":
        print(f"  tools:         {YELLOW}all{RESET}")
    elif isinstance(tools, list):
        label = ", ".join(tools) if tools else f"{DIM}(none — bouncer won't intercept anything){RESET}"
        print(f"  tools:         {label}")
    else:
        errors.append(f"'tools' must be a list or the string \"all\", got: {tools!r}")

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

    llm = data.get("llm", {})
    if llm:
        provider = llm.get("provider", "ollama")
        model    = llm.get("model")
        print(f"  llm:           provider={provider}, model={model or '?'}")
        if not model:
            warnings.append(
                "'llm.model' is not set — it is required and has no default. "
                "Set it here or in ~/.config/bouncer/config.yaml.")

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
