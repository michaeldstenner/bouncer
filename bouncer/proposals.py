"""Approved policy edits, applied by the orchestrator: propose, apply, revert.

Working agents never edit a policy: an agent that hits a wall must not
simply move it. The orchestrator (Hobbes) may edit ANOTHER project's
policy, but only a change the user approved in the moment, and only that
exact change:

    bouncer propose <project> <diff>   store the diff, print a short code
                                       that fingerprints it, and the diff
    (the orchestrator shows the user the diff and the code; the user says
     yes to that change)
    bouncer apply <code>               write exactly that change
    bouncer revert <code>              put back what was there before

Every check is made here, in the code that writes — not in the CLI, not in
the hook — so it holds however this is reached: the command, an import, a
forged hook payload.

WHO MAY APPLY. The process doing the write must descend from a Claude Code
session (a process named `claude`) whose launch directory is a configured
orchestrator project, with no other `claude` above it. A session's launch
directory is its process cwd, which the agent inside it cannot change:
`cd` moves only its shell. A worker that forges a hook payload, imports
this module, or runs `bouncer apply` is still a descendant of its own
session. A nested session (`claude -p` started by an agent inside the
orchestrator's directory) has two `claude` ancestors and is refused. The
orchestrator list is read from the USER config only (`orchestrators:`),
which no agent may write; a project config cannot name itself.

The limit, stated plainly: a session launched fresh in the orchestrator's
directory OUTSIDE any agent's process tree (through tmux, say) looks exactly
like the orchestrator. Approval travels through the orchestrator by design,
as with the airlock at home; `_approval_ok` is where an approval minted by
a channel the orchestrator cannot author (a Polly button) plugs in later.

WHAT MAY BE WRITTEN. Only `<project>/.bouncer/policy.md` of an initialized
project that is not an orchestrator, as a regular file (no symlink). Never
`policy.local.md` (the user's private layer), never any `config*.yaml`,
never the user-scope ~/.config/bouncer/, never profile state, and nothing
of `bouncer review`. One code per approval, applied once, reverted at most
once.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import config as _config

PROPOSALS_DIR = _config.HOME / ".local" / "share" / "bouncer" / "proposals"

_CODE_LENGTH = 8
_PATCH = shutil.which("patch") or "/usr/bin/patch"


class Refused(Exception):
    """A proposal, apply or revert that bouncer will not perform. The
    message is written for the agent that receives it."""


# --- who is calling -----------------------------------------------------------

def _process_row(pid: int) -> tuple[int, str] | None:
    """(parent pid, command name) of a process, or None."""
    try:
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.stdout.strip()
    if not line:
        return None
    ppid, _, comm = line.partition(" ")
    try:
        return int(ppid), comm.strip()
    except ValueError:
        return None


def _process_cwd(pid: int) -> Path | None:
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd",
                              "-Fn"], capture_output=True, text=True,
                             timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        if line.startswith("n"):
            return Path(line[1:])
    return None


def calling_session(pid: int | None = None) -> tuple[Path | None, bool]:
    """(launch directory of the nearest Claude Code session above `pid`,
    whether another session sits above that one). (None, False) when no
    session is found: a person at a terminal, or a process detached from
    the session that started it."""
    pid = os.getpid() if pid is None else pid
    session_pid, nested, hops = None, False, 0
    while pid > 1 and hops < 64:
        row = _process_row(pid)
        if row is None:
            break
        ppid, comm = row
        if Path(comm).name == "claude":
            if session_pid is None:
                session_pid = pid
            else:
                nested = True
                break
        pid, hops = ppid, hops + 1
    if session_pid is None:
        return None, False
    return _process_cwd(session_pid), nested


def orchestrators() -> list[Path]:
    """The orchestrator projects, from the USER config only."""
    raw = _config.load_yaml_config(_config.USER_CONFIG_FILE) or {}
    value = raw.get("orchestrators") or []
    if isinstance(value, str):
        value = [value]
    out = []
    for item in value:
        try:
            out.append(Path(str(item)).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    return out


def check_caller() -> Path:
    """The orchestrator project the caller belongs to, or Refused."""
    launch, nested = calling_session()
    if launch is None:
        raise Refused(
            "Refused: bouncer apply and revert run only from the "
            "orchestrator's own agent session, and this process is not "
            "inside one. A person at the terminal edits the policy "
            "directly instead.")
    if nested:
        raise Refused(
            "Refused: this is a Claude Code session started inside another "
            "one. Only the orchestrator's own top-level session may apply "
            "or revert a policy change.")
    allowed = orchestrators()
    if not allowed:
        raise Refused(
            "Refused: no orchestrator is configured. The user adds "
            "`orchestrators: [<project dir>]` to ~/.config/bouncer/"
            "config.yaml to enable bouncer apply.")
    try:
        launched = launch.resolve()
    except (OSError, RuntimeError):
        launched = launch
    if launched not in allowed:
        raise Refused(
            f"Refused: wrong caller. This session belongs to {launched}, "
            "which is not an orchestrator. A working agent never applies a "
            "policy change, its own or another project's. Report what you "
            "need instead.")
    return launched


def _approval_ok(record: dict, caller: Path) -> bool:
    """Whether this change carries the approval it needs. Today approval
    travels through the orchestrator (its yes-in-chat is the record), so a
    valid code from the orchestrator suffices. A channel the orchestrator
    cannot author — a Polly button writing an approval file into the
    protected store — plugs in here."""
    return True


# --- what may be written ------------------------------------------------------

def resolve_project(arg: str) -> Path:
    path = Path(arg).expanduser()
    candidates = [path]
    if not path.is_absolute() and "/" not in arg:
        candidates += [_config.HOME / "Code" / arg,
                       _config.HOME / "Documents" / "Code" / arg]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    raise Refused(f"Refused: no project directory {arg!r}.")


def _protected_roots() -> list[Path]:
    from . import profile as _profile
    roots = []
    for base in (_config.USER_CONFIG_DIR, Path(_profile.PROFILE_DIR),
                 PROPOSALS_DIR):
        try:
            roots.append(Path(base).resolve())
        except (OSError, RuntimeError):
            roots.append(Path(base))
    return roots


def check_target(target: Path) -> Path:
    """Refused unless `target` is another project's `.bouncer/policy.md`."""
    if target.name != "policy.md" or target.parent.name != ".bouncer":
        raise Refused(
            f"Refused: wrong target {target}. Only a project's "
            ".bouncer/policy.md may be changed this way — never "
            "policy.local.md, never any config*.yaml.")
    if target.is_symlink() or not target.is_file():
        raise Refused(
            f"Refused: wrong target {target}. It must be a regular file, "
            "not a symlink and not missing.")
    resolved = target.resolve()
    bouncer_dir = resolved.parent
    if not (bouncer_dir / "config.yaml").is_file():
        raise Refused(
            f"Refused: wrong target {target}. That project is not "
            "initialized (no .bouncer/config.yaml).")
    for root in _protected_roots():
        if resolved == root or root in resolved.parents:
            raise Refused(f"Refused: wrong target {target}.")
    for orchestrator in orchestrators():
        if bouncer_dir == (orchestrator / ".bouncer").resolve():
            raise Refused(
                f"Refused: wrong target {target}. That is an "
                "orchestrator's own policy, which only the user edits.")
    return resolved


# --- the store ----------------------------------------------------------------

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def code_for(record: dict) -> str:
    """The code that fingerprints one exact change: the target, the text it
    starts from, the text it ends at, and the diff the user was shown."""
    material = json.dumps({key: record[key] for key in
                           ("target", "base_sha", "new_sha", "diff")},
                          sort_keys=True)
    return _sha(material)[:_CODE_LENGTH]


def _record_path(code: str) -> Path:
    if not code.isalnum() or len(code) != _CODE_LENGTH:
        raise Refused(f"Refused: {code!r} is not a proposal code.")
    return PROPOSALS_DIR / f"{code}.json"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load(code: str) -> dict:
    path = _record_path(code)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Refused(f"Refused: no proposal with code {code}.") from None
    except (OSError, json.JSONDecodeError):
        raise Refused(f"Refused: proposal {code} cannot be read.") from None


def _save(record: dict) -> None:
    _write_atomic(_record_path(record["code"]),
                  json.dumps(record, indent=2, sort_keys=True) + "\n")


def _log(action: str, record: dict, caller: Path | None) -> None:
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "action": action, "code": record["code"],
             "target": record["target"], "base_sha": record["base_sha"],
             "new_sha": record["new_sha"],
             "by": str(caller) if caller else None}
    with open(PROPOSALS_DIR / "log.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _verified(code: str) -> dict:
    record = _load(code)
    # The code covers the two hashes; the texts are checked against them,
    # or a record with a valid code could carry any text at all.
    if record.get("code") != code or code_for(record) != code \
            or _sha(record.get("base_text", "")) != record["base_sha"] \
            or _sha(record.get("new_text", "")) != record["new_sha"]:
        raise Refused(
            f"Refused: proposal {code} no longer matches its code — the "
            "stored change was altered after it was proposed. Nothing was "
            "written. Propose it again and get a fresh yes.")
    return record


# --- the three verbs ----------------------------------------------------------

def _patched(base: str, diff: str) -> str:
    """`base` with `diff` applied, by patch(1) on a scratch copy."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "policy.md"
        out = Path(tmp) / "policy.new"
        patch = Path(tmp) / "change.diff"
        src.write_text(base, encoding="utf-8")
        patch.write_text(diff, encoding="utf-8")
        try:
            result = subprocess.run(
                [_PATCH, "-s", "-f", "-o", str(out), str(src), str(patch)],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise Refused(f"Refused: could not run patch: {exc}") from None
        if result.returncode != 0 or not out.exists():
            raise Refused(
                "Refused: the diff does not apply to the current policy. "
                + (result.stdout + result.stderr).strip()[:300])
        return out.read_text(encoding="utf-8")


def propose(project_arg: str, diff: str) -> dict:
    """Store a proposed change and return its record (with its code).
    Anyone may propose; a proposal changes nothing until the orchestrator
    applies it on the user's yes."""
    project = resolve_project(project_arg)
    target = check_target(project / ".bouncer" / "policy.md")
    base = target.read_text(encoding="utf-8")
    new = _patched(base, diff)
    if new == base:
        raise Refused("Refused: the diff changes nothing.")
    launch, _ = calling_session()
    record = {"target": str(target), "project": str(project),
              "diff": diff, "base_sha": _sha(base), "new_sha": _sha(new),
              "base_text": base, "new_text": new,
              "proposed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "proposed_by": str(launch) if launch else None,
              "applied_at": None, "applied_by": None,
              "reverted_at": None, "reverted_by": None}
    record["code"] = code_for(record)
    if _record_path(record["code"]).exists():
        existing = _load(record["code"])
        if existing.get("applied_at"):
            raise Refused(
                f"Refused: this exact change was already applied as "
                f"{record['code']}.")
        return existing
    _save(record)
    return record


def apply(code: str) -> dict:
    caller = check_caller()
    record = _verified(code)
    if record.get("applied_at"):
        raise Refused(
            f"Refused: code {code} was already used, at "
            f"{record['applied_at']}. One code is one approval; a new "
            "change needs a new proposal and a new yes.")
    target = check_target(Path(record["target"]))
    if not _approval_ok(record, caller):
        raise Refused(f"Refused: proposal {code} has no approval.")
    if _sha(target.read_text(encoding="utf-8")) != record["base_sha"]:
        raise Refused(
            f"Refused: {target} changed after proposal {code} was made, "
            "so it is not the change the user approved. Propose it again.")
    _write_atomic(target, record["new_text"])
    record["applied_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    record["applied_by"] = str(caller)
    _save(record)
    _log("apply", record, caller)
    return record


def revert(code: str) -> dict:
    caller = check_caller()
    record = _verified(code)
    if not record.get("applied_at"):
        raise Refused(f"Refused: proposal {code} was never applied.")
    if record.get("reverted_at"):
        raise Refused(
            f"Refused: code {code} was already reverted, at "
            f"{record['reverted_at']}.")
    target = check_target(Path(record["target"]))
    if _sha(target.read_text(encoding="utf-8")) != record["new_sha"]:
        raise Refused(
            f"Refused: {target} changed after {code} was applied, so "
            "putting back the old text would discard that later change. "
            "The user reverts it by hand.")
    _write_atomic(target, record["base_text"])
    record["reverted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    record["reverted_by"] = str(caller)
    _save(record)
    _log("revert", record, caller)
    return record
