_TOOL_CHARS = {
    "Bash":      "B",
    "Write":     "W",
    "Edit":      "E",
    "Read":      "R",
    "Glob":      "G",
    "Grep":      "G",
    "Task":      "T",
    "WebFetch":  "F",
    "WebSearch": "S",
}

_ACTIVITY_COLORS = {
    "ALLOW":    "\033[32m",
    "DENY":     "\033[31m",
    "BLOCK":    "\033[31m",
    "UNSURE":   "\033[35m",
    "TIMEOUT":  "\033[30;45m",
    "LLM_ERROR": "\033[30;41m",
    "ESCALATE": "\033[36m",
}
_ACTIVITY_RESET = "\033[0m"

_TMUX_COLORS = {
    "ALLOW":    "#[fg=green]",
    "DENY":     "#[fg=red]",
    "BLOCK":    "#[fg=red]",
    "UNSURE":   "#[fg=magenta]",
    "TIMEOUT":  "#[bg=magenta,fg=black,bold]",
    "LLM_ERROR": "#[bg=red,fg=black,bold]",
    "ESCALATE": "#[fg=cyan]",
}
_TMUX_RESET = "#[default]"

_ANSI_STYLE_NAMES = {
    "black": "\033[30m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "black_on_red_bold": "\033[30;41;1m",
    "black_on_magenta_bold": "\033[30;45;1m",
}

_TMUX_STYLE_NAMES = {
    "black": "#[fg=black]",
    "red": "#[fg=red]",
    "green": "#[fg=green]",
    "yellow": "#[fg=yellow]",
    "blue": "#[fg=blue]",
    "magenta": "#[fg=magenta]",
    "cyan": "#[fg=cyan]",
    "white": "#[fg=white]",
    "dim": "#[dim]",
    "bold": "#[bold]",
    "black_on_red_bold": "#[bg=red,fg=black,bold]",
    "black_on_magenta_bold": "#[bg=magenta,fg=black,bold]",
}


def _tool_char(tool_name: str) -> str:
    key = tool_name.capitalize()
    return _TOOL_CHARS.get(key) or _TOOL_CHARS.get(tool_name, "?")


def _style_value(value: object, names: dict[str, str]) -> str:
    if not isinstance(value, str):
        return ""
    return names.get(value, "")


def _format_colors(as_format: str, cfg: dict | None = None) -> tuple[dict[str, str], str]:
    if as_format == "ansi":
        defaults = _ACTIVITY_COLORS
        reset = _ACTIVITY_RESET
        names = _ANSI_STYLE_NAMES
    elif as_format == "tmux":
        defaults = _TMUX_COLORS
        reset = _TMUX_RESET
        names = _TMUX_STYLE_NAMES
    else:
        return {}, ""

    colors = dict(defaults)
    activity_cfg = (cfg or {}).get("activity", {})
    overrides = activity_cfg.get("colors", {}) if isinstance(activity_cfg, dict) else {}
    if isinstance(overrides, dict):
        for decision, value in overrides.items():
            if isinstance(value, dict):
                continue
            colors[str(decision).upper()] = _style_value(value, names)
    return colors, reset


def _render_activity(entries: list[dict], as_format: str = "plain", cfg: dict | None = None) -> str:
    if as_format == "json":
        import json as _json
        items = []
        for e in entries:
            decision = e.get("d", "?").upper()
            if decision == "BREAK":
                items.append({"c": "·", "d": "break"})
            else:
                items.append({"c": _tool_char(e.get("t", "?")), "d": decision.lower()})
        return _json.dumps(items)

    # ansi and tmux share the same color-map lookup; plain returns ({}, "").
    colors, format_reset = _format_colors(as_format, cfg)
    parts = []
    for e in entries:
        decision = e.get("d", "?").upper()
        if decision == "BREAK":
            if as_format == "ansi":
                parts.append("\033[2m·\033[0m")
            elif as_format == "tmux":
                parts.append("#[dim]·#[default]")
            else:
                parts.append("·")
            continue
        char  = _tool_char(e.get("t", "?"))
        color = colors.get(decision, "")
        reset = format_reset if color else ""
        parts.append(f"{color}{char}{reset}")
    return "".join(parts)
