"""Coupled-Goodhart indicator (GOAL §5 L8, §11 M9 "coupled-Goodhart gap not widening").

gap(t) = A-score(t) - sealed score(t): how much better the system looks on the verifier set it is optimised against
than on the sealed split. The indicator is the Theil-Sen slope (median of pairwise slopes, robust to one outlier) of the
gap over the last `config.GOODHART_WINDOW` points; it alarms when the slope exceeds `config.GOODHART_SLOPE_ALARM`.
Fewer than `config.GOODHART_MIN_POINTS` points give "insufficient_data", never "ok". Non-finite scores and times that
do not strictly increase are errors (fail closed).

The inputs include sealed scores, so the report is an OWNER-facing surface: it must not be fed to the evolver
(invariant 18). This module does not enforce where its output goes.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from . import config as C


@dataclass(frozen=True)
class GapPoint:
    t: float
    a_score: float
    sealed_score: float


@dataclass(frozen=True)
class GoodhartReport:
    status: str  # alarm | ok | insufficient_data
    slope: float | None
    gaps: tuple[float, ...]
    window: int
    threshold: float


def goodhart_indicator(points: Sequence[GapPoint], *, window: int = C.GOODHART_WINDOW,
                       min_points: int = C.GOODHART_MIN_POINTS,
                       slope_alarm: float = C.GOODHART_SLOPE_ALARM) -> GoodhartReport:
    for p in points:
        if not all(math.isfinite(x) for x in (p.t, p.a_score, p.sealed_score)):
            raise ValueError(f"non-finite point {p}")
    if any(b.t <= a.t for a, b in itertools.pairwise(points)):
        raise ValueError("time must strictly increase")
    gaps = tuple(p.a_score - p.sealed_score for p in points)
    recent = list(points)[-window:]
    if len(recent) < min_points:
        return GoodhartReport("insufficient_data", None, gaps, window, slope_alarm)
    slope = statistics.median(((b.a_score - b.sealed_score) - (a.a_score - a.sealed_score)) / (b.t - a.t)
                              for i, a in enumerate(recent) for b in recent[i + 1:])
    return GoodhartReport("alarm" if slope > slope_alarm else "ok", slope, gaps, window, slope_alarm)
