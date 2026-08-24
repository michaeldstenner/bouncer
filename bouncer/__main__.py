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
from .commands.escalate import cmd_escalate


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
ESCALATE is a retry, not a shortcut. Submit normally first — most commands are
approved without involving the user. Escalating things bouncer would allow just
spams the user; that's the failure this gate prevents.

If denied, prefer another way: a safe equivalent within policy (read with your
editor, not `cat` in a blocked path), a narrower command, or — if it's routine
— a policy addition (see "Suggesting a policy addition" below). Escalate only
genuine one-offs.

For a shell command, repeat the byte-identical command with a
`# ESCALATE: <reason>` line prepended:

    # ESCALATE: clearing stale build artifacts before release
    rm -rf dist/ build/

If bouncer says your command "doesn't match a command you submitted recently,"
the text after your marker differs from what you ran — resubmit it, then
escalate that exact text.

For any OTHER tool (Read, Write, Edit, WebFetch, MCP tools, ...) there is no
comment to carry the marker, so use the out-of-band signal instead: after the
denial, run `bouncer escalate "<reason>"`, then re-issue the exact same tool
call. bouncer routes that one call to the user. (This also works for shell
commands if you prefer it.) Same rule: it only escalates a call you actually
submitted and got denied — you cannot escalate something pre-emptively.

Current harness behavior:
  * Claude Code — ASK is available.
  * Codex PermissionRequest — ASK is available by abstaining and letting Codex
    show its normal approval prompt.
  * opencode — ASK is available by abstaining from the native permission
    prompt; optional plugin config can delay automatic ALLOW/DENY replies.
  * Codex legacy PreToolUse / shell shim — ASK is not available; outward ASKs
    are delivered as denials or pass-through depending on integration.

── Suggesting a policy addition ──────────────────────────────────────────────
If an operation is routine for this project but keeps getting denied, suggest a
policy addition to the user. Bouncer reads your policy as prose. Describe the
project by location (where it may read/write), action (what it may do), and
association (what the targets belong to — e.g. its mail config, or its launchd
service). Keep it short.

Policy is plain prose fed to an LLM. Bouncer's strength is judgment: describe
intent, scope, and expected effects so the classifier can decide whether a
specific tool call fits the approved work. Do not try to enumerate every
allowed path, command, or flag unless the detail is an example that clarifies
intent.

Remember what the bouncer LLM sees: only bouncer's small system prompt, the
assembled policy, and the current tool call. It is stateless and does not know
the chat history, your plan, files you inspected, or broader project context.
The policy must carry enough project intent for a fresh classifier call.

A useful policy shape is:

    <brief description of the project to inform intent assessment>

    The agent is permitted to:
    - read data from these areas or systems for these purposes
    - affect/modify files in these areas as part of these workflows
    - configure, start, stop, or inspect these services when doing this work

    The agent is not permitted to:
    - affect data, files, services, credentials, or accounts outside that scope
    - perform irreversible, production, or external actions without approval

Good policy names boundaries and intent:
  "This project manages the user's shell environment. The agent may read and
   update shell/editor configuration under the user's dotfile and XDG config
   areas when implementing environment changes, and may run local shell startup
   checks to verify them. It must not modify unrelated application data or
   system-wide configuration."

Poor policy is a brittle command allowlist:
  "Allow rm, allow ~/.zshrc, allow pip install"

When suggesting policy, include concrete examples as examples, not as the whole
rule. Prefer categories such as "may read test fixtures", "may modify generated
build artifacts", "may restart the local development database", or "must not
touch production AWS resources" over long lists of individual commands.

Two common misses:
  * Bless the ordinary. Say the agent may read anywhere and create/edit/move/
    delete within its own tree. Reads are rarely the risk — don't make the
    classifier guess.
  * Avoid broad NEVERs. They're applied by keyword: "never touch bouncer" also
    blocks reading a source file in bouncer/. Name location AND action — "never
    edit ~/.config/bouncer/ or any .bouncer/ config dir" — and a read is not an
    edit.

Reserve caution for effects: deletes outside your tree, system/other-project
changes, force-push, sending data out.

Suggest policy additions without a lot of back-and-forth when the user asks for
policy suggestions, or when a requested operation is clearly within the user's
intent but outside the current policy. Give a short rationale and a paste-ready
markdown block the user can apply.

Do not edit bouncer config or policy files directly. Bouncer rejects agent
attempts to set their own permission scope. The user must make policy changes
themselves, for example by running `bouncer policy` and pasting the suggested
text. `bouncer review` is also human-only; do not invoke it through an agent
tool call.

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

    p_review = sub.add_parser(
        "review", help="cluster logged requests and draft policy improvements")
    p_review.add_argument("--deny", action="store_true",
                          help="review only DENY decisions")
    p_review.add_argument("--all", action="store_true", help=argparse.SUPPRESS)
    p_review.add_argument("--since", metavar="DURATION",
                          help="override cursor with a time window (e.g. 14d, 2h)")
    p_review.add_argument("--all-history", action="store_true",
                          help="ignore the review cursor and analyze the retained log")

    sub.add_parser("abort",
                   help="abort pending LLM classification in this project → ALLOW")

    p_escalate = sub.add_parser("escalate",
                   help="send your last denied tool call to the user (then re-issue it)")
    p_escalate.add_argument("reason", nargs="*",
                            help="why this denied call should be allowed")

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
        "escalate": cmd_escalate,
    }

    dispatch[args.cmd_name](args)


if __name__ == "__main__":
    main()
