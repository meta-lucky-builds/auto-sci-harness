"""HONEST model with a NONDETERMINISTIC split (random.SystemRandom ignores the seed).

Audit fixture (stdlib only, deterministic unless stated). Contract: --data --out --seed."""

import argparse
import csv
import random


def load():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    with open(a.data, newline="") as f:
        rows = list(csv.DictReader(f))
    return a, rows


def permutation_split(rows, seed, frac=0.3):
    idx = list(range(len(rows)))
    random.Random(seed).shuffle(idx)
    n_test = int(len(rows) * frac)
    return sorted(idx[n_test:]), sorted(idx[:n_test])


def stratified_split(rows, seed, frac=0.3):
    rng = random.Random(seed)
    groups = {}
    for i, r in enumerate(rows):
        groups.setdefault(r["label"], []).append(i)
    train, test = [], []
    for lab in sorted(groups):
        g = groups[lab]
        rng.shuffle(g)
        k = int(len(g) * frac)
        test += g[:k]
        train += g[k:]
    return sorted(train), sorted(test)


def write(path, pairs):
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["row_id", "prediction"])
        for rid, p in pairs:
            w.writerow([rid, p])


def cat_counts(rows, idx, classes):
    counts = {}
    for i in idx:
        counts.setdefault(rows[i]["cat"], {c: 0 for c in classes})[rows[i]["label"]] += 1
    return counts


def te_predict(rows, test, counts, classes, fallback):
    out = []
    for i in test:
        cc = counts.get(rows[i]["cat"])
        out.append((rows[i]["id"], fallback if cc is None else max(classes, key=lambda c: (cc[c], c))))
    return out


def majority(rows, idx, classes):
    return max(classes, key=lambda c: (sum(1 for i in idx if rows[i]["label"] == c), c))


FEATS = ("x1", "x2", "x3")


def nn1(rows, train, test, transform):
    pts = [(transform(rows[j]), rows[j]["label"]) for j in train]
    out = []
    for i in test:
        q = transform(rows[i])
        best = min(range(len(pts)), key=lambda k: (sum((a - b) ** 2 for a, b in zip(q, pts[k][0], strict=True)), k))
        out.append((rows[i]["id"], pts[best][1]))
    return out


def fit_scaler(rows, idx):
    stats = []
    for f in FEATS:
        v = [float(rows[i][f]) for i in idx]
        m = sum(v) / len(v)
        s = (sum((x - m) ** 2 for x in v) / len(v)) ** 0.5 or 1.0
        stats.append((m, s))
    return lambda r: tuple((float(r[f]) - m) / s for f, (m, s) in zip(FEATS, stats, strict=True))


def main():
    a, rows = load()
    classes = sorted({r["label"] for r in rows})
    idx = list(range(len(rows)))
    random.SystemRandom().shuffle(idx)
    n_test = int(len(rows) * 0.3)
    train, test = sorted(idx[n_test:]), sorted(idx[:n_test])
    counts = cat_counts(rows, train, classes)
    write(a.out, te_predict(rows, test, counts, classes, majority(rows, train, classes)))


if __name__ == "__main__":
    main()
