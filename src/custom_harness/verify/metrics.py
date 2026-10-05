"""Trusted metric implementations (run on the host, never by agent code). Stdlib only."""

from __future__ import annotations

import math
from collections import Counter

HIGHER_IS_BETTER = {"accuracy": True, "f1_macro": True, "rmse": False, "mae": False}


def _num(xs: list[str]) -> list[float]:
    return [float(x) for x in xs]


def accuracy(y_true: list[str], y_pred: list[str]) -> float:
    return sum(a == b for a, b in zip(y_true, y_pred)) / len(y_true)


def f1_macro(y_true: list[str], y_pred: list[str]) -> float:
    labels = set(y_true)
    tp, fp, fn = Counter(), Counter(), Counter()
    for t, p in zip(y_true, y_pred):
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
    t, p = _num(y_true), _num(y_pred)
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(t, p)) / len(t))


def mae(y_true: list[str], y_pred: list[str]) -> float:
    t, p = _num(y_true), _num(y_pred)
    return sum(abs(a - b) for a, b in zip(t, p)) / len(t)


METRICS = {"accuracy": accuracy, "f1_macro": f1_macro, "rmse": rmse, "mae": mae}


def score(metric: str, y_true: list[str], y_pred: list[str]) -> float:
    if len(y_true) != len(y_pred):
        raise ValueError(f"prediction count {len(y_pred)} != label count {len(y_true)}")
    return METRICS[metric](y_true, y_pred)
