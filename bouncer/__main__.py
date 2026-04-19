import argparse
import sys

from .commands.init     import cmd_init
from .commands.lint     import cmd_lint
from .commands.config   import cmd_config, cmd_policy
from .commands.status   import cmd_status
from .commands.activity import cmd_activity
from .commands.log      import cmd_log
from .commands.check    import cmd_check
from .commands.classify import cmd_classify
from .commands.review   import cmd_review


def main():
    parser = argparse.ArgumentParser(
        prog="bouncer",
        description="AI agent permission classifier and manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-u", "--user", action="store_true",
        help="operate on user-scope config (~/.config/bouncer/)",
    )

    sub = parser.add_subparsers(dest="cmd_name", metavar="command")

    p_init = sub.add_parser("init", help="create .bouncer template in current project")
    p_init.add_argument(
        "--harness", metavar="NAME",
        help="also wire the AI harness hooks: auto | claude_code | codex | opencode",
    )

    p_lint = sub.add_parser("lint", help="validate config.yaml")
    p_lint.add_argument("file", nargs="?", help="file to lint (default: project config.yaml)")

    sub.add_parser("config", help="open config.yaml in $EDITOR")
    sub.add_parser("policy", help="open policy.md in $EDITOR")

    p_status = sub.add_parser("status", help="show bouncer status (use -v for full detail)")
    p_status.add_argument("-v", "--verbose", action="store_true",
                          help="show full config breakdown")

    p_activity = sub.add_parser("activity", help="print colored recent-decision indicator")
    p_activity.add_argument("--session", metavar="ID", help="session ID (required)")
    p_activity.add_argument("--cwd", metavar="PATH",
                            help="project directory (for inactive indicator)")
    p_activity.add_argument("--width", metavar="N", type=int, default=10,
                            help="number of recent decisions to show (default: 10)")

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

    sub.add_parser("review", help="interactive review of UNSURE decisions")

    args = parser.parse_args()

    if not args.cmd_name:
        parser.print_help()
        sys.exit(0)

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
