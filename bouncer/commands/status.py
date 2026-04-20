import sys
from pathlib import Path

from ..colors import RESET, BOLD, GREEN, RED, YELLOW, DIM
from ..config import (
    USER_CONFIG_FILE,
    USER_POLICY_FILE,
    USER_LOG_FILE,
    _find_bouncer_dir,
    _merged_config,
    load_yaml_config,
    load_policy,
    project_log_file,
)


def _print_config_summary(data: dict, prefix: str = "  ") -> None:
    enabled   = data.get("enabled", True)
    tools     = data.get("tools", ["Bash"])
    tools_str = "all" if tools == "all" else (", ".join(tools) if tools else "(none)")
    pm        = data.get("policy_mode", "append")
    llm       = data.get("llm", {})
    on_u      = data.get("on_unsure", "ask")
    on_na     = data.get("on_unavailable", "ask")
    log_cfg   = data.get("log", {})

    print(f"{prefix}enabled:        {GREEN if enabled else DIM}{enabled}{RESET}")
    print(f"{prefix}tools:          {tools_str}")
    print(f"{prefix}policy_mode:    {pm}")
    print(f"{prefix}llm:            {llm.get('provider', 'ollama')} / {llm.get('model', '?')}")
    print(f"{prefix}on_unsure:      {on_u}")
    print(f"{prefix}on_unavailable: {on_na}")
    v  = log_cfg.get("verbosity", "all")
    me = log_cfg.get("max_entries", "?")
    print(f"{prefix}log:            verbosity={v}, max_entries={me}")


def cmd_status(args):
    cwd    = Path.cwd()
    config = _merged_config(cwd)

    if getattr(args, "verbose", False):
        _cmd_status_verbose(config, cwd)
        return

    enabled   = config.get("enabled", True)
    tools     = config.get("tools", ["Bash"])
    tools_str = "all" if tools == "all" else (", ".join(tools) if tools else "(none)")
    llm       = config.get("llm", {})
    model_str = f"{llm.get('provider', 'ollama')}/{llm.get('model', '?')}"
    on_u      = config.get("on_unsure", "ask")
    on_na     = config.get("on_unavailable", "ask")
    d         = _find_bouncer_dir(cwd)
    has_proj  = d is not None and (d / "config.yaml").exists()

    if not enabled:
        print(f"{DIM}○ bouncer disabled{RESET}")
        return

    if not has_proj:
        print(f"{YELLOW}○ bouncer inactive{RESET}  no project config  "
              f"{DIM}(run 'bouncer init'){RESET}")
        return

    proj_name = d.parent.name
    print(f"{GREEN}● bouncer active{RESET}  "
          f"{BOLD}{tools_str}{RESET} via {model_str}  "
          f"{DIM}[{proj_name}]{RESET}")
    print(f"  unsure→{on_u}  unavailable→{on_na}")


def _cmd_status_verbose(config: dict, cwd: Path) -> None:
    print(f"{BOLD}User config:{RESET}  {USER_CONFIG_FILE}")
    if USER_CONFIG_FILE.exists():
        _print_config_summary(load_yaml_config(USER_CONFIG_FILE))
    else:
        print(f"  {DIM}(not found — run 'bouncer -g config' to create){RESET}")

    print(f"{BOLD}User policy:{RESET}  {USER_POLICY_FILE}")
    user_policy = load_policy(USER_POLICY_FILE)
    if user_policy:
        wc    = len(user_policy.split())
        first = user_policy.splitlines()[0][:72]
        print(f"  {DIM}{wc} words — {first}{RESET}")
    else:
        print(f"  {DIM}(empty — run 'bouncer -g policy'){RESET}")

    print()

    d = _find_bouncer_dir(cwd)
    if d:
        print(f"{BOLD}Project config:{RESET} {d / 'config.yaml'}")
        if (d / "config.yaml").exists():
            _print_config_summary(load_yaml_config(d / "config.yaml"))
        else:
            print(f"  {DIM}(not found — run 'bouncer init'){RESET}")

        print(f"{BOLD}Project policy:{RESET} {d / 'policy.md'}")
        proj_policy = load_policy(d / "policy.md")
        if proj_policy:
            wc    = len(proj_policy.split())
            first = proj_policy.splitlines()[0][:72]
            print(f"  {DIM}{wc} words — {first}{RESET}")
        else:
            print(f"  {DIM}(empty — run 'bouncer policy'){RESET}")
    else:
        print(f"{BOLD}Project config:{RESET} "
              f"{DIM}(not found — bouncer inactive for this project){RESET}")

    print()
    print(f"{BOLD}Effective config:{RESET}")
    _print_config_summary(config)

    print()
    plog = project_log_file(cwd)
    if plog and plog.exists():
        count = sum(1 for _ in open(plog, encoding="utf-8"))
        print(f"{BOLD}Project log:{RESET} {plog} ({count} entries)")
    elif plog:
        print(f"{BOLD}Project log:{RESET} {DIM}{plog} (not yet created){RESET}")
    if USER_LOG_FILE.exists():
        count = sum(1 for _ in open(USER_LOG_FILE, encoding="utf-8"))
        print(f"{BOLD}User log:{RESET}    {USER_LOG_FILE} ({count} entries)")
