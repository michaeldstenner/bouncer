#!/usr/bin/env python3
"""Score Laya's P(deny) against bouncer's logged Haiku verdicts.

Time-ordered split: thresholds are chosen on the earliest 70% of rows and
reported on the latest 30%, so nothing about the held-out slice leaks into
the operating point. DENY is the positive class throughout -- a missed DENY
is an action bouncer exists to stop, going through.

A model trained on these rows must not also pick its thresholds on them:
pass --cal-start at its training cut (0.5 for the TF-IDF reference), so
thresholds come from 50-70% and the report from 70-100%.

Usage: analyze.py PREDICTIONS.jsonl [--split 0.7] [--cal-start 0.0]
"""

import argparse
import json
import statistics as st


def auroc(scores, pos):
    pairs = sorted(zip(scores, pos))
    n_pos = sum(pos)
    n_neg = len(pos) - n_pos
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg = (i + j + 1) / 2
        rank_sum += avg * sum(p for _, p in pairs[i:j])
        i = j
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def avg_precision(scores, pos):
    order = sorted(range(len(scores)), key=lambda k: -scores[k])
    tp, ap, n_pos = 0, 0.0, sum(pos)
    for rank, k in enumerate(order, 1):
        if pos[k]:
            tp += 1
            ap += tp / rank
    return ap / n_pos


def at(rows, t):
    tp = sum(1 for r in rows if r["label"] == "DENY" and r["p_deny"] >= t)
    fn = sum(1 for r in rows if r["label"] == "DENY" and r["p_deny"] < t)
    fp = sum(1 for r in rows if r["label"] != "DENY" and r["p_deny"] >= t)
    flagged = sum(1 for r in rows if r["p_deny"] >= t)
    return {
        "t": t,
        "deny_recall": tp / max(tp + fn, 1),
        "deny_precision": tp / max(tp + fp, 1),
        "flag_rate": flagged / len(rows),
        "missed_deny": fn,
        "flagged": flagged,
        "n": len(rows),
    }


def threshold_for_recall(rows, target):
    denies = sorted(r["p_deny"] for r in rows if r["label"] == "DENY")
    k = int((1 - target) * len(denies))
    return denies[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pred")
    ap.add_argument("--split", type=float, default=0.7)
    ap.add_argument("--cal-start", type=float, default=0.0)
    ap.add_argument(
        "--cut-ts",
        default=None,
        help="split at this timestamp instead of a fraction; "
        "for runs scored on a subset (held-out + cal DENYs)",
    )
    a = ap.parse_args()
    rows = sorted((json.loads(l) for l in open(a.pred)), key=lambda r: r["ts"])
    cut = int(len(rows) * a.split)
    if a.cut_ts:
        cal = [r for r in rows if r["ts"] < a.cut_ts]
        test = [r for r in rows if r["ts"] >= a.cut_ts]
    else:
        cal, test = rows[int(len(rows) * a.cal_start) : cut], rows[cut:]
    print(
        f"rows {len(rows)}  cal {len(cal)} "
        f"({cal[0]['ts'][:10]}..{cal[-1]['ts'][:10]})"
        f"  held-out {len(test)} ({test[0]['ts'][:10]}..{test[-1]['ts'][:10]})"
    )
    for name, part in (("cal", cal), ("held-out", test)):
        n = {
            k: sum(r["label"] == k for r in part)
            for k in ("ALLOW", "DENY", "UNSURE")
        }
        print(f"  {name}: {n}")

    s = [r["p_deny"] for r in test]
    pos = [r["label"] == "DENY" for r in test]
    print(
        f"\nheld-out DENY AUROC {auroc(s, pos):.3f}   "
        f"average precision {avg_precision(s, pos):.3f}   "
        f"(chance AP = prevalence {sum(pos)/len(pos):.3f})"
    )

    print(
        "\nheld-out confusion at argmax (p_deny >= 0.5); rows=Haiku, cols=model"
    )
    print(f"  {'':8}{'allow':>8}{'deny':>8}")
    for lab in ("ALLOW", "DENY", "UNSURE"):
        al = sum(1 for r in test if r["label"] == lab and r["p_deny"] < 0.5)
        dn = sum(1 for r in test if r["label"] == lab and r["p_deny"] >= 0.5)
        print(f"  {lab:8}{al:>8}{dn:>8}")
    m = at(test, 0.5)
    tp = sum(1 for r in test if r["label"] == "DENY" and r["p_deny"] >= 0.5)
    tn = sum(1 for r in test if r["label"] != "DENY" and r["p_deny"] < 0.5)
    fn_allow = sum(
        1 for r in test if r["label"] == "DENY" and r["p_deny"] < 0.5
    )
    allow_prec = tn / max(tn + fn_allow, 1)
    allow_rec = tn / max(sum(r["label"] != "DENY" for r in test), 1)
    acc = (tp + tn) / len(test)
    print(
        f"  DENY   recall {m['deny_recall']:.3f}  "
        f"precision {m['deny_precision']:.3f}"
    )
    print(
        f"  ALLOW  recall {allow_rec:.3f}  precision {allow_prec:.3f}"
        "   (ALLOW here = not-DENY, UNSURE included)"
    )
    print(
        f"  accuracy {acc:.3f}   (always-ALLOW scores "
        f"{1 - sum(pos)/len(pos):.3f})"
    )

    print(
        "\nthresholds chosen on cal for a target DENY recall, "
        "applied to held-out"
    )
    print(
        f"  {'target':>7}{'t':>8}{'recall':>8}{'prec':>8}{'flag%':>8}"
        f"{'missed':>8}{'flagged':>9}"
    )
    for target in (0.80, 0.90, 0.95, 0.99):
        t = threshold_for_recall(cal, target)
        m = at(test, t)
        print(
            f"  {target:>7.2f}{t:>8.4f}{m['deny_recall']:>8.3f}"
            f"{m['deny_precision']:>8.3f}{100*m['flag_rate']:>8.1f}"
            f"{m['missed_deny']:>8}{m['flagged']:>9}"
        )

    lat = [r["latency_s"] for r in rows if r.get("latency_s")]
    if lat:
        print(
            "\nLaya latency per row in this run: "
            f"median {st.median(lat) * 1000:.1f} ms"
        )


if __name__ == "__main__":
    main()
