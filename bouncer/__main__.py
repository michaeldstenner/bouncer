import argparse
import sys

from .commands.init     import cmd_init, cmd_global_init
from .commands.lint     import cmd_lint
from .commands.config   import cmd_config, cmd_policy
from .commands.status   import cmd_status
from .commands.activity import cmd_activity
from .commands.log      import cmd_log
from .commands.check    import cmd_check
from .commands.classify import cmd_classify
from .commands.review   import cmd_review


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
When you need something the policy doesn't cover — or believe a denial is
wrong in an ASK-capable harness — prefix the command with an ESCALATE comment
explaining why:

    # ESCALATE: clearing stale build artifacts before release
    rm -rf dist/ build/

Bouncer skips the LLM and forwards the request to the user for a decision.

Current harness behavior:
  * Claude Code / Codex — ASK is available.
  * opencode / shell shim — ASK is not available; outward ASKs are delivered
    as denials.

Use ESCALATE sparingly; it is not a way to force bouncer to approve something.

── Policy suggestions ───────────────────────────────────────────────────────
If an operation is routine for this project but keeps getting denied, suggest
a policy addition to the user. Policy is plain prose fed to an LLM — describe
intent, scope, and expected effects rather than listing specific commands or
paths. The bouncer LLM reasons about what a project legitimately does.

Good:  "This project manages the user's shell environment and may read and
        write files under ~/.config/ and ~/.local/share/."
Poor:  "Allow rm, allow ~/.zshrc, allow pip install"

To propose a policy change, tell the user what you'd add and why, then ask
if they'd like you to append it to .bouncer/policy.md.

── User-level policy ────────────────────────────────────────────────────────
~/.config/bouncer/policy.md applies across all projects. Project-level policy
is in .bouncer/policy.md and is appended by default (policy_mode: append).
"""


def _cmd_agent_help():
    print(_AGENT_HELP)


def main():
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

    p_config = sub.add_parser("config", help="open config.yaml in $EDITOR  (-e/-d: enable/disable)")
    p_config_tog = p_config.add_mutually_exclusive_group()
    p_config_tog.add_argument("-e", "--enable",  action="store_true", help="set enabled: true")
    p_config_tog.add_argument("-d", "--disable", action="store_true", help="set enabled: false")
    sub.add_parser("policy", help="open policy.md in $EDITOR")

    p_status = sub.add_parser("status", help="show bouncer status (use -v for full detail)")
    p_status.add_argument("-v", "--verbose", action="store_true",
                          help="show full config breakdown")

    p_activity = sub.add_parser("activity", help="print recent-decision indicator")
    p_activity.add_argument("--session", metavar="ID", help="session ID (required)")
    p_activity.add_argument("--cwd", metavar="PATH",
                            help="project directory (for inactive indicator)")
    p_activity.add_argument("--width", metavar="N", type=int, default=10,
                            help="number of recent decisions to show (default: 10)")
    p_activity.add_argument("--as", dest="as_format", metavar="FORMAT",
                            choices=["plain", "ansi", "json"], default="plain",
                            help="output format: plain (default), ansi, or json")

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

    p_classify = sub.add_parser("classify", help="internal hook interface")
    p_classify.add_argument("--hook", action="store_true", required=True,
                             help="read hook JSON from stdin, write response to stdout")
    p_classify.add_argument("--format", dest="format", default="json",
                             choices=["json", "plain"],
                             help="output format: json (default) or plain")

    p_review = sub.add_parser("review", help="interactive review of UNSURE decisions")
    p_review.add_argument("--all", action="store_true", help="review all decisions (ALLOW, DENY, UNSURE)")
    p_review.add_argument("--deny", action="store_true", help="review only DENY decisions")

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
        "classify": cmd_classify,
        "review":   cmd_review,
    }

    dispatch[args.cmd_name](args)


if __name__ == "__main__":
    main()
