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
from .init import format_installed_harnesses


def _print_config_summary(data: dict, prefix: str = "  ") -> None:
    enabled   = data.get("enabled", True)
    tools     = data.get("tools", "all")
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


def _probe_llm(config: dict) -> None:
    """Best-effort reachability check for the configured LLM backend.

    Never raises; uses short timeouts. For Ollama it pings the server and
    reports whether the configured model is installed. For cloud providers it
    only reports whether an API key resolves (no billable request is made).
    """
    import json
    import urllib.request
    import urllib.error
    from ..llmclient._keys import resolve_url, resolve_api_key

    llm      = config.get("llm", {})
    provider = llm.get("provider", "ollama")
    model    = llm.get("model")
    url      = resolve_url(provider, llm.get("url", ""))

    print()
    print(f"{BOLD}LLM backend:{RESET}   {provider} / {model or '?'}")
    if url:
        print(f"  url:          {url}")

    if not model:
        print(f"  {YELLOW}⚠ llm.model is not set{RESET} — required, no default "
              f"{DIM}(set in 'bouncer -g config'){RESET}")

    if provider == "ollama":
        try:
            req = urllib.request.Request(url + "/api/tags")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            print(f"  reachable:    {RED}✗{RESET} could not reach {url} {DIM}({exc}){RESET}")
            return
        names = [m.get("name") or m.get("model", "") for m in data.get("models", [])]
        print(f"  reachable:    {GREEN}✓{RESET} {len(names)} model(s) installed")
        if model:
            prefix  = model.split(":")[0]
            present = any(n == model or n.startswith(prefix) for n in names)
            if present:
                print(f"  model:        {GREEN}✓{RESET} {model} installed")
            else:
                print(f"  model:        {RED}✗{RESET} {model} not installed "
                      f"{DIM}(run 'ollama pull {model}'){RESET}")
    elif provider in ("openai", "openai_compatible", "anthropic"):
        key = resolve_api_key(provider, llm.get("api_key", ""))
        if key:
            print(f"  api key:      {GREEN}✓{RESET} found")
        else:
            print(f"  api key:      {RED}✗{RESET} not found "
                  f"{DIM}(set env var, llm.api_key, or a provider section "
                  f"in ~/.config/bouncer/config.yaml){RESET}")


def cmd_status(args):
    cwd    = Path.cwd()
    config = _merged_config(cwd)

    if getattr(args, "verbose", False):
        _cmd_status_verbose(config, cwd)
        return

    enabled   = config.get("enabled", True)
    tools     = config.get("tools", "all")
    tools_str = "all" if tools == "all" else (", ".join(tools) if tools else "(none)")
    llm       = config.get("llm", {})
    model_str = f"{llm.get('provider', 'ollama')}/{llm.get('model', '?')}"
    on_u      = config.get("on_unsure", "ask")
    on_na     = config.get("on_unavailable", "ask")
    d         = _find_bouncer_dir(cwd)
    has_proj  = d is not None and (d / "config.yaml").exists()

    if not enabled:
        print(f"{DIM}○ bouncer disabled{RESET}")
        print(f"  integrations: {format_installed_harnesses()}")
        return

    if not has_proj:
        print(f"{YELLOW}○ bouncer inactive{RESET}  no project config  "
              f"{DIM}(run 'bouncer init'){RESET}")
        print(f"  integrations: {format_installed_harnesses()}")
        return

    proj_name = d.parent.name
    print(f"{GREEN}● bouncer active{RESET}  "
          f"{BOLD}{tools_str}{RESET} via {model_str}  "
          f"{DIM}[{proj_name}]{RESET}")
    print(f"  unsure→{on_u}  unavailable→{on_na}")
    print(f"  integrations: {format_installed_harnesses()}")


def _cmd_status_verbose(config: dict, cwd: Path) -> None:
    print(f"{BOLD}User config:{RESET}  {USER_CONFIG_FILE}")
    if USER_CONFIG_FILE.exists():
        user_config = load_yaml_config(USER_CONFIG_FILE)
        _print_config_summary(user_config)
        review_cfg = user_config.get("review", {})
        review_llm = review_cfg.get("llm", {}) if isinstance(review_cfg, dict) else {}
        if review_llm:
            classifier_llm = user_config.get("llm", {})
            classifier_provider = (classifier_llm.get("provider", "ollama")
                                   if isinstance(classifier_llm, dict) else "ollama")
            print(f"  review.llm:     {review_llm.get('provider', classifier_provider)} / "
                  f"{review_llm.get('model', '?')}")
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
    print(f"{BOLD}Installed integrations:{RESET} {format_installed_harnesses()}")

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

    _probe_llm(config)
