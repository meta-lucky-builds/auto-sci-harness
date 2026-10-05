"""Generate the `rings` task: nonlinear 3-class classification with noise features and a
categorical column. CPU-scale, stdlib only, deterministic.

    python examples/tasks/make_rings_task.py OUT_DIR [--n-train 1500] [--n-test 1000]
"""

from __future__ import annotations

import argparse
import csv
import math
import random
from pathlib import Path

BASELINE = '''"""Baseline: nearest class centroid on numeric columns (stdlib only)."""
import argparse, csv, math

p = argparse.ArgumentParser()
p.add_argument("--train"); p.add_argument("--test"); p.add_argument("--out"); p.add_argument("--seed", type=int)
a = p.parse_args()

def load(path):
    with open(path, newline="") as f:
        r = list(csv.reader(f))
    return r[0], r[1:]

def numeric_cols(header, rows, skip):
    out = []
    for j, h in enumerate(header):
        if h == skip:
            continue
        try:
            [float(r[j]) for r in rows[:50]]
            out.append(h)
        except ValueError:
            pass
    return out

h, rows = load(a.train)
th, trows = load(a.test)
cols = numeric_cols(h, rows, "label")
yi = h.index("label")
cent, cnt = {}, {}
for r in rows:
    y = r[yi]
    v = [float(r[h.index(c)]) for c in cols]
    cent[y] = [s + x for s, x in zip(cent.get(y, [0.0] * len(v)), v)]
    cnt[y] = cnt.get(y, 0) + 1
cent = {y: [s / cnt[y] for s in v] for y, v in cent.items()}
with open(a.out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["prediction"])
    for r in trows:
        v = [float(r[th.index(c)]) for c in cols]
        w.writerow([min(cent, key=lambda y: sum((p - q) ** 2 for p, q in zip(v, cent[y])))])
'''

TASK_YAML = """id: rings-v1
title: Noisy concentric rings (3-class)
description: >-
  Predict the integer class `label` (0, 1 or 2) of each point. Features: x1, x2 (signal),
  n1..n4 (pure noise), color (categorical, weakly informative). Classes lie on noisy concentric
  rings, so linear and centroid models do poorly.
metric: accuracy
target_column: label
solution_timeout_s: 600
"""


def sample(rng: random.Random) -> list[str]:
    label = rng.randrange(3)
    radius = 1.0 + 1.2 * label + rng.gauss(0, 0.35)
    theta = rng.uniform(0, 2 * math.pi)
    x1, x2 = radius * math.cos(theta), radius * math.sin(theta)
    noise = [rng.gauss(0, 1) for _ in range(4)]
    color = rng.choice(["red", "green", "blue"]) if rng.random() < 0.6 else ["red", "green", "blue"][label]
    return [f"{x1:.5f}", f"{x2:.5f}", *[f"{n:.5f}" for n in noise], color, str(label)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--n-train", type=int, default=1500)
    ap.add_argument("--n-test", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=12345)
    a = ap.parse_args()
    out = Path(a.out_dir)
    (out / "public").mkdir(parents=True, exist_ok=True)
    (out / "private").mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)
    header = ["x1", "x2", "n1", "n2", "n3", "n4", "color", "label"]
    train = [sample(rng) for _ in range(a.n_train)]
    test = [sample(rng) for _ in range(a.n_test)]
    with open(out / "public" / "train.csv", "w", newline="") as f:
        csv.writer(f).writerows([header] + train)
    with open(out / "public" / "test_X.csv", "w", newline="") as f:
        csv.writer(f).writerows([header[:-1]] + [r[:-1] for r in test])
    with open(out / "private" / "test_y.csv", "w", newline="") as f:
        csv.writer(f).writerows([["label"]] + [[r[-1]] for r in test])
    (out / "baseline.py").write_text(BASELINE)
    (out / "task.yaml").write_text(TASK_YAML)
    print(f"wrote task to {out}")


if __name__ == "__main__":
    main()
