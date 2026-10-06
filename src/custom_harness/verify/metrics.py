"""Trusted metric implementations (run on the host, never by agent code). Stdlib only."""

from __future__ import annotations

import math
from collections import Counter

HIGHER_IS_BETTER = {"accuracy": True, "f1_macro": True, "rmse": False, "mae": False}


def _num(xs: list[str]) -> list[float]:
    out = [float(x) for x in xs]
    if not all(math.isfinite(v) for v in out):
        raise ValueError("non-finite value (nan/inf) in a numeric metric input")
    return out


def _check(y_true: list[str], y_pred: list[str]) -> None:
    """Every metric fails closed: empty or misaligned inputs raise, never score."""
    if not y_true:
        raise ValueError("no labels to score")
    if len(y_true) != len(y_pred):
        raise ValueError(f"prediction count {len(y_pred)} != label count {len(y_true)}")


def accuracy(y_true: list[str], y_pred: list[str]) -> float:
    _check(y_true, y_pred)
    return sum(a == b for a, b in zip(y_true, y_pred, strict=True)) / len(y_true)


def f1_macro(y_true: list[str], y_pred: list[str]) -> float:
    _check(y_true, y_pred)
    labels = set(y_true)
    tp: Counter[str] = Counter()
    fp: Counter[str] = Counter()
    fn: Counter[str] = Counter()
    for t, p in zip(y_true, y_pred, strict=True):
        if t == p:
            tp[t] += 1
        else:
            fp[p] += 1
            fn[t] += 1
    f1s = []
    for c in labels:
        denom = 2 * tp[c] + fp[c] + fn[c]
        f1s.append(0.0 if denom == 0 else 2 * tp[c] / denom)
    return sum(f1s) / len(f1s)


def rmse(y_true: list[str], y_pred: list[str]) -> float:
    _check(y_true, y_pred)
    t, p = _num(y_true), _num(y_pred)
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(t, p, strict=True)) / len(t))


def mae(y_true: list[str], y_pred: list[str]) -> float:
    _check(y_true, y_pred)
    t, p = _num(y_true), _num(y_pred)
    return sum(abs(a - b) for a, b in zip(t, p, strict=True)) / len(t)


METRICS = {"accuracy": accuracy, "f1_macro": f1_macro, "rmse": rmse, "mae": mae}


def per_example(metric: str, y_true: list[str], y_pred: list[str]) -> list[float] | None:
    """Per-example values whose aggregate (see `aggregate`) equals the metric, or None when the metric is
    not decomposable (f1_macro). Lets resampling avoid recomputing the metric on string lists."""
    _check(y_true, y_pred)
    if metric == "accuracy":
        return [1.0 if a == b else 0.0 for a, b in zip(y_true, y_pred, strict=True)]
    if metric == "mae":
        return [abs(a - b) for a, b in zip(_num(y_true), _num(y_pred), strict=True)]
    if metric == "rmse":
        return [(a - b) ** 2 for a, b in zip(_num(y_true), _num(y_pred), strict=True)]
    return None


def aggregate(metric: str, mean_of_values: float) -> float:
    return math.sqrt(mean_of_values) if metric == "rmse" else mean_of_values


METRIC_RANGE = {"accuracy": (0.0, 1.0), "f1_macro": (0.0, 1.0), "rmse": (0.0, math.inf), "mae": (0.0, math.inf)}


def score(metric: str, y_true: list[str], y_pred: list[str]) -> float:
    if metric not in METRICS:
        raise ValueError(f"unregistered metric {metric!r}")
    return METRICS[metric](y_true, y_pred)
