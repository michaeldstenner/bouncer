import json
import shutil
import textwrap

from ..tool_catalog import merged_tool_catalog


def _selected_catalog(harness: str | None) -> dict:
    catalog = merged_tool_catalog()
    if not harness:
        return catalog
    key = harness.strip().replace("-", "_")
    return {key: catalog[key]} if key in catalog else {}


def cmd_tools(args):
    catalog = _selected_catalog(getattr(args, "harness", None))

    if getattr(args, "as_format", "plain") == "json":
        print(json.dumps(catalog, indent=2))
        return

    if not catalog:
        print(f"Unknown harness: {getattr(args, 'harness', '')}")
        _print_wrapped("Known harnesses: " + ", ".join(merged_tool_catalog()))
        return

    print("Documented and observed tool names")
    _print_wrapped("Use these names in the config.yaml tools list. Matching is case-insensitive.")
    _print_wrapped("Local observations are learned from harness hook traffic when bouncer runs.")
    print()
    for harness, info in catalog.items():
        print(f"{harness}:")
        _print_tool_list("documented", info.get("documented", []))
        _print_tool_list("observed", info.get("observed", []))
        notes = info.get("notes", [])
        if notes:
            _print_wrapped("note: " + " ".join(notes),
                           initial_indent="  ",
                           subsequent_indent="    ")
        print()


def _terminal_width() -> int:
    return shutil.get_terminal_size(fallback=(88, 24)).columns


def _print_wrapped(text: str, *, initial_indent: str = "", subsequent_indent: str = "") -> None:
    print(textwrap.fill(
        text,
        width=_terminal_width(),
        initial_indent=initial_indent,
        subsequent_indent=subsequent_indent,
        break_long_words=False,
        break_on_hyphens=False,
    ))


def _print_tool_list(label: str, tools: list[str]) -> None:
    value = ", ".join(tools) if tools else "(none)"
    _print_wrapped(
        f"{label}: {value}",
        initial_indent="  ",
        subsequent_indent="    ",
    )
