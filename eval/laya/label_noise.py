#!/usr/bin/env python3
"""How consistent is Haiku with itself? Groups byte-identical tool calls
(same tool, same cwd, same input) and counts groups that received both an
ALLOW and a DENY. That disagreement is a ceiling on how well ANY model can
agree with these labels -- the label is a sample, not a rule.

Usage: label_noise.py DATASET [--era haiku]
"""

import json
import sys
from collections import defaultdict


def main():
    era = sys.argv[3] if len(sys.argv) > 3 else "haiku"
    groups = defaultdict(list)
    for line in open(sys.argv[1]):
        r = json.loads(line)
        if r["era"] == era:
            groups[(r["tool"], r["cwd"], r["input"])].append(r["label"])
    repeated = {k: v for k, v in groups.items() if len(v) > 1}
    mixed = {k: v for k, v in repeated.items() if "ALLOW" in v and "DENY" in v}
    deny_groups = {k: v for k, v in repeated.items() if "DENY" in v}
    print(f"distinct calls {len(groups)}; seen more than once {len(repeated)}")
    print(
        f"repeated calls with any DENY {len(deny_groups)}; "
        f"of those, also ALLOWed at least once {len(mixed)}"
    )
    rows_in_deny = sum(len(v) for v in deny_groups.values())
    denies = sum(v.count("DENY") for v in deny_groups.values())
    print(
        f"within repeated DENY-bearing calls: {denies}/{rows_in_deny} "
        f"verdicts were DENY"
    )
    for k, v in list(mixed.items())[:5]:
        print(
            "  ", k[0], k[2][:110], "->", dict((x, v.count(x)) for x in set(v))
        )


if __name__ == "__main__":
    main()
