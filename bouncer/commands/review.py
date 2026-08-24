import difflib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .. import config as config_mod
from ..colors import RESET, BOLD, GREEN, YELLOW, RED, DIM, CYAN
from ..config import (
    POLICY_LOCAL_SENTINEL,
    load_policy,
    split_policy_tempfile,
)
from ..review import (
    ClusterDisposition,
    cluster_events,
    load_review_events,
    load_reviewed_ids,
    parse_since,
    review_state_file,
    replay_policy_change,
    save_reviewed_ids,
    synthesize_policy,
)


def _policy_sources(
    user: bool,
    bouncer_dir: Path | None,
    merged: dict,
) -> dict:
    sources = {
        "user_policy": load_policy(config_mod.USER_POLICY_FILE),
    }
    if not user and bouncer_dir:
        sources.update({
            "project_policy": load_policy(bouncer_dir / "policy.md"),
            "local_policy": load_policy(bouncer_dir / "policy.local.md"),
            "policy_mode": merged.get("policy_mode", "append"),
        })
    return sources


def _request_text(request: dict) -> str:
    command = request.get("command")
    if isinstance(command, str):
        return command
    return json.dumps(request, ensure_ascii=False, sort_keys=True)


def _review_clusters(clusters, events):
    by_id = {event.event_id: event for event in events}
    reviewed_clusters = []
    dispositions = []
    for index, cluster in enumerate(clusters, 1):
        cluster_events = [by_id[event_id] for event_id in cluster.event_ids]
        counts = Counter(event.decision for event in cluster_events)
        print(f"\n{BOLD}Cluster {index}/{len(clusters)}: {cluster.title}{RESET}")
        print(f"{DIM}{cluster.intent}{RESET}")
        count_text = "  ".join(f"{name}: {count}" for name, count in sorted(counts.items()))
        print(f"{len(cluster_events)} requests  {count_text}")
        print("\nExamples:")
        for event in cluster_events[:3]:
            text = _request_text(event.request).replace("\n", " ")
            if len(text) > 240:
                text = text[:237] + "..."
            location = f"  {DIM}[{event.cwd}]{RESET}" if event.cwd else ""
            print(f"  {event.decision:<8} {text}{location}")
        print(f"\nReviewer assessment: {cluster.rationale}")
        if cluster.suggested_boundary:
            print(f"Suggested boundary: {cluster.suggested_boundary}")
        print(f"Recommendation: {BOLD}{cluster.recommendation.upper()}{RESET}")

        default = {
            "allow": "allow",
            "allow_with_boundary": "allow",
            "deny": "deny",
            "context_dependent": "context_dependent",
            "one_off": "one_off",
        }[cluster.recommendation]
        comment = ""
        while True:
            print("[enter] agree  [a] allow  [d] deny  [o] one-off  "
                  "[c] comment  [s] skip  [q] save and quit: ", end="", flush=True)
            try:
                choice = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                choice = "q"
            if choice in ("", "a", "allow", "d", "deny", "o", "one-off",
                          "one_off", "s", "skip", "q"):
                break
            if choice in ("c", "comment"):
                try:
                    comment = input("Comment: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                continue
            print(f"{YELLOW}Choose enter, a, d, o, c, s, or q.{RESET}")
        if choice == "q":
            break
        disposition = {
            "": default,
            "a": "allow", "allow": "allow",
            "d": "deny", "deny": "deny",
            "o": "one_off", "one-off": "one_off", "one_off": "one_off",
            "s": "skip", "skip": "skip",
        }[choice]
        reviewed_clusters.append(cluster)
        dispositions.append(ClusterDisposition(cluster.cluster_id, disposition, comment))
    return reviewed_clusters, dispositions


def _diff_text(old: str, new: str, old_name: str, new_name: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=old_name,
        tofile=new_name,
    ))


def _run_editor(path: Path) -> bool:
    before = path.stat()
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
    subprocess.run(shlex.split(editor) + [str(path)])
    try:
        after = path.stat()
    except OSError:
        return False
    return (after.st_mtime_ns, after.st_size, after.st_ino) != (
        before.st_mtime_ns, before.st_size, before.st_ino
    )


def _confirm_apply(diff: str) -> bool:
    print(f"\n{BOLD}Final policy diff{RESET}\n")
    print(diff or f"{DIM}(no changes){RESET}")
    if not diff:
        return False
    try:
        return input("Apply this policy change? [y/N]: ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _read_exact(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _policy_bytes(text: str) -> bytes | None:
    normalized = text.strip()
    return (normalized + "\n").encode() if normalized else None


def _stage_file(path: Path, data: bytes, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(data)
        staged = Path(handle.name)
    os.chmod(staged, mode)
    return staged


def _replace_exact(path: Path, data: bytes | None, mode: int | None = None) -> None:
    if data is None:
        path.unlink(missing_ok=True)
        return
    staged = _stage_file(path, data, mode)
    try:
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


@contextmanager
def _policy_locks(paths):
    import fcntl

    handles = []
    try:
        for parent in sorted({path.parent for path in paths}, key=str):
            parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(parent, os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX)
            handles.append(fd)
        yield
    finally:
        for fd in reversed(handles):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _apply_policy_updates(
    expected: dict[Path, bytes | None],
    desired: dict[Path, bytes | None],
) -> bool:
    with _policy_locks(expected):
        if any(_read_exact(path) != content for path, content in expected.items()):
            print(f"{RED}Policy changed during confirmation; refusing to overwrite it.{RESET}")
            return False
        modes = {
            path: (path.stat().st_mode & 0o777) if path.exists() else None
            for path in expected
        }
        staged = {}
        try:
            for path, content in desired.items():
                if content is not None:
                    staged[path] = _stage_file(path, content, modes.get(path))
            for path, content in desired.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    staged[path].replace(path)
            return True
        except OSError as exc:
            rollback_errors = []
            for path, content in expected.items():
                try:
                    _replace_exact(path, content, modes.get(path))
                except OSError as rollback_exc:
                    rollback_errors.append(f"{path}: {rollback_exc}")
            detail = ("; rollback failures: " + "; ".join(rollback_errors)) \
                if rollback_errors else ""
            print(f"{RED}Could not update policy atomically: {exc}{detail}{RESET}")
            return False
        finally:
            for path in staged.values():
                path.unlink(missing_ok=True)


def _write_project_policy(
    bouncer_dir: Path,
    old_committed: bytes | None,
    old_local: bytes | None,
    new_committed: str,
    new_local: str,
) -> bool:
    committed_path = bouncer_dir / "policy.md"
    local_path = bouncer_dir / "policy.local.md"
    if (_read_exact(committed_path), _read_exact(local_path)) != (old_committed, old_local):
        print(f"{RED}Policy changed while review was open; refusing to overwrite it.{RESET}")
        return False
    old_committed_text = old_committed.decode() if old_committed else ""
    old_local_text = old_local.decode() if old_local else ""
    new_committed_bytes = _policy_bytes(new_committed)
    new_local_bytes = _policy_bytes(new_local)
    diff = _diff_text(old_committed_text,
                      new_committed_bytes.decode() if new_committed_bytes else "",
                      str(committed_path), str(committed_path))
    diff += _diff_text(old_local_text,
                       new_local_bytes.decode() if new_local_bytes else "",
                       str(local_path), str(local_path))
    if not _confirm_apply(diff):
        return False
    return _apply_policy_updates(
        {committed_path: old_committed, local_path: old_local},
        {committed_path: new_committed_bytes, local_path: new_local_bytes},
    )


def _edit_project_proposal(bouncer_dir: Path, proposal) -> bool:
    old_committed = _read_exact(bouncer_dir / "policy.md")
    old_local = _read_exact(bouncer_dir / "policy.local.md")
    content = (proposal.project_policy.rstrip() + "\n\n" + POLICY_LOCAL_SENTINEL +
               "\n" + proposal.local_policy.rstrip() + "\n")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="bouncer-review-policy-",
        delete=False, encoding="utf-8",
    ) as handle:
        handle.write(content)
        path = Path(handle.name)
    try:
        if not _run_editor(path):
            print(f"{DIM}Editor buffer was not saved; policy unchanged.{RESET}")
            return False
        new_committed, new_local = split_policy_tempfile(path.read_text(encoding="utf-8"))
        return _write_project_policy(
            bouncer_dir, old_committed, old_local, new_committed, new_local
        )
    finally:
        path.unlink(missing_ok=True)


def _edit_global_proposal(proposal) -> bool:
    path = config_mod.USER_POLICY_FILE
    old = _read_exact(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="bouncer-review-policy-",
        delete=False, encoding="utf-8",
    ) as handle:
        handle.write(proposal.user_policy.rstrip() + "\n")
        draft = Path(handle.name)
    try:
        if not _run_editor(draft):
            print(f"{DIM}Editor buffer was not saved; policy unchanged.{RESET}")
            return False
        new = draft.read_text(encoding="utf-8").strip()
        if _read_exact(path) != old:
            print(f"{RED}Policy changed while review was open; refusing to overwrite it.{RESET}")
            return False
        new_bytes = _policy_bytes(new)
        diff = _diff_text(old.decode() if old else "",
                          new_bytes.decode() if new_bytes else "",
                          str(path), str(path))
        if not _confirm_apply(diff):
            return False
        return _apply_policy_updates({path: old}, {path: new_bytes})
    finally:
        draft.unlink(missing_ok=True)


def _save_report(report_dir: Path, payload: dict) -> Path:
    report_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(report_dir, 0o700)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = report_dir / f"review-{stamp}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


def _confirm_mark_reviewed() -> bool:
    try:
        return input("Mark these requests reviewed without changing policy? [y/N]: ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _show_replay(replay) -> None:
    print(f"\n{BOLD}Counterfactual replay{RESET}")
    for row in replay.rows:
        marker = "canary" if row["canary"] else "history"
        print(f"  {row['current'] or '?':<10} -> {row['proposed'] or '?':<10} "
              f"[{marker}] {row['label']}")


def cmd_review(args):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("bouncer review requires an interactive terminal", file=sys.stderr)
        sys.exit(1)

    if args.user:
        log_file = config_mod.USER_LOG_FILE
        bouncer_dir = None
        project_root = None
        merged = config_mod._deep_merge(
            config_mod.CONFIG_DEFAULTS,
            config_mod.load_yaml_config(config_mod.USER_CONFIG_FILE),
        )
        scope = "global"
    else:
        bouncer_dir = config_mod._find_bouncer_dir()
        if bouncer_dir is None or not (bouncer_dir / "config.yaml").exists():
            print(f"{YELLOW}No project config found.{RESET} Run 'bouncer init' first.")
            sys.exit(1)
        log_file = bouncer_dir / "log.jsonl"
        project_root = bouncer_dir.parent
        merged = config_mod._merged_config(project_root)
        scope = "project"

    if not log_file.exists():
        print(f"{DIM}No log entries to review.{RESET}")
        return

    review_cfg = config_mod.load_review_config()
    llm_cfg = review_cfg.get("llm", {}) if isinstance(review_cfg, dict) else {}
    if not isinstance(llm_cfg, dict) or not llm_cfg.get("model"):
        print(f"{RED}No independent reviewer configured.{RESET} Add review.llm.model "
              "to ~/.config/bouncer/config.yaml.", file=sys.stderr)
        sys.exit(1)

    state_path = review_state_file(config_mod.USER_REVIEW_DIR, project_root)
    reviewed_ids = load_reviewed_ids(state_path)
    try:
        since = parse_since(getattr(args, "since", None))
    except ValueError as exc:
        print(f"{RED}Error: {exc}{RESET}", file=sys.stderr)
        sys.exit(1)
    ignore_cursor = bool(since or getattr(args, "all_history", False))
    decisions = {"DENY"} if getattr(args, "deny", False) else None
    review_input = load_review_events(
        log_file,
        reviewed_ids=set() if ignore_cursor else reviewed_ids,
        since=since,
        decisions=decisions,
    )
    if not review_input.events:
        print(f"{GREEN}No new detailed policy decisions to review.{RESET}")
        if review_input.compact_count:
            print(f"{DIM}{review_input.compact_count} compact records lacked request data.{RESET}")
        return

    provider = llm_cfg.get("provider", "ollama")
    print(f"{BOLD}Bouncer policy review{RESET}")
    print(f"Reviewer: {provider} / {llm_cfg['model']}")
    print(f"Scope: {scope}")
    print(f"Requests: {len(review_input.events)}")
    if review_input.operational:
        print(f"Operational failures excluded: {len(review_input.operational)}")
    if review_input.compact_count:
        print(f"Compact records without request data: {review_input.compact_count}")
    if review_input.malformed_count:
        print(f"Malformed records ignored: {review_input.malformed_count}")
    print(f"\n{CYAN}Clustering requests with the independent reviewer...{RESET}")

    policies = _policy_sources(args.user, bouncer_dir, merged)
    try:
        clusters = cluster_events(review_input.events, policies, review_cfg)
        reviewed_clusters, dispositions = _review_clusters(clusters, review_input.events)
        if not reviewed_clusters:
            print(f"{DIM}No clusters reviewed; cursor unchanged.{RESET}")
            return
        selected_event_ids = {
            event_id for cluster in reviewed_clusters for event_id in cluster.event_ids
        }
        selected_events = [event for event in review_input.events
                           if event.event_id in selected_event_ids]
        print(f"\n{CYAN}Drafting policy changes from reviewed clusters...{RESET}")
        proposal = synthesize_policy(
            reviewed_clusters, dispositions, selected_events,
            policies, scope, review_cfg,
        )
        replay = replay_policy_change(
            reviewed_clusters,
            selected_events,
            policies,
            proposal,
            scope,
            merged,
            project_root or Path.cwd(),
            max_examples=int(review_cfg.get("max_replay_events", 8)),
        )
    except (RuntimeError, ValueError) as exc:
        print(f"{RED}Review failed: {exc}{RESET}", file=sys.stderr)
        sys.exit(1)

    report_path = _save_report(state_path.parent, {
        "version": 1,
        "scope": scope,
        "reviewer": {"provider": provider, "model": llm_cfg["model"]},
        "clusters": [cluster.__dict__ for cluster in reviewed_clusters],
        "dispositions": [item.__dict__ for item in dispositions],
        "proposal": proposal.__dict__,
        "replay": {"rows": replay.rows, "canary_failures": replay.canary_failures},
    })
    print(f"Review report: {report_path}")
    _show_replay(replay)
    if replay.canary_failures:
        print(f"{RED}Proposed policy failed a fixed negative canary; editor not opened.{RESET}")
        if _confirm_mark_reviewed():
            save_reviewed_ids(state_path, reviewed_ids | selected_event_ids)
        return
    applied = (_edit_global_proposal(proposal) if args.user
               else _edit_project_proposal(bouncer_dir, proposal))
    if applied:
        save_reviewed_ids(state_path, reviewed_ids | selected_event_ids)
        print(f"{GREEN}Policy updated; reviewed requests recorded.{RESET}")
    elif _confirm_mark_reviewed():
        save_reviewed_ids(state_path, reviewed_ids | selected_event_ids)
        print(f"{DIM}Policy unchanged; reviewed requests recorded.{RESET}")
    else:
        print(f"{DIM}Policy unchanged; review cursor unchanged.{RESET}")
