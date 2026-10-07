"""Deterministic data handling on the host: CSV IO, cross-validation folds, data profiling (SciDER-style, PLAN L4.2)."""

from __future__ import annotations

import csv
import io
import math
import random
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ValueError(f"{path} is empty")
    return rows[0], rows[1:]


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def read_predictions(path: Path) -> list[str]:
    """Same acceptance rule as held-out runs (kernel.execution.parse_predictions): header `prediction`, exactly one
    value per row. Anything else is a ValueError (the attempt is routed to "fix"), never another exception."""
    header, rows = read_csv(path)
    if header != ["prediction"]:
        raise ValueError(f"predictions header must be ['prediction'], got {header[:5]}")
    bad = next((i for i, r in enumerate(rows, start=2) if len(r) != 1), None)
    if bad is not None:
        raise ValueError(f"predictions file line {bad}: expected exactly one value")
    return [r[0] for r in rows]


_BOM = b"\xef\xbb\xbf"


def csv_style(raw: bytes) -> tuple[str, bool, bool]:
    """(line terminator, ends with a terminator, starts with a UTF-8 BOM) of a CSV file's bytes."""
    return ("\r\n" if b"\r\n" in raw else "\n"), raw.endswith(b"\n"), raw.startswith(_BOM)


def _csv_bytes(header: list[str], rows: list[list[str]], style: tuple[str, bool, bool] = ("\n", True, False)
               ) -> bytes:
    term, final, bom = style
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator=term)
    w.writerow(header)
    w.writerows(rows)
    text = buf.getvalue()
    if not final and text.endswith(term):
        text = text[: -len(term)]
    return (_BOM if bom else b"") + text.encode("utf-8")


def cv_folds(train_csv: Path, target: str, k: int, seed: int, stratify: bool,
             test_csv: Path | None = None) -> list[dict[str, Any]]:
    """K-fold cross-validation over the public training file (the dev estimate, GOAL L4.7).

    Rows are shuffled with `seed`, then dealt round-robin to the k folds (stratified: dealt class by class, so
    every fold's class counts differ by at most one). Each fold is {"train": csv bytes of every OTHER fold's rows
    (with the target), "valid_x": csv bytes of this fold's rows without the target, "labels": this fold's
    target values, "rows": their 0-based indices in the file}; validation rows keep their file order. Every row
    is a validation row in exactly one fold. The labels stay on the host (they are the private dev labels).

    Byte conventions (line terminator, final newline, BOM) mirror the files a held-out run receives: fold training
    files those of `train_csv`, validation files those of `test_csv` (default: `train_csv`), so a program cannot
    tell a dev fold from a held-out run by file format. Row counts still differ (stated limitation)."""
    header, rows = read_csv(train_csv)
    if header:
        header = [header[0].removeprefix("﻿"), *header[1:]]
    train_style = csv_style(Path(train_csv).read_bytes())
    test_style = csv_style(Path(test_csv).read_bytes()) if test_csv is not None else train_style
    if target not in header:
        raise ValueError(f"target column '{target}' not in {train_csv}")
    if not 2 <= k <= len(rows):
        raise ValueError(f"need 2 <= k <= number of rows ({len(rows)}), got k={k}")
    ti = header.index(target)
    idx = list(range(len(rows)))
    random.Random(seed).shuffle(idx)
    if stratify:
        groups: dict[str, list[int]] = {}
        for i in idx:
            groups.setdefault(rows[i][ti], []).append(i)
        idx = [i for label in sorted(groups) for i in groups[label]]
    fold_of = {i: n % k for n, i in enumerate(idx)}
    x_header = [h for j, h in enumerate(header) if j != ti]
    folds = []
    for f in range(k):
        valid = [i for i in range(len(rows)) if fold_of[i] == f]
        folds.append({"train": _csv_bytes(header, [rows[i] for i in range(len(rows)) if fold_of[i] != f],
                                          train_style),
                      "valid_x": _csv_bytes(x_header, [[v for j, v in enumerate(rows[i]) if j != ti] for i in valid],
                                            test_style),
                      "labels": [rows[i][ti] for i in valid], "rows": valid})
    return folds


def _as_float(v: str) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except ValueError:
        return None


def _pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3:
        return 0.0
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0


def _eta_squared(xs: list[float], labels: list[str]) -> float:
    """Correlation ratio: share of a numeric feature's variance explained by the class (0..1)."""
    if len(xs) < 3:
        return 0.0
    m = statistics.fmean(xs)
    total = sum((x - m) ** 2 for x in xs)
    if total == 0:
        return 0.0
    groups: dict[str, list[float]] = {}
    for x, y in zip(xs, labels, strict=True):
        groups.setdefault(y, []).append(x)
    between = sum(len(g) * (statistics.fmean(g) - m) ** 2 for g in groups.values())
    return between / total


def _role(info: dict[str, Any], n_rows: int, nums: list[float]) -> str:
    """Unique-per-row alone does NOT mean an id: continuous measurements are unique per row too. Id-like =
    a categorical value unique per row, or integers forming one consecutive run (0..n-1, 1..n, ...)."""
    u = info.get("n_unique", 0)
    if u <= 1:
        return "constant"
    if u == 2:
        return "binary"
    if u >= 0.98 * n_rows and n_rows > 20:  # tolerant: a duplicated row also duplicates its id
        if info["type"] == "categorical":
            return "id-like"
        if all(v == int(v) for v in nums) and max(nums) - min(nums) == u - 1:
            return "id-like"
    if info["type"] == "numeric" and u <= 10:
        return "coded-categorical"
    return "continuous" if info["type"] == "numeric" else "categorical"


def profile(train_csv: Path, target: str, max_columns: int = 60) -> dict[str, Any]:
    """Compact, LLM-readable profile (GOAL L4.2): shape; per-column type, role, missing, stats, outliers;
    target distribution; feature-target association ranking (|Pearson r| for regression, correlation ratio
    eta^2 for classification); redundant feature pairs; leakage / duplicate / imbalance flags. Deterministic,
    computed by code on the DEV training split only."""
    header, rows = read_csv(train_csv)
    cols: list[dict[str, Any]] = []
    numeric: dict[str, list[float | None]] = {}
    for j, name in enumerate(header[:max_columns]):
        values = [r[j] for r in rows if j < len(r)]
        missing = sum(1 for v in values if v.strip() == "")
        parsed = [_as_float(v) for v in values]
        nums = [f for f in parsed if f is not None]
        info: dict[str, Any] = {"name": name, "missing": missing}
        if len(nums) >= 0.95 * max(1, len(values) - missing):
            info["type"] = "numeric"
            if nums:
                info.update(min=min(nums), max=max(nums), mean=round(statistics.fmean(nums), 4),
                            std=round(statistics.pstdev(nums), 4), n_unique=len(set(nums)))
                if len(nums) >= 8:
                    q = statistics.quantiles(nums, n=4)
                    iqr = q[2] - q[0]
                    sd = statistics.pstdev(nums)
                    info["outliers_iqr"] = sum(1 for v in nums if v < q[0] - 1.5 * iqr or v > q[2] + 1.5 * iqr)
                    info["outliers_z4"] = sum(1 for v in nums if sd > 0 and abs(v - info["mean"]) > 4 * sd)
                    info["skew"] = round(sum((v - statistics.fmean(nums)) ** 3 for v in nums)
                                         / (len(nums) * sd ** 3), 3) if sd > 0 else 0.0
                numeric[name] = parsed
        else:
            counts = Counter(values)
            info.update(type="categorical", n_unique=len(counts), top=counts.most_common(5))
        info["role"] = _role(info, len(rows), nums)
        cols.append(info)
    ti = header.index(target)
    tvals = [r[ti] for r in rows]
    tnums = [f for f in (_as_float(v) for v in tvals) if f is not None]
    is_classification = len(set(tvals)) <= max(20, int(0.05 * len(tvals))) or len(tnums) < len(tvals)
    target_info: dict[str, Any] = {"name": target, "task_type": "classification" if is_classification
                                   else "regression"}
    if is_classification:
        target_info["class_counts"] = Counter(tvals).most_common(20)
    else:
        target_info.update(mean=statistics.fmean(tnums), std=statistics.pstdev(tnums))
    flags = []
    for c in cols:
        if c["name"] != target and c["role"] == "id-like":
            flags.append(f"column '{c['name']}' is unique per row (likely an ID; do not use as a feature)")
        if c["name"] != target and c["role"] == "constant":
            flags.append(f"column '{c['name']}' is constant (no information)")
        if c["missing"] > 0.3 * len(rows):
            flags.append(f"column '{c['name']}' is {c['missing'] / len(rows):.0%} missing")
    # feature-target association (complete rows only), ranked
    assoc = []
    features = [n for n in numeric if n != target]
    for name in features:
        labelled = [(x, t) for x, t in zip(numeric[name], tvals, strict=True) if x is not None]
        xs = [p[0] for p in labelled]
        if is_classification:
            score = _eta_squared(xs, [p[1] for p in labelled])
        else:
            keep: list[tuple[float, float]] = []
            for x, t in labelled:
                y = _as_float(t)
                if y is not None:
                    keep.append((x, y))
            score = abs(_pearson([k[0] for k in keep], [k[1] for k in keep]))
        assoc.append((round(score, 4), name))
        if score > 0.98:
            flags.append(f"column '{name}' almost determines the target (association {score:.3f}): possible leak")
    assoc.sort(reverse=True)
    target_info["association_measure"] = "eta_squared" if is_classification else "abs_pearson"
    target_info["top_features"] = [{"name": n, "association": s} for s, n in assoc[:12]]
    # redundant numeric feature pairs (bounded work: first 40 numeric features)
    redundant = []
    sample = features[:40]
    for i, a in enumerate(sample):
        for b in sample[i + 1:]:
            pairs = [(x, y) for x, y in zip(numeric[a], numeric[b], strict=True) if x is not None and y is not None]
            r = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
            if abs(r) > 0.95:
                redundant.append({"a": a, "b": b, "r": round(r, 4)})
    duplicates = len(rows) - len({tuple(r) for r in rows})
    if duplicates:
        flags.append(f"{duplicates} duplicate rows")
    if is_classification:
        counts = Counter(tvals)
        minority = min(counts.values()) / len(tvals)
        if minority < 0.1:
            flags.append(f"class imbalance: smallest class is {minority:.1%} of rows")
    return {"n_rows": len(rows), "n_columns": len(header), "columns": cols,
            "columns_truncated": len(header) > max_columns, "target": target_info,
            "redundant_pairs": redundant[:10], "flags": flags}


def csv_text(header: list[str], rows: list[list[str]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()
