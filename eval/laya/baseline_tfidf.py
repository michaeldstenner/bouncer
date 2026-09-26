#!/usr/bin/env python3
"""Reference model: TF-IDF + logistic regression, trained on the earliest
50% of the rows. Thresholds are then picked on 50-70% and the result
reported on the same held-out 70-100% slice Laya is scored on.

Not a proposal. It answers one question: how much of the task is word
spotting? If this matches Laya, a 421M model is not what is doing the work.

Usage: baseline_tfidf.py DATASET OUT_PREDICTIONS [--era haiku] [--train 0.5]
"""

import argparse
import json

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression


def text_of(r):
    return f"{r['tool']} {r['cwd']} {r['input'][:3000]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("out")
    ap.add_argument("--era", default="haiku")
    ap.add_argument("--train", type=float, default=0.5)
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.dataset)]
    rows = sorted(
        (r for r in rows if r["era"] == a.era), key=lambda r: r["ts"]
    )
    cut = int(len(rows) * a.train)
    train = rows[:cut]
    vec = TfidfVectorizer(
        token_pattern=r"[A-Za-z0-9_\-\.~/]+",
        ngram_range=(1, 2),
        min_df=2,
        max_features=200_000,
        sublinear_tf=True,
    )
    X = vec.fit_transform(text_of(r) for r in train)
    y = [r["label"] == "DENY" for r in train]
    clf = LogisticRegression(max_iter=2000, class_weight="balanced", C=4.0)
    clf.fit(X, y)
    p = clf.predict_proba(vec.transform(text_of(r) for r in rows))[:, 1]
    with open(a.out, "w") as fh:
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
    main()
