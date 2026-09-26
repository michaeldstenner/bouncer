#!/usr/bin/env python3
"""Build a labelled dataset from bouncer's own decision logs.

Reads every per-project .bouncer/log.jsonl plus the central log, READ-ONLY,
and keeps only decisions the LLM made (prompt_chars > 0): deterministic
guard denials, skips, PENDING halves and BREAK markers are not classifier
verdicts and would pollute the labels. Duplicates (the same event logged in
two places, or a copied tree like opencode/opencode.nosync) collapse on
(timestamp, cwd, input_summary).

The label is what the classifier of the day said -- qwen3:32b before
2026-07-25, Haiku after. It is agreement with that classifier, not ground
truth, and every row carries its era so the two can be separated.
"""

import glob
import json
import sys
from collections import Counter
from pathlib import Path

HAIKU_FROM = "2026-07-25"
KEEP = {"ALLOW", "DENY", "UNSURE"}


def main(out_path: str) -> None:
    paths = sorted(
        glob.glob(str(Path.home() / "Documents/Code/*/.bouncer/log.jsonl"))
    )
    paths.append(str(Path.home() / ".local/share/bouncer/log.jsonl"))
    seen, rows, raw = set(), [], Counter()
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                dec = d.get("decision")
                raw[dec] += 1
                if dec not in KEEP or not d.get("prompt_chars"):
                    continue
                key = (
                    d.get("timestamp", "")[:19],
                    d.get("cwd"),
                    d.get("input_summary"),
                )
                if key in seen:
                    continue
                seen.add(key)
                ts = d.get("timestamp", "")
                rows.append(
                    {
                        "ts": ts,
                        "era": "haiku" if ts >= HAIKU_FROM else "qwen",
                        "tool": d.get("tool"),
                        "cwd": d.get("cwd"),
                        "input": d.get("input_summary") or "",
                        "label": dec,
                        "reason": d.get("reason") or "",
                        "elapsed_s": d.get("elapsed_s"),
                    }
                )
    rows.sort(key=lambda r: r["ts"])
    with open(out_path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print("raw decision lines:", dict(raw.most_common()), file=sys.stderr)
    by = Counter((r["era"], r["label"]) for r in rows)
    print("kept (deduped, LLM-decided):", len(rows), file=sys.stderr)
    for k in sorted(by):
        print("  ", k, by[k], file=sys.stderr)
    if rows:
        print(
            "span:",
            rows[0]["ts"][:10],
            "->",
            rows[-1]["ts"][:10],
            file=sys.stderr,
        )


if __name__ == "__main__":
    main(sys.argv[1])
