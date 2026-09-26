#!/usr/bin/env python3
"""The adapted test: a logistic probe on Laya's own encoder embeddings.

Laya's model card says to fine-tune on your own stable labels before
trusting it. This is the cheapest honest version of that: freeze the
encoder, mean-pool it (Laya's own embed_fn_from_agent), and fit a
class-balanced logistic regression on the earliest 50% of rows. Thresholds
are then picked on 50-70% and the result reported on 70-100% -- the same
held-out slice every other model here is scored on.

Usage: probe.py embed DATASET EMB.npy [--subfolder X]   (slow: one encoder
                                                          pass per row)
       probe.py fit   DATASET EMB.npy OUT_PREDICTIONS
"""

import json
import sys

import numpy as np


def haiku_rows(path):
    rows = [json.loads(l) for l in open(path)]
    return sorted(
        (r for r in rows if r["era"] == "haiku"), key=lambda r: r["ts"]
    )


def text_of(r):
    return json.dumps(
        {"tool": r["tool"], "project_dir": r["cwd"], "call": r["input"][:1600]}
    )


def embed(dataset, out, subfolder=None):
    import time
    import laya
    from laya.shortlist import embed_fn_from_agent

    rows = haiku_rows(dataset)
    agent = laya.load(
        "convaiinnovations/laya", device="cpu", subfolder=subfolder
    )
    fn = embed_fn_from_agent(agent, max_length=512, batch_size=16)
    parts, t0 = [], time.time()
    for i in range(0, len(rows), 256):
        parts.append(fn([text_of(r) for r in rows[i : i + 256]]))
        print(f"{i + 256}/{len(rows)} {time.time() - t0:.0f}s", flush=True)
    np.save(out, np.concatenate(parts))


def fit(dataset, emb, out):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    rows = haiku_rows(dataset)
    X = np.load(emb)
    y = np.array([r["label"] == "DENY" for r in rows])
    cut = int(len(rows) * 0.5)
    sc = StandardScaler().fit(X[:cut])
    clf = LogisticRegression(max_iter=5000, class_weight="balanced", C=0.05)
    clf.fit(sc.transform(X[:cut]), y[:cut])
    p = clf.predict_proba(sc.transform(X))[:, 1]
    with open(out, "w") as fh:
        for r, pd in zip(rows, p):
            fh.write(
                json.dumps(
                    {
                        "ts": r["ts"],
                        "label": r["label"],
                        "p_deny": float(pd),
                        "latency_s": None,
                    }
                )
                + "\n"
            )


if __name__ == "__main__":
    if sys.argv[1] == "embed":
        sub = (
            sys.argv[sys.argv.index("--subfolder") + 1]
            if "--subfolder" in sys.argv
            else None
        )
        embed(sys.argv[2], sys.argv[3], sub)
    else:
        fit(sys.argv[2], sys.argv[3], sys.argv[4])
