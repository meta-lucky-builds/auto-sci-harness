"""Deterministic data handling on the host: CSV IO, dev split, data profiling (SciDER-style, PLAN L4.2)."""

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
    header, rows = read_csv(path)
    if header != ["prediction"]:
        raise ValueError(f"predictions header must be ['prediction'], got {header[:5]}")
    return [r[0] for r in rows]


def dev_split(train_csv: Path, target: str, fraction: float, seed: int, out_dir: Path) -> list[str]:
    """Split public train into dev/train.csv + dev/valid_X.csv. Returns the held-back valid labels
    (kept on the host only)."""
    header, rows = read_csv(train_csv)
    if target not in header:
        raise ValueError(f"target column '{target}' not in {train_csv}")
    ti = header.index(target)
    idx = list(range(len(rows)))
    random.Random(seed).shuffle(idx)
    n_valid = max(1, int(len(rows) * fraction))
    valid, train = idx[:n_valid], idx[n_valid:]
    write_csv(out_dir / "train.csv", header, [rows[i] for i in train])
    x_header = [h for h in header if h != target]
    write_csv(out_dir / "valid_X.csv", x_header, [[v for j, v in enumerate(rows[i]) if j != ti] for i in valid])
    return [rows[i][ti] for i in valid]


def _as_float(v: str) -> float | None:
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except ValueError:
        return None


def profile(train_csv: Path, target: str, max_columns: int = 60) -> dict[str, Any]:
    """Compact, LLM-readable profile: shape, per-column type/missing/stats, target distribution,
    simple leakage flags. Deterministic."""
    header, rows = read_csv(train_csv)
    cols: list[dict[str, Any]] = []
    for j, name in enumerate(header[:max_columns]):
        values = [r[j] for r in rows if j < len(r)]
        missing = sum(1 for v in values if v.strip() == "")
        nums = [f for f in (_as_float(v) for v in values) if f is not None]
        info: dict[str, Any] = {"name": name, "missing": missing}
        if len(nums) >= 0.95 * max(1, len(values) - missing):
            info["type"] = "numeric"
            if nums:
                info.update(min=min(nums), max=max(nums), mean=round(statistics.fmean(nums), 4),
                            std=round(statistics.pstdev(nums), 4), n_unique=len(set(nums)))
        else:
            counts = Counter(values)
            info.update(type="categorical", n_unique=len(counts), top=counts.most_common(5))
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
        if c["name"] != target and c.get("n_unique") == len(rows) and c["type"] == "categorical":
            flags.append(f"column '{c['name']}' is unique per row (likely an ID; do not use as a feature)")
        if c["missing"] > 0.3 * len(rows):
            flags.append(f"column '{c['name']}' is {c['missing'] / len(rows):.0%} missing")
    return {"n_rows": len(rows), "n_columns": len(header), "columns": cols,
            "columns_truncated": len(header) > max_columns, "target": target_info, "flags": flags}


def csv_text(header: list[str], rows: list[list[str]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()
