import glob
import os
import re
import signal
import shlex
import sys
import time
from pathlib import Path

from .config import _merged_config, project_has_bouncer, project_log_file, _find_bouncer_dir, tool_intercepted
from .log import log_decision
from .escalation_cache import record_attempt, was_attempted, strip_escalate_prefix, parse_escalation
from .escalation_grant import record_denial, arm_escalation, take_grant
from .hook import _emit_hook_response, harness_can_ask, resolve_fallback
from .notify import notify_decision
from . import profile as _profile_mod
from .profile import (note_harness, profile_allows_ask,
                      resolve_unattended_action)
from .providers import call_llm
from ._abort import ABORT_EVENT


_BOUNCER_DIAGNOSTIC_COMMANDS = {
    "activity",
    "check",
    "log",
    "profile",
    "status",
}

_BOUNCER_SELF_MANAGEMENT_COMMANDS = {
    "config",
    "init",
    "policy",
    "review",
}


# Refusals that belong to the profile layer, not to policy. Both are returned
# to the agent immediately and create no ASK state, so there is nothing to hang
# in — the failure mode `solo` exists to remove.
_ESCALATION_OFF_REASON = (
    "Escalation is not available in this session, so this request cannot be "
    "sent to a human."
)

_PROFILE_SELF_SET_REASON = (
    "A session may not set its own bouncer profile. The profile decides "
    "whether you may appeal a denial to a human; changing it is the user's "
    "call, not yours. Ask the user to run 'bouncer profile <name>' if it is "
    "wrong."
)


# Pure-output filters a bouncer diagnostic is routinely piped into. Named one
# by one, because this list is the entire scope of the pipeline relaxation in
# `_is_bouncer_diagnostic_command` — every member must be incapable of writing
# anywhere by itself.
_OUTPUT_FILTERS = frozenset({"cat", "head", "less", "more", "tail", "wc"})


def _is_bouncer_diagnostic_command(command: str) -> bool:
    # A single pipeline only: `;`, `&&` and `||` would let an unrelated command
    # ride along on the diagnostic's skip, so those are refused outright.
    segments = _pipeline_segments(command)
    if not segments:
        return False
    # `bouncer --agent-help | head -60` is the same read as `bouncer
    # --agent-help`. Agents pipe it into a pager almost every time, and that
    # trip through the classifier is why bouncer's own advertised remedy was
    # sometimes refused.
    for downstream in segments[1:]:
        argv = _strip_redirections(downstream)
        if not argv or Path(argv[0]).name not in _OUTPUT_FILTERS:
            return False
    parts = _strip_redirections(segments[0])
    if len(parts) > 1 and Path(parts[0]).name == "bouncer" \
            and parts[1] in ("--agent-help", "--help", "-h"):
        return True
    if _segment_bouncer_subcommand(parts) not in _BOUNCER_DIAGNOSTIC_COMMANDS:
        return False
    # `bouncer profile` reads the profile; `bouncer profile <name>` sets it,
    # which is not a diagnostic — see the self-set guard in run_classify.
    return _parse_profile_set_command(command) is None


# Tokens made only of these separate one command from the next. A newline is
# one: `echo hi\nbouncer profile solo` runs two commands, not one `echo`.
_SEPARATOR_CHARS = frozenset(";&|()\n")

# Commands that run a heredoc's body as shell code rather than read it as data.
# Where one of these appears, the body stays in and is read line by line.
_HEREDOC_CODE_READERS = frozenset({
    "bash", "dash", "eval", "fish", "ksh", "mksh", "sh", "source", "ssh",
    "xargs", "zsh",
})


def _lex_tokens(command: str) -> list[str] | None:
    # Backslash-newline is a line continuation to the shell, not a separator.
    command = command.replace("\\\n", "").strip()
    try:
        lexer = shlex.shlex(command, posix=True,
                            punctuation_chars=";&|()<>\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _lex(command: str) -> list[str] | None:
    return _lex_tokens(_without_heredoc_bodies(command))


def _feeds_code_reader(tokens: list[str]) -> bool:
    at_start = True
    for token in tokens:
        name = Path(token).name
        if name in _HEREDOC_CODE_READERS or (at_start and token == "."):
            return True
        at_start = bool(token) and all(char in _SEPARATOR_CHARS
                                       for char in token)
    return False


def _without_heredoc_bodies(command: str) -> str:
    """`command` with its heredoc bodies taken out, so the text of a message
    written through `cat <<EOF` or `tp tell X <<EOF` is not read as commands.

    That is how a dispatch note containing "(bouncer profile --cwd . --as
    json, plus …)" was refused as a profile set named "plus": the `(` split
    the body into a segment of its own.

    A body fed to a shell (`bash <<EOF`, `cat <<EOF | sh`, `source
    /dev/stdin <<EOF`) is code, so it stays in, minus its comment lines.
    Shell comments outside heredocs are dropped too. Anything this scanner
    cannot place with confidence — an unterminated heredoc, an unclosed quote
    in the delimiter, an unmatched `((`, a command substitution outside
    quotes — returns `command` unchanged: reading too much as code only
    over-denies.
    """
    if "<<" not in command and "#" not in command:
        return command
    stripped: list[str] = []
    kept: list[str] = []
    pending: list[tuple[str, bool]] = []
    changed = False
    quote = None
    index, length = 0, len(command)

    def emit(text: str) -> None:
        stripped.append(text)
        kept.append(text)

    while index < length:
        char = command[index]
        if quote:
            if char == "\\" and quote == '"':
                emit(command[index:index + 2])
                index += 2
                continue
            if char == quote:
                quote = None
            emit(char)
            index += 1
            continue
        if char == "\\":
            emit(command[index:index + 2])
            index += 2
            continue
        if char in "'\"":
            quote = char
            emit(char)
            index += 1
            continue
        if char == "`" or (command.startswith("$(", index)
                           and not command.startswith("$((", index)):
            return command
        if char == "#" and (index == 0 or command[index - 1] in " \t\n;&|()"):
            # A comment runs to the end of the line. Dropped so that a `#
            # don't` note does not leave a lone quote for the lexer.
            end = command.find("\n", index)
            index = length if end == -1 else end
            changed = True
            continue
        if command.startswith("((", index):
            # Arithmetic: `$((1<<2))` shifts, it opens no heredoc.
            end = command.find("))", index)
            if end == -1:
                return command
            emit(command[index:end + 2])
            index = end + 2
            continue
        if command.startswith("<<<", index):
            emit("<<<")
            index += 3
            continue
        if command.startswith("<<", index):
            cursor = index + 2
            strip_tabs = command.startswith("-", cursor)
            if strip_tabs:
                cursor += 1
            while cursor < length and command[cursor] in " \t":
                cursor += 1
            delimiter = ""
            while cursor < length and command[cursor] not in " \t\n;&|()<>":
                letter = command[cursor]
                if letter in "'\"":
                    close = command.find(letter, cursor + 1)
                    if close == -1:
                        return command
                    delimiter += command[cursor + 1:close]
                    cursor = close + 1
                elif letter == "\\":
                    delimiter += command[cursor + 1:cursor + 2]
                    cursor += 2
                else:
                    delimiter += letter
                    cursor += 1
            if not delimiter:
                return command
            emit(command[index:cursor])
            pending.append((delimiter, strip_tabs))
            changed = True
            index = cursor
            continue
        emit(char)
        index += 1
        if char == "\n" and pending:
            for delimiter, strip_tabs in pending:
                while True:
                    if index >= length:
                        return command
                    end = command.find("\n", index)
                    end = length if end == -1 else end
                    line = command[index:end]
                    index = end + 1
                    if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                        break
                    if not line.lstrip().startswith("#"):
                        kept.append(line + "\n")
            pending = []
    if pending or quote:
        return command
    if not changed:
        return command
    tokens = _lex_tokens("".join(stripped))
    if tokens is None or _feeds_code_reader(tokens):
        return "".join(kept)
    return "".join(stripped)


def _pipeline_segments(command: str) -> list[list[str]] | None:
    """The segments of `command` when it is a single pipeline, else None.

    Deliberately stricter than `_shell_segments`: `|` is the only separator
    accepted, so `;`, `&&` and `||` — which could smuggle an unrelated command
    alongside a whitelisted one — end the match rather than splitting it.
    """
    parts = _lex(command)
    if parts is None:
        return None
    segments, current = [], []
    for part in parts:
        if part and all(char in _SEPARATOR_CHARS for char in part):
            if part != "|":
                return None
            segments.append(current)
            current = []
        else:
            current.append(part)
    segments.append(current)
    return segments


# Deeper than this, a nested command is refused as unreadable rather than
# followed further.
_MAX_NESTING = 4


def _command_segments(command: str, depth: int = 0) -> list[list[str]] | None:
    """Every segment the shell would run for `command`, including the ones
    inside a nested command line (`bash -c STR`, `eval ARGS`, `bash <<<
    STR`), each followed into all of its segments, not just its first.

    None when any level cannot be lexed or nests past _MAX_NESTING: the
    caller cannot tell what would run, and says so rather than guessing.
    """
    parts = _lex(command)
    if parts is None:
        return None
    segments, current = [], []
    for part in parts:
        if part and all(char in _SEPARATOR_CHARS for char in part):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(part)
    if current:
        segments.append(current)
    expanded = []
    for segment in segments:
        expanded.append(segment)
        nested = _nested_command(segment)
        if nested is None:
            continue
        if depth >= _MAX_NESTING:
            return None
        inner = _command_segments(nested, depth + 1)
        if inner is None:
            return None
        expanded.extend(inner)
    return expanded


def _shell_segments(command: str) -> list[list[str]]:
    return _command_segments(command) or []


# "bouncer" as a word: a command, or a path ending in it — not a directory
# on the way to something else (`bouncer/classify.py`).
_BOUNCER_WORD = re.compile(r"(?<![\w.-])bouncer(?![\w./-])")


def _names_bouncer_unreadably(command: str) -> bool:
    """True when `command` names bouncer but cannot be split into segments, so
    neither guard can say what it would run. Refused, not passed unread: a
    lone quote must not be a way past the guards."""
    return (_command_segments(command) is None
            and _BOUNCER_WORD.search(command) is not None)


_REDIRECT_CHARS = frozenset("<>&")


def _strip_redirections(parts: list[str]) -> list[str]:
    """Drop shell redirection syntax from a lexed segment, leaving the argv the
    command itself would see.

    shlex hands `2>&1` back as ['2', '>&', '1'] and `2>/dev/null` as
    ['2', '>', '/dev/null'], so a redirection tacked onto the end of a command
    otherwise reads as a positional argument. That is how
    `bouncer profile --cwd DIR --as json 2>&1` — a pure read — came to look
    like a profile *set* named "2" and was refused as one.

    Used only for reading a bouncer argv. `_shell_writes_protected_path`
    deliberately reads the raw segments instead, because there the `>` IS the
    thing being matched.
    """
    argv: list[str] = []
    index = 0
    while index < len(parts):
        token = parts[index]
        is_redirect = (token
                       and all(char in _REDIRECT_CHARS for char in token)
                       and any(char in "<>" for char in token))
        if is_redirect:
            # Drop the operator and its target, plus the leading fd number
            # shlex split off the front (`2` of `2>&1`).
            if argv and argv[-1].isdigit():
                argv.pop()
            index += 2
            continue
        argv.append(token)
        index += 1
    return argv


# Shells whose `-c STR` (or `<<< STR`) is a command line of its own.
_NESTING_SHELLS = frozenset({"bash", "dash", "ksh", "mksh", "sh", "zsh"})


def _nested_command(parts: list[str]) -> str | None:
    """The command line a segment hands to a shell to run, if any:
    `bash -c STR` (and `-lc`, `-ec`, …), `eval ARGS…` (joined with spaces,
    as eval does), and `bash <<< STR` / `. /dev/stdin <<< STR`."""
    argv = _strip_redirections(parts)
    index = _command_index(argv)
    if index >= len(argv):
        return None
    name = Path(argv[index]).name or argv[index]
    if name == "eval":
        return " ".join(argv[index + 1:]) or None
    if name in _NESTING_SHELLS:
        for position in range(index + 1, len(argv)):
            flag = argv[position]
            if flag.startswith("-") and not flag.startswith("--") \
                    and "c" in flag[1:]:
                return argv[position + 1] if position + 1 < len(argv) else None
    if name in _NESTING_SHELLS or name in ("source", "."):
        if "<<<" in parts:
            position = parts.index("<<<")
            if position + 1 < len(parts):
                return parts[position + 1]
    return None


def _command_index(parts: list[str]) -> int:
    """Index of the executable in a redirection-free segment, past leading
    `VAR=value` assignments and wrappers (`env`, `sudo`, `nice`, …) with
    their options."""
    index = 0
    while index < len(parts) and "=" in parts[index] and not parts[index].startswith("="):
        index += 1
    while index < len(parts) and Path(parts[index]).name in (
        "command", "exec", "env", "nice", "nohup", "sudo",
    ):
        wrapper = Path(parts[index]).name
        index += 1
        while index < len(parts):
            part = parts[index]
            if wrapper == "env" and "=" in part and not part.startswith("="):
                index += 1
                continue
            if not part.startswith("-"):
                break
            option = part.split("=", 1)[0]
            index += 1
            takes_value = (
                (wrapper == "env" and option in ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"))
                or (wrapper == "sudo" and option in ("-u", "--user", "-g", "--group", "-h", "--host",
                                                         "-p", "--prompt", "-C", "--close-from",
                                                         "-R", "--chroot", "-D", "--chdir"))
                or (wrapper == "nice" and option in ("-n", "--adjustment"))
            )
            if takes_value and "=" not in part and index < len(parts):
                index += 1
    return index


def _segment_bouncer_argv(parts: list[str]) -> list[str] | None:
    """The bouncer argv of a single shell segment, from the subcommand onward,
    with wrappers (`env`, `sudo`, `python -m`, …) peeled off and shell
    redirections removed.

    A nested command line (`bash -c '…'`, `eval …`) is not read here: it is
    one segment of `_command_segments`' output, and its own segments follow
    it there, so each guard sees every one of them."""
    parts = _strip_redirections(parts)
    index = _command_index(parts)
    if index >= len(parts):
        return None
    executable = Path(parts[index]).name
    index += 1
    if executable.startswith("python"):
        try:
            module_index = parts.index("-m", index)
        except ValueError:
            return None
        if module_index + 1 >= len(parts) or parts[module_index + 1] != "bouncer":
            return None
        index = module_index + 2
    elif executable != "bouncer":
        return None
    if index < len(parts) and parts[index] in ("-g", "--global"):
        index += 1
    return parts[index:] if index < len(parts) else None


def _segment_bouncer_subcommand(parts: list[str]) -> str | None:
    argv = _segment_bouncer_argv(parts)
    return argv[0] if argv else None


def _parse_profile_set_command(command: str) -> str | None:
    """If `command` is a `bouncer profile <name>` invocation (the setting form,
    not the read-only bare one), return the name; otherwise None.

    Wrapper-aware for the same reason `_segment_bouncer_argv` is: a guard the
    agent walks past with `env FOO=1 bouncer profile solo` is not a guard."""
    for parts in _shell_segments(command):
        argv = _segment_bouncer_argv(parts)
        if not argv or argv[0] != "profile":
            continue
        skip = False
        for token in argv[1:]:
            if skip:
                skip = False
                continue
            if token.startswith("-"):
                skip = token in ("--cwd", "--as")
                continue
            return token
    return None


def _is_bouncer_self_management_command(command: str) -> bool:
    return any(
        _segment_bouncer_subcommand(segment) in _BOUNCER_SELF_MANAGEMENT_COMMANDS
        for segment in _shell_segments(command)
    )


# --- the protected set -------------------------------------------------------
#
# What no agent may write: every project's config and policy, the `.bouncer/`
# directory itself (removing or moving it disarms the project), the user-wide
# config dir, and the session-profile state (writing it is `bouncer profile
# live` by another route). Deliberately NOT the decision log, which plenty of
# read-only analysis touches.

_PROTECTED_PROJECT_FILES = frozenset({
    "config.yaml", "config.local.yaml", "policy.md", "policy.local.md",
})

# Where scripts may be run by name rather than by path.
HELPER_SCRIPTS_DIR = Path.home() / ".helpers" / "scripts"

# The protected set as it appears in text: inline code, a heredoc fed to an
# interpreter, a script file, a patch. Relative forms count, because a script
# run from a project root writes `.bouncer/policy.md` without a leading path.
_PROTECTED_TEXT = re.compile(
    r"\.bouncer/(?:config(?:\.local)?\.yaml|policy(?:\.local)?\.md)"
    r"|\.config/bouncer(?![\w.-])"
    r"|\.local/share/bouncer/profile(?![\w.-])"
)


def _protected_dirs() -> list[Path]:
    """Directories protected whole. Read at call time so a patched
    PROFILE_DIR (tests) is honoured."""
    dirs = [Path.home() / ".config" / "bouncer", Path(_profile_mod.PROFILE_DIR)]
    out = []
    for d in dirs:
        out.append(d.absolute())
        try:
            out.append(d.resolve())
        except (OSError, RuntimeError):
            pass
    return out


def _is_protected_location(path: Path) -> bool:
    """True when `path` (absolute) is in the protected set."""
    if path.name == ".bouncer":
        return True
    if path.parent.name == ".bouncer" and path.name in _PROTECTED_PROJECT_FILES:
        return True
    for base in _protected_dirs():
        if path == base or base in path.parents:
            return True
    return False


def _is_protected_bouncer_path(value: str, cwd: Path) -> bool:
    """True when `value`, read as a path from `cwd`, lands in the protected
    set — judged on the path as written and on where it really resolves, so a
    symlink is no way around it."""
    try:
        path = Path(value).expanduser()
        lexical = Path(os.path.normpath(
            path if path.is_absolute() else cwd / path))
        resolved = lexical.resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return _is_protected_location(lexical) or _is_protected_location(resolved)


def _mentions_protected_text(text: str,
                             dirs: list[str] | None = None) -> bool:
    if _PROTECTED_TEXT.search(text):
        return True
    if dirs is None:
        dirs = [str(d) for d in _protected_dirs()]
    return any(d in text for d in dirs)


_GLOB_CHARS = frozenset("*?[")


_QUOTED = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def _quoted_words(command: str) -> frozenset[str]:
    """Words that appear quoted in `command`. The lexer has already removed
    the quotes, and a quoted glob (`tp inspect ".*"`) is passed to the
    program literally, not expanded by the shell."""
    return frozenset(a or b for a, b in _QUOTED.findall(command))


def _token_touches_protected(token: str, cwd: Path,
                             literal: frozenset[str] = frozenset()) -> bool:
    """Whether a single argv token names something in the protected set:
    as a path, as the value of `key=value` / `--opt=value`, or as any match
    of a glob the shell would expand (not one in `literal`, the quoted
    words)."""
    candidates = [token]
    if "=" in token and not token.startswith("="):
        candidates.append(token.split("=", 1)[1])
    home = str(Path.home())
    for candidate in candidates:
        if not candidate:
            continue
        candidate = (candidate.replace("${HOME}", home)
                     .replace("$HOME", home))
        if _is_protected_bouncer_path(candidate, cwd):
            return True
        if _GLOB_CHARS & set(candidate) and token not in literal:
            pattern = os.path.expanduser(candidate)
            if not os.path.isabs(pattern):
                pattern = str(cwd / pattern)
            # As the shell globs: `*` does not match a dotfile, `.b*` does.
            for match in glob.glob(pattern)[:200]:
                if _is_protected_bouncer_path(match, cwd):
                    return True
    return False


# --- what a segment does ------------------------------------------------------

def _is_redirect_operator(token: str) -> bool:
    return (bool(token) and all(char in _REDIRECT_CHARS for char in token)
            and ">" in token)


_HARMLESS_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr",
                             "/dev/tty"})


def _output_targets(parts: list[str]) -> list[str]:
    """Files a segment's output redirections write. fd duplication (`2>&1`,
    `>&2`) and the /dev sinks write nothing."""
    targets = []
    for index, token in enumerate(parts):
        if not _is_redirect_operator(token) or index + 1 >= len(parts):
            continue
        target = parts[index + 1]
        if token.endswith("&") and (target.isdigit() or target == "-"):
            continue
        if target in _HARMLESS_SINKS:
            continue
        targets.append(target)
    return targets


# Leading words that are shell syntax, not the command being run.
_SHELL_PREFIX_WORDS = frozenset({
    "!", "do", "elif", "else", "if", "then", "time", "until", "while", "{",
})
# Segments that only bind or close: `for p in a b`, `done`, `fi`.
_SHELL_BINDING_WORDS = frozenset({"case", "for", "function", "select"})
_SHELL_CLOSING_WORDS = frozenset({"done", "esac", "fi", "}"})

# Commands that cannot write a file named on their command line. Membership
# is the claim; anything absent is assumed able to write.
_READERS = frozenset({
    "[", "[[", "basename", "bat", "cat", "cd", "cmp", "column", "cut",
    "diff", "dirname", "du", "echo", "egrep", "false", "fgrep", "file",
    "grep", "head", "hexdump", "jq", "less", "ls", "md5", "md5sum", "more",
    "nl", "od", "printf", "pwd", "read", "readlink", "realpath", "rg",
    "sha1sum", "sha256sum", "shasum", "stat", "strings", "tail", "test",
    "tr", "tree", "true", "type", "wc", "which",
})
_FIND_WRITERS = frozenset({
    "-delete", "-exec", "-execdir", "-fls", "-fprint", "-fprint0",
    "-fprintf", "-ok", "-okdir",
})
# git subcommands that leave working-tree files alone. `add` and `commit`
# write only the index and the object store.
_GIT_READERS = frozenset({
    "add", "blame", "cat-file", "check-ignore", "commit", "describe", "diff",
    "grep", "log", "ls-files", "rev-parse", "shortlog", "show", "status",
})
_BOUNCER_READERS = frozenset({
    "activity", "check", "escalate", "lint", "log", "profile", "status",
    "tools",
})
# sed's own write commands: `w FILE`, and the `w` flag of `s///`.
_SED_WRITE = re.compile(r"(?:^|[;{}\n]\s*|\d\s*|/)[wW]\s+\S")

_INTERPRETERS = frozenset({
    "awk", "bun", "deno", "fish", "gawk", "lua", "mawk", "nawk", "node",
    "nodejs", "osascript", "perl", "php", "pipx", "Rscript", "ruby", "swift",
    "tclsh", "ts-node", "tsx", "uv",
}) | _NESTING_SHELLS


def _is_interpreter(name: str) -> bool:
    return name in _INTERPRETERS or name.startswith("python")


def _segment_argv(parts: list[str]) -> list[str]:
    """The argv of a segment from its executable on: redirections, shell
    syntax words, assignments and wrappers (`env`, `sudo`, …) removed."""
    argv = _strip_redirections(parts)
    while argv and argv[0] in _SHELL_PREFIX_WORDS:
        argv = argv[1:]
    return argv[_command_index(argv):]


def _copy_writes_protected(argv: list[str], cwd: Path) -> bool:
    """For cp / rsync: whether the destination is protected. A protected file
    among the sources is only being read."""
    args, dest = [], None
    index = 1
    while index < len(argv):
        token = argv[index]
        if token in ("-t", "--target-directory") and index + 1 < len(argv):
            dest = argv[index + 1]
            index += 2
            continue
        if token.startswith("--target-directory="):
            dest = token.split("=", 1)[1]
        elif not token.startswith("-"):
            args.append(token)
        index += 1
    if dest is None:
        dest = args[-1] if args else None
    return dest is None or _token_touches_protected(dest, cwd)


def _segment_is_read_only(parts: list[str], cwd: Path) -> bool:
    """Whether a segment provably writes nothing on its command line. A
    segment that hands a command line to a shell (`bash -c`, `eval`) is
    judged by the segments it runs, which `_command_segments` lists too."""
    if _nested_command(parts) is not None:
        return True
    argv = _strip_redirections(parts)
    while argv and argv[0] in _SHELL_PREFIX_WORDS:
        argv = argv[1:]
    if not argv:
        return True
    if argv[0] in _SHELL_BINDING_WORDS:
        return True
    if argv[0] in _SHELL_CLOSING_WORDS and len(argv) == 1:
        return True
    argv = argv[_command_index(argv):]
    if not argv:
        return True
    name = Path(argv[0]).name
    rest = argv[1:]
    if name in _READERS:
        return True
    if name == "sed":
        if any(a.startswith("-i") or a.startswith("--in-place") for a in rest):
            return False
        return not any(_SED_WRITE.search(a) for a in rest
                       if not a.startswith("-"))
    if name == "find":
        return not (_FIND_WRITERS & set(rest))
    if name == "git":
        index = 0
        while index < len(rest) and rest[index].startswith("-"):
            index += 2 if rest[index] in ("-C", "-c") else 1
        return index < len(rest) and rest[index] in _GIT_READERS
    if name in ("cp", "rsync"):
        return not _copy_writes_protected(argv, cwd)
    if name == "bouncer":
        sub = _segment_bouncer_subcommand(parts)
        return (sub in _BOUNCER_READERS
                and _parse_profile_set_command(" ".join(parts)) is None)
    return False


def _scanned_files(parts: list[str],
                   cwd: Path) -> tuple[list[Path], list[Path]]:
    """Files whose CONTENT decides what a segment does, as (scripts,
    patches): the script an interpreter runs, an executable named by path, a
    `helper run` script, a patch being applied, and anything fed to those on
    stdin."""
    argv = _segment_argv(parts)
    if not argv:
        return [], []
    exe = argv[0]
    name = Path(exe).name
    found: list[str] = []
    if "/" in exe:
        found.append(exe)
    if name == "helper" and len(argv) > 2 and argv[1] == "run":
        found.append(str(HELPER_SCRIPTS_DIR / argv[2]))
    runs_input = False
    is_patch = False
    if _is_interpreter(name):
        runs_input = True
        rest = argv[1:]
        if name == "uv" and rest[:1] == ["run"]:
            rest = rest[1:]
        index = 0
        while index < len(rest):
            token = rest[index]
            if token in ("-", "-m", "-c", "-e", "-p", "--eval", "--print"):
                break
            if token in ("-f", "--file") and index + 1 < len(rest):
                found.append(rest[index + 1])
                break
            if token.startswith("-"):
                index += 1
                continue
            found.append(token)
            break
    elif name == "patch" or (name == "git" and len(argv) > 1
                             and argv[1] in ("apply", "am")):
        runs_input = True
        is_patch = True
        found.extend(a for a in argv[2 if name == "git" else 1:]
                     if not a.startswith("-"))
        if "-i" in argv:
            position = argv.index("-i")
            if position + 1 < len(argv):
                found.append(argv[position + 1])
    if runs_input and "<" in parts:
        position = parts.index("<")
        if position + 1 < len(parts):
            found.append(parts[position + 1])
    paths = []
    for item in found:
        path = Path(os.path.expanduser(item))
        path = path if path.is_absolute() else cwd / path
        try:
            if path.is_file():
                paths.append(path)
        except OSError:
            continue
    return ([], paths) if is_patch else (paths, [])


# Code that writes a file. Paired with a protected mention on the SAME line
# of a script, or anywhere in inline code, it is a write to that location.
# Plain `>` is left out: in most languages it is a comparison.
_WRITE_HINT = re.compile(
    r"""open\([^)]*,\s*(?:mode\s*=\s*)?['"][^'"]*[wax+][^'"]*['"]"""
    r"""|\.open\(\s*(?:mode\s*=\s*)?['"][^'"]*[wax+][^'"]*['"]"""
    r"|write_text|write_bytes|writeFile|appendFile|createWriteStream"
    r"|shutil\.(?:copy|move|rmtree)|os\.(?:rename|replace|remove|unlink"
    r"|rmdir|truncate)|\.unlink\(|\.touch\(|rmtree"
    r"|>>?\s*['\"]?\S*bouncer|\btee\b|\bsed\s+-i"
    r"|(?:^|[\s;&|(`'\"])(?:cp|mv|rm|ln|install|dd|truncate|touch|rsync"
    r"|patch)\s"
)


def _code_writes_protected(code: str, *, by_line: bool) -> bool:
    """Whether code text writes the protected set. Inline code (agent-written
    for this one call) is judged whole; a script file, which may be a
    long-standing tool that only READS bouncer config, line by line."""
    dirs = [str(d) for d in _protected_dirs()]
    if not _mentions_protected_text(code, dirs):
        return False
    if not by_line:
        return bool(_WRITE_HINT.search(code))
    # Only lines that can mention the protected set are worth the regexes.
    needles = ["bouncer", *dirs]
    return any(any(n in line for n in needles)
               and _mentions_protected_text(line, dirs)
               and _WRITE_HINT.search(line)
               for line in code.splitlines())


_SCAN_LIMIT = 1 << 20


def _read_text(path: Path) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(_SCAN_LIMIT)
    except OSError:
        return ""


def _inline_code(parts: list[str], command: str) -> str | None:
    """Code an interpreter segment runs from its own command line: a `-c` /
    `-e` argument, or — when it reads its program from stdin — the whole
    command, heredoc bodies included (they are stripped from the segments)."""
    argv = _segment_argv(parts)
    if not argv or not _is_interpreter(Path(argv[0]).name) \
            or _nested_command(parts) is not None:
        return None
    rest = argv[1:]
    for index, token in enumerate(rest):
        if token in ("-c", "-e", "--eval", "-p", "--print") \
                and index + 1 < len(rest):
            return rest[index + 1]
    positional = [t for t in rest if not t.startswith("-") or t == "-"]
    if not positional or positional[0] == "-":
        return command
    return None


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _tainted_names(segments: list[list[str]], cwd: Path) -> set[str]:
    """Shell variables bound to something in the protected set:
    `P=…/.bouncer/policy.md` and `for f in …/.bouncer/*`."""
    names = set()
    for parts in segments:
        argv = _strip_redirections(parts)
        for token in argv[:_command_index(argv)] if argv else []:
            name, _, value = token.partition("=")
            if _IDENT.match(name) and _token_touches_protected(value, cwd):
                names.add(name)
        if len(argv) > 3 and argv[0] in ("for", "select") \
                and _IDENT.match(argv[1]) and argv[2] == "in" \
                and any(_token_touches_protected(t, cwd) for t in argv[3:]):
            names.add(argv[1])
    return names


def _uses_name(token: str, names: set[str]) -> bool:
    return any(f"${n}" in token or f"${{{n}" in token for n in names)


def _next_cwd(parts: list[str], cwd: Path) -> Path:
    """Best-effort `cd` tracking, so `cd DIR && echo x > policy.md` is
    judged in DIR. Anything it cannot follow leaves the cwd alone."""
    argv = _segment_argv(parts)
    if len(argv) == 2 and argv[0] in ("cd", "pushd") \
            and not argv[1].startswith("-") and "$" not in argv[1]:
        target = Path(os.path.expanduser(argv[1]))
        return Path(os.path.normpath(target if target.is_absolute()
                                     else cwd / target))
    return cwd


def _shell_writes_protected_path(command: str, cwd: Path) -> bool:
    """Whether a shell command could write the protected set.

    Judged by effect, segment by segment, not by the name of the command.
    A segment is refused when:

    - it redirects output into a protected location, or into an unexpanded
      variable while the command has reached a protected location at all;
    - it runs code — inline, a heredoc, a script file, a patch — whose text
      writes a protected location;
    - an argument names a protected location (directly, via a glob, a
      symlink, a `cd` earlier in the line, or a variable bound to one) and
      the command is not a known reader.

    A segment that only reads is never refused for what other segments do:
    `cat policy.md; docket list` is two commands, one of them a read.
    """
    segments = _command_segments(command)
    if segments is None:
        return _mentions_protected_text(command)

    tainted = _tainted_names(segments, cwd)
    literal = _quoted_words(command)
    here = cwd
    reached = bool(tainted)
    judged = []
    for parts in segments:
        touches = False
        for token in parts:
            if _is_redirect_operator(token) or token in ("<", "<<", "<<<"):
                continue
            if _token_touches_protected(token, here, literal) \
                    or (tainted and _uses_name(token, tainted)):
                touches = True
        reached = reached or touches
        judged.append((parts, here, touches))
        here = _next_cwd(parts, here)

    for parts, here, touches in judged:
        for target in _output_targets(parts):
            if _token_touches_protected(target, here):
                return True
            if reached and ("$" in target or "`" in target):
                return True
        code = _inline_code(parts, command)
        if code is not None and _code_writes_protected(code, by_line=False):
            return True
        scripts, patches = _scanned_files(parts, here)
        if any(_code_writes_protected(_read_text(f), by_line=True)
               for f in scripts):
            return True
        if any(_mentions_protected_text(_read_text(f)) for f in patches):
            return True
        if touches and not _segment_is_read_only(parts, here):
            return True
    return False


_SHELL_WRITE_REASON = (
    "Bouncer policy and configuration files cannot be modified by an agent "
    "shell command. Reading them is fine: use cat, sed -n, grep, diff or "
    "your file-reading tool. A program that names them is refused because "
    "bouncer cannot see what it writes.")

# A tool whose name has one of these in it may write the paths it is given.
_MUTATING_WORDS = (
    "append", "copy", "create", "delete", "edit", "mkdir", "move", "put",
    "remove", "rename", "replace", "save", "truncate", "upload", "write",
)


# A tool argument is a path when its key says so. Content (`old_string`,
# `new_string`, `content`) is not: an edit to ~/.config/git/ignore that
# mentions `.bouncer/policy.local.md` writes the ignore file, not the policy.
_PATH_KEY_WORDS = ("dest", "dir", "file", "path", "source", "src", "target")


def _string_values(tool_input: dict) -> list[str]:
    """Every path-like string a tool was handed at the top level, including
    inside a list, under any key that names a path (`file_path`,
    `notebook_path`, `destination`, `target_dir`, …)."""
    values = []
    for key, value in tool_input.items():
        if not any(word in str(key).lower() for word in _PATH_KEY_WORDS):
            continue
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(v for v in value if isinstance(v, str))
    return values


def _management_denial_reason(
    tool_name: str,
    tool_input: dict,
    cwd: Path,
) -> str | None:
    if _is_bouncer_self_management_command(tool_input.get("command", "")):
        return ("Bouncer policy and configuration management must be run directly "
                "by the user, outside an agent tool call")
    if _names_bouncer_unreadably(tool_input.get("command", "")):
        return ("This command names bouncer, but bouncer cannot parse it (an "
                "unbalanced quote, or nesting too deep), so it cannot tell "
                "whether it changes bouncer's own settings. Fix the quoting "
                "and re-issue.")
    if _shell_writes_protected_path(tool_input.get("command", ""), cwd):
        return _SHELL_WRITE_REASON
    mutating_tool = any(word in tool_name.lower() for word in _MUTATING_WORDS)
    if mutating_tool:
        for value in _string_values(tool_input):
            if _is_protected_bouncer_path(value, cwd):
                return ("Bouncer policy and configuration files cannot be modified "
                        "by an agent tool call")
    if "patch" in tool_name.lower():
        patch_text = tool_input.get("patchText") or tool_input.get("patch_text") or ""
        for line in str(patch_text).splitlines():
            prefixes = ("*** Add File: ", "*** Update File: ", "*** Delete File: ",
                        "*** Move to: ")
            for prefix in prefixes:
                if line.startswith(prefix) and _is_protected_bouncer_path(
                    line[len(prefix):].strip(), cwd
                ):
                    return ("Bouncer policy and configuration files cannot be modified "
                            "by an agent patch tool call")
    return None


def _parse_escalate_command(command: str) -> str | None:
    """If `command` is a `bouncer escalate [reason]` invocation, return the
    reason (possibly ""); otherwise None. This is the out-of-band escalation
    signal that works for any tool, not just Bash."""
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        return None
    if not parts or Path(parts[0]).name != "bouncer":
        return None
    idx = 1
    if idx < len(parts) and parts[idx] in ("-g", "--global"):
        idx += 1
    if idx < len(parts) and parts[idx] == "escalate":
        return " ".join(parts[idx + 1:])
    return None


def _skip_reason(tool_name: str, tool_input: dict, config: dict) -> str | None:
    """
    Why bouncer should skip this call (no LLM, no decision), or None to classify.

    Single source of truth for the config-derived skip conditions, shared by
    get_classification (pure path) and run_classify (so it can bail before
    logging a PENDING entry). The cwd-based project_has_bouncer gate is checked
    by callers before the config is loaded.
    """
    if not config.get("enabled", True):
        return "bouncer disabled in config"
    if not tool_intercepted(tool_name, config):
        return f"tool {tool_name!r} not intercepted (tools/groups config)"
    if _is_bouncer_diagnostic_command(tool_input.get("command", "")):
        return "bouncer diagnostic command"
    return None


def _fallback_action(config: dict, key: str, harness: str,
                     permission_mode: str | None = None) -> str:
    """The configured `on_unsure` / `on_unavailable` action, resolved through
    the profile.

    Under a profile with no ASK channel (`solo`), every path that could produce
    an ASK resolves to something else: the harness's own floor where one is
    known to decide without a human, and `deny` where none is. "Known" is
    narrow on purpose — for Claude Code it means the `auto` permission mode
    and nothing else, so an abstain can never turn into a prompt nobody
    answers. Under `live` the configured action is used as written."""
    action = config.get(key, "ask")
    if profile_allows_ask(config):
        return action
    return resolve_unattended_action(action, harness, permission_mode)


def get_classification(
    tool_name: str,
    tool_input: dict,
    cwd: str,
    harness: str = "unknown",
    permission_mode: str | None = None,
) -> tuple[str, str, str | None, int | None, list[dict] | None]:
    """
    Pure logic: get decision and reason for a tool call.
    Returns (decision, reason, action_to_take, prompt_chars, queue_snapshot).

    decision: ALLOW | DENY | UNSURE | ESCALATE | SKIP
    reason:   text explanation
    action_to_take: ALLOW | DENY | ASK | None (if skipped/disabled)
    prompt_chars: combined system+user prompt length, or None if LLM not called
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()

    # The hard guard first, armed tree or not: "agents never edit bouncer
    # config" holds everywhere, not only where someone ran `bouncer init`.
    management_denial = _management_denial_reason(tool_name, tool_input, cwd_path)
    if management_denial:
        return "DENY", management_denial, "DENY", 0, None

    config = _merged_config(cwd_path)
    if not project_has_bouncer(cwd_path):
        return _classify_unarmed(tool_name, tool_input, cwd_path, config)

    skip = _skip_reason(tool_name, tool_input, config)
    if skip:
        return "SKIP", skip, None, None, None

    command = tool_input.get("command", "")

    escalation = parse_escalation(command)
    if escalation is not None:
        escalate_reason, _underlying = escalation
        if not profile_allows_ask(config):
            return "DENY", _ESCALATION_OFF_REASON, "DENY", None, None
        return "ESCALATE", escalate_reason, "ASK", None, None

    try:
        decision, reason, prompt_chars, snap = call_llm(tool_name, tool_input, cwd_path, config)
    except Exception as exc:
        decision, reason, prompt_chars, snap = None, str(exc), None, None

    if decision is None or decision in ("TIMEOUT", "LLM_ERROR"):
        display_dec = decision or "UNSURE"
        fallback_action = _fallback_action(config, "on_unavailable", harness,
                                           permission_mode)
        final_dec, final_reason = resolve_fallback(
            fallback_action,
            f"LLM unavailable: {reason}"
        )
        return display_dec, final_reason, final_dec, prompt_chars, snap

    if decision in ("ALLOW", "DENY"):
        return decision, reason, decision, prompt_chars, None

    # UNSURE
    fallback_action = _fallback_action(config, "on_unsure", harness,
                                       permission_mode)
    final_dec, final_reason = resolve_fallback(
        fallback_action,
        f"LLM unsure: {reason}"
    )
    return "UNSURE", final_reason, final_dec, prompt_chars, None


_UNARMED_NOTE = ("(There is no bouncer project here, so this came from "
                 "the user-wide policy.)")


def _unarmed_verdict(tool_name: str, tool_input: dict, cwd: Path,
                     config: dict):
    """The LLM's verdict for a call in an unarmed tree, or None when there is
    nothing to ask it. An `# ESCALATE:` marker is stripped first: with no
    project there is no escalation, and the marker must not talk the
    classifier out of a denial."""
    if config.get("unarmed", "deny") != "deny":
        return None
    if _skip_reason(tool_name, tool_input, config):
        return None
    command = tool_input.get("command", "")
    if _parse_escalate_command(command) is not None:
        return None
    if parse_escalation(command) is not None:
        tool_input = dict(tool_input, command=strip_escalate_prefix(command))
    try:
        return call_llm(tool_name, tool_input, cwd, config)
    except Exception:
        return None


def _classify_unarmed(tool_name, tool_input, cwd: Path, config: dict):
    """get_classification for an unarmed tree: DENY or ABSTAIN, never ALLOW
    and never ASK."""
    verdict = _unarmed_verdict(tool_name, tool_input, cwd, config)
    if verdict is None:
        return "SKIP", "no project config", None, None, None
    decision, reason, prompt_chars, snap = verdict
    if decision == "DENY":
        return "DENY", f"{reason} {_UNARMED_NOTE}", "DENY", prompt_chars, None
    return decision or "UNSURE", reason, "ABSTAIN", prompt_chars, snap


def _run_unarmed(tool_name: str, tool_input: dict, cwd: str, config: dict,
                 fmt: str) -> None:
    """The hook in a tree with no .bouncer/config.yaml, after the guards.

    The user-wide policy may add a DENY there and nothing else. An ALLOW does
    not auto-approve what the harness would have prompted for, and UNSURE or
    an unreachable LLM never becomes an ASK — nobody configured this tree,
    so nobody is known to be watching it. Everything that is not a DENY
    emits no decision, which is exactly what this tree got before.

    Only denials are logged, to the user log: there is no project log."""
    started = time.time()
    verdict = _unarmed_verdict(tool_name, tool_input, Path(cwd), config)
    if verdict is None or verdict[0] != "DENY":
        sys.exit(0)
    _, reason, prompt_chars, _ = verdict
    reason = f"{reason} {_UNARMED_NOTE}"
    log_decision(tool_name, tool_input, cwd, "DENY", reason, config, None,
                 elapsed_s=round(time.time() - started, 2),
                 prompt_chars=prompt_chars)
    _emit_hook_response("DENY", reason, fmt, False)


def run_classify(
    tool_name: str,
    tool_input: dict,
    cwd: str,
    session_id: str,
    fmt: str = "json",
    harness: str = "unknown",
    permission_mode: str | None = None,
) -> None:
    """
    Legacy/CLI entry point: classifies, logs, updates activity,
    and EXITS the process.
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()

    # Armed or not, the guards below run. Only what follows them differs: an
    # unarmed tree (no .bouncer/config.yaml anywhere up to $HOME) has no
    # project log, no profile state and no escalation, and gets the user-wide
    # policy as a deny-only check — see _run_unarmed.
    armed          = project_has_bouncer(cwd_path)
    config         = _merged_config(cwd_path)
    proj_log       = project_log_file(cwd_path) if armed else None
    bouncer_dir    = _find_bouncer_dir(cwd_path) if armed else None
    rid            = os.getpid()
    command        = tool_input.get("command", "")

    # Effective ASK capability = profile ∧ harness. Both halves are needed:
    # the profile says whether anyone is on the line, the format says whether
    # this harness has a channel to reach them at all. It feeds the DENY
    # wording below and, via note_harness, the indicator. An unarmed tree has
    # nowhere to record a profile and never asks.
    profile_ask = armed and profile_allows_ask(config)
    if armed:
        note_harness(bouncer_dir, harness, harness_can_ask(fmt),
                     permission_mode)

    # Footgun cover, not a security boundary: an agent that notices `bouncer
    # profile` exists must not helpfully flip itself to `live`. The CLI
    # resolves its target project from its own cwd — the same cwd this hook
    # was handed — so any set seen here is a session setting its own profile.
    # Kept separate from _management_denial_reason below: the sets are
    # disjoint, and this one earns its own wording.
    profile_target = _parse_profile_set_command(command)
    if profile_target is not None:
        log_decision(tool_name, tool_input, cwd, "DENY",
                     _PROFILE_SELF_SET_REASON, config, proj_log)
        _emit_hook_response("DENY", _PROFILE_SELF_SET_REASON, fmt, profile_ask)
        return

    management_denial = _management_denial_reason(tool_name, tool_input, cwd_path)
    if management_denial:
        log_decision(tool_name, tool_input, cwd, "DENY", management_denial,
                     config, proj_log)
        notify_decision(
            cfg=config,
            tool_name=tool_name,
            tool_input=tool_input,
            cwd=cwd,
            session_id=session_id,
            decision="DENY",
            action="DENY",
            reason=management_denial,
            request_id=None,
            proj_log=proj_log,
        )
        _emit_hook_response("DENY", management_denial, fmt, profile_ask)
        return

    if not armed:
        _run_unarmed(tool_name, tool_input, cwd, config, fmt)
        return

    # `bouncer escalate [reason]` is the out-of-band escalation signal that
    # works for any tool: it arms a one-shot grant for this project's most
    # recent denial, then the agent re-issues the denied call. Intercept it
    # here (before skip/LLM) so the arming runs with the hook payload's
    # session_id + cwd. Allowing the command lets the CLI print a confirmation.
    # Under a profile with no ASK channel there is nobody to arm it for, so
    # refuse before any grant state exists — an agent trying from habit or a
    # stale brief gets an immediate answer instead of a wait.
    escalate_reason = _parse_escalate_command(command)
    if escalate_reason is not None:
        if not profile_ask:
            log_decision(tool_name, tool_input, cwd, "DENY",
                         _ESCALATION_OFF_REASON, config, proj_log)
            _emit_hook_response("DENY", _ESCALATION_OFF_REASON, fmt,
                                profile_ask)
            return
        if bouncer_dir is not None:
            arm_escalation(bouncer_dir, escalate_reason)
        _emit_hook_response("ALLOW", "bouncer: escalation armed", fmt,
                            profile_ask)
        return

    # Bail on the config-derived SKIP conditions (disabled, tool not in the
    # intercept list, bouncer's own diagnostic commands) BEFORE logging a
    # PENDING entry — otherwise a non-intercepted call strands a PENDING with
    # no resolving entry. Shares _skip_reason with get_classification.
    if _skip_reason(tool_name, tool_input, config):
        sys.exit(0)

    # ESCALATE bypasses the LLM, so we log a single entry (no PENDING).
    if parse_escalation(command) is not None:
        # The profile gate sits ahead of this whole branch, because
        # parse_escalation short-circuits past the LLM straight to
        # _emit_hook_response — a check further down would never run. The
        # agent gets its DENY back immediately and no ASK state is created.
        if not profile_ask:
            log_decision(tool_name, tool_input, cwd, "DENY",
                         _ESCALATION_OFF_REASON, config, proj_log)
            notify_decision(
                cfg=config,
                tool_name=tool_name,
                tool_input=tool_input,
                cwd=cwd,
                session_id=session_id,
                decision="DENY",
                action="DENY",
                reason=_ESCALATION_OFF_REASON,
                request_id=None,
                proj_log=proj_log,
            )
            _emit_hook_response("DENY", _ESCALATION_OFF_REASON, fmt,
                                profile_ask)
            return

        decision, reason, action, _, _snap = get_classification(
            tool_name, tool_input, cwd, harness, permission_mode)
        if decision == "SKIP":
            sys.exit(0)

        # Gate: only honor the escalation if the underlying command was
        # actually attempted (bare, without the prefix) recently this session.
        # Curbs agents that pre-emptively escalate commands they never tried.
        if config.get("escalation_requires_attempt", True):
            underlying = strip_escalate_prefix(command)
            ttl = config.get("escalation_attempt_ttl", 300)
            if not was_attempted(underlying, session_id, ttl):
                reject = (
                    "The command under your `# ESCALATE:` marker doesn't match "
                    "a command you submitted recently. Resubmit it byte-for-byte "
                    "(whitespace aside), changing only the marker, then escalate "
                    "that exact text. Escalation is gated to a command bouncer "
                    "has already seen this session."
                )
                log_decision(tool_name, tool_input, cwd, "DENY", reject,
                             config, proj_log)
                notify_decision(
                    cfg=config,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    cwd=cwd,
                    session_id=session_id,
                    decision="DENY",
                    action="DENY",
                    reason=reject,
                    request_id=None,
                    proj_log=proj_log,
                )
                _emit_hook_response("DENY", reject, fmt, profile_ask)
                return

        log_decision(tool_name, tool_input, cwd, "ESCALATE", reason,
                     config, proj_log)
        notify_decision(
            cfg=config,
            tool_name=tool_name,
            tool_input=tool_input,
            cwd=cwd,
            session_id=session_id,
            decision="ESCALATE",
            action=action,
            reason=reason,
            request_id=None,
            proj_log=proj_log,
        )
        _emit_hook_response(action, f"agent escalation requested: {reason}",
                            fmt, profile_ask)
        return

    # Cross-tool escalation: if the agent armed a grant for this exact call (via
    # `bouncer escalate`) after it was denied, honor it as an ESCALATE without
    # consulting the LLM. One-shot — take_grant consumes it.
    # Cross-tool grants are only consumed while the profile has an ASK
    # channel. Under `solo` the grant is left untouched to expire, and the
    # re-issued call goes to the LLM like any other — which is what a call
    # with no appeal available should do.
    grant_reason = (take_grant(bouncer_dir, tool_name, tool_input)
                    if bouncer_dir is not None and profile_ask else None)
    if grant_reason is not None:
        log_decision(tool_name, tool_input, cwd, "ESCALATE", grant_reason,
                     config, proj_log)
        notify_decision(
            cfg=config,
            tool_name=tool_name,
            tool_input=tool_input,
            cwd=cwd,
            session_id=session_id,
            decision="ESCALATE",
            action="ASK",
            reason=grant_reason,
            request_id=None,
            proj_log=proj_log,
        )
        _emit_hook_response(
            "ASK", f"agent escalation requested: {grant_reason}",
            fmt, profile_ask)
        return

    signal.signal(signal.SIGUSR1, lambda sig, frame: ABORT_EVENT.set())

    log_decision(tool_name, tool_input, cwd, "PENDING", "calling LLM",
                 None, proj_log, rid)

    t0 = time.monotonic()
    decision, reason, action, prompt_chars, snap = get_classification(
        tool_name, tool_input, cwd, harness, permission_mode)
    elapsed = time.monotonic() - t0

    if decision == "SKIP":
        sys.exit(0)

    if ABORT_EVENT.is_set():
        decision, reason, action = "ALLOW", "user aborted — allowing", "ALLOW"

    log_decision(tool_name, tool_input, cwd, decision, reason,
                 config, proj_log, rid,
                 elapsed_s=elapsed, prompt_chars=prompt_chars,
                 queue_snapshot=snap)
    # Record this bare attempt so a later `# ESCALATE:` of the same command is
    # recognized as a genuine retry rather than a pre-emptive escalation.
    if config.get("escalation_requires_attempt", True):
        record_attempt(command, session_id)
    # Remember a denial so a later `bouncer escalate` can route this exact call
    # to the user — the cross-tool equivalent of the bash attempt gate.
    if decision == "DENY" and bouncer_dir is not None:
        record_denial(bouncer_dir, tool_name, tool_input, reason)
    notify_decision(
        cfg=config,
        tool_name=tool_name,
        tool_input=tool_input,
        cwd=cwd,
        session_id=session_id,
        decision=decision,
        action=action,
        reason=reason,
        request_id=rid,
        elapsed_s=elapsed,
        prompt_chars=prompt_chars,
        proj_log=proj_log,
    )

    if action:
        _emit_hook_response(action, reason, fmt, profile_ask)
    else:
        sys.exit(0)
