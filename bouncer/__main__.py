import argparse
import sys

from .commands.init     import cmd_init, cmd_global_init
from .commands.lint     import cmd_lint
from .commands.config   import cmd_config, cmd_policy
from .commands.status   import cmd_status
from .commands.activity import cmd_activity
from .commands.log      import cmd_log
from .commands.check    import cmd_check
from .commands.tools    import cmd_tools
from .commands.classify import cmd_classify
from .commands.review   import cmd_review
from .commands.abort    import cmd_abort


_AGENT_HELP = """\
Bouncer is an LLM-powered permission classifier that intercepts tool calls
from AI coding agents. Each call is classified ALLOW / DENY / ASK against a
plain-text project policy.

── Two workflow shapes ───────────────────────────────────────────────────────
Some harnesses have ASK available, and some do not.

- If ASK is available, bouncer uses a three-option workflow: ALLOW / DENY / ASK.
  A DENY can be retried with `# ESCALATE:` to send the request to the user.
- If ASK is not available, bouncer uses a delivered two-option workflow:
  ALLOW / DENY. The LLM may still internally return ASK, but bouncer delivers
  it outward as a DENY with guidance to find another way or suggest a policy
  change.

── Escalating to the user (only when ASK is available) ──────────────────────
ESCALATE is a retry mechanism, not a preemptive one. Submit the command
normally first. If bouncer DENYs it and you believe that denial is wrong,
retry the exact same command with an ESCALATE comment explaining why:

    # ESCALATE: clearing stale build artifacts before release
    rm -rf dist/ build/

Bouncer skips the LLM and forwards the request to the user for a decision.
This is enforced: an ESCALATE for a command you have not already submitted
normally is rejected. Run the command first; escalate only if it is denied.

Current harness behavior:
  * Claude Code — ASK is available.
  * Codex PermissionRequest — ASK is available by abstaining and letting Codex
    show its normal approval prompt.
  * opencode — ASK is available by abstaining from the native permission
    prompt; optional plugin config can delay automatic ALLOW/DENY replies.
  * Codex legacy PreToolUse / shell shim — ASK is not available; outward ASKs
    are delivered as denials or pass-through depending on integration.

Do not use ESCALATE preemptively. Trying to anticipate what bouncer will
decide wastes tokens and defeats the purpose of automated classification —
most commands are allowed without user involvement.

── Policy suggestions ───────────────────────────────────────────────────────
If an operation is routine for this project but keeps getting denied, suggest
a policy addition to the user. Policy is plain prose fed to an LLM — describe
intent, scope, and expected effects rather than listing specific commands or
paths. The bouncer LLM reasons about what a project legitimately does.

Good:  "This project manages the user's shell environment and may read and
        write files under ~/.config/ and ~/.local/share/."
Poor:  "Allow rm, allow ~/.zshrc, allow pip install"

To propose a policy change, tell the user what you'd add and why, then ask
if they'd like you to append it to .bouncer/policy.md. Do not edit bouncer
config or policy files directly — the agent cannot set its own permission
scope.

── User-level policy ────────────────────────────────────────────────────────
~/.config/bouncer/policy.md applies across all projects. Project-level policy
is in .bouncer/policy.md and is appended by default (policy_mode: append).
"""


def _cmd_agent_help():
    print(_AGENT_HELP)


def main():
    # Name the app and point it at bouncer's config dir so API keys / URLs /
    # parallel_slots resolve from ~/.config/bouncer/config.yaml as a top-priority
    # overlay on ~/.config/llmclient/. Logs land in ~/.local/share/bouncer/.
    # The slot queue is shared at ~/.local/state/llmclient/queue.db (0.9.0+).
    from .llmclient import configure
    from .config import USER_CONFIG_DIR
    configure(app="bouncer", config_dir=USER_CONFIG_DIR)

    parser = argparse.ArgumentParser(
        prog="bouncer",
        description="AI agent permission classifier and manager\n"
                    "Coding agents: run 'bouncer --agent-help' for instructions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--agent-help", action="store_true",
        help="print instructions for AI coding agents and exit",
    )
    parser.add_argument(
        "-g", "--global", dest="user", action="store_true",
        help="operate on user-scope config (~/.config/bouncer/); "
             "for 'init': also creates config/policy and wires harness hooks",
    )

    sub = parser.add_subparsers(dest="cmd_name", metavar="command")

    p_init = sub.add_parser("init", help="create .bouncer template in current project  "
                            "(-g: user-level setup + harness hooks)")
    p_init.add_argument(
        "--harness", metavar="NAME",
        help="wire AI harness hooks: auto | all | claude_code | codex | opencode | shim  "
             "(comma-separated for multiple; -g only: prompts if omitted)",
    )

    p_lint = sub.add_parser("lint", help="validate config.yaml")
    p_lint.add_argument("file", nargs="?", help="file to lint (default: project config.yaml)")

    p_config = sub.add_parser("config", help="open config.yaml in $EDITOR  (-e/-d: enable/disable, -t/-a: tools)")
    p_config_tog = p_config.add_mutually_exclusive_group()
    p_config_tog.add_argument("-e", "--enable",  action="store_true", help="set enabled: true")
    p_config_tog.add_argument("-d", "--disable", action="store_true", help="set enabled: false")
    p_config_tog.add_argument("-t", "--tools", metavar="LIST",
                              help="set intercepted tools (comma-separated, e.g. bash,read)")
    p_config_tog.add_argument("-a", "--all", dest="all_tools", action="store_true",
                              help="intercept all tools (sets 'tools: all')")
    sub.add_parser("policy", help="open policy.md in $EDITOR")

    p_status = sub.add_parser("status", help="show bouncer status (use -v for full detail)")
    p_status.add_argument("-v", "--verbose", action="store_true",
                          help="show full config breakdown")

    p_activity = sub.add_parser("activity", help="print recent-decision indicator")
    p_activity.add_argument("--session", metavar="ID",
                            help="accepted for compatibility; no longer used")
    p_activity.add_argument("--project", action="store_true",
                            help="accepted for compatibility; the strip always "
                                 "renders from the project log")
    p_activity.add_argument("--cwd", metavar="PATH",
                            help="project directory (for inactive indicator)")
    p_activity.add_argument("--width", metavar="N", type=int, default=None,
                            help="number of recent decisions to show "
                                 "(default: config activity_width, or 10)")
    p_activity.add_argument("--as", dest="as_format", metavar="FORMAT",
                            choices=["plain", "ansi", "json", "tmux"], default="plain",
                            help="output format: plain (default), ansi, json, or tmux")

    p_log = sub.add_parser("log", help="view decision log")
    p_log.add_argument("--break", dest="mark_break", action="store_true",
                       help="append a break marker (called from UserPromptSubmit hook)")
    p_log.add_argument("--filter", dest="filter_dec", metavar="DECISION",
                       help="filter by decision: allow, deny, unsure")
    p_log.add_argument("--since", metavar="DURATION",
                       help="show entries since (e.g. 1h, 30m, 2d)")
    p_log.add_argument("--tail", action="store_true", help="follow log in real time")
    p_log.add_argument("-S", dest="pager", action="store_true",
                       help="open in less -RS (no line wrap)")
    p_log.add_argument("--columns", metavar="N", type=int,
                       help="force output width (default: terminal width)")

    p_check = sub.add_parser("check", help="dry-run: what would bouncer decide?")
    p_check.add_argument("cmd", metavar="command", help="command to evaluate")
    p_check.add_argument("--llm", action="store_true",
                         help="actually call the LLM to get a decision")

    p_tools = sub.add_parser("tools", help="list observed harness tool names")
    p_tools.add_argument("--harness", metavar="NAME",
                         help="show one harness: claude_code | codex | opencode | shim")
    p_tools.add_argument("--as", dest="as_format", metavar="FORMAT",
                         choices=["plain", "json"], default="plain",
                         help="output format: plain (default) or json")

    p_classify = sub.add_parser("classify", help="internal hook interface")
    p_classify.add_argument("--hook", action="store_true", required=True,
                             help="read hook JSON from stdin, write response to stdout")
    p_classify.add_argument("--format", dest="format", default="json",
                             choices=["json", "plain", "codex-permission", "codex-pretool"],
                             help="output format: json (default), plain, codex-permission, or codex-pretool")

    p_review = sub.add_parser("review", help="interactive review of UNSURE decisions")
    p_review.add_argument("--all", action="store_true", help="review all decisions (ALLOW, DENY, UNSURE)")
    p_review.add_argument("--deny", action="store_true", help="review only DENY decisions")

    sub.add_parser("abort",
                   help="abort pending LLM classification in this project → ALLOW")

    args = parser.parse_args()

    if args.agent_help:
        _cmd_agent_help()
        return

    if not args.cmd_name:
        parser.print_help()
        sys.exit(0)

    if args.user and args.cmd_name == "init":
        cmd_global_init(args)
        return

    dispatch = {
        "init":     cmd_init,
        "lint":     cmd_lint,
        "config":   cmd_config,
        "policy":   cmd_policy,
        "status":   cmd_status,
        "activity": cmd_activity,
        "log":      cmd_log,
        "check":    cmd_check,
        "tools":    cmd_tools,
        "classify": cmd_classify,
        "review":   cmd_review,
        "abort":    cmd_abort,
    }

    dispatch[args.cmd_name](args)


if __name__ == "__main__":
    main()
