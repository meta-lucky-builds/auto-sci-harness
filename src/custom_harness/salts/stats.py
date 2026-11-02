"""Exact binomial intervals for campaign rates, the exact upper bound of the detectability exam, and the paired
effect interval used to score positive salts. Stdlib only.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from statistics import NormalDist


def _check_counts(x: int, n: int) -> None:
    if not (isinstance(x, int) and isinstance(n, int)) or n < 1 or not 0 <= x <= n:
        raise ValueError(f"need integer counts with 0 <= x <= n and n >= 1 (got x={x}, n={n})")


@lru_cache(maxsize=64)
def _log_comb(n: int) -> tuple[float, ...]:
    return tuple(math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1) for k in range(n + 1))


def binom_pmf(n: int, p: float) -> list[float]:
    """P(X = k) for k = 0..n, X ~ Binomial(n, p)."""
    if p <= 0.0:
        return [1.0] + [0.0] * n
    if p >= 1.0:
        return [0.0] * n + [1.0]
    lp, lq = math.log(p), math.log1p(-p)
    return [math.exp(c + k * lp + (n - k) * lq) for k, c in enumerate(_log_comb(n))]


def _cdf_le(x: int, n: int, p: float) -> float:
    return min(1.0, sum(binom_pmf(n, p)[: x + 1]))


def _solve_decreasing(f, target: float) -> float:
    """p in [0, 1] with f(p) = target for f decreasing in p (bisection, 60 steps)."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if f(mid) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clopper_pearson(x: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Exact two-sided Clopper-Pearson interval for a binomial proportion x/n (coverage >= confidence)."""
    _check_counts(x, n)
    a = (1 - confidence) / 2
    lower = 0.0 if x == 0 else _solve_decreasing(lambda p: _cdf_le(x - 1, n, p), 1 - a)
    upper = 1.0 if x == n else _solve_decreasing(lambda p: _cdf_le(x, n, p), a)
    return lower, upper


# ---------------------------------------------------------------- the detectability exam's bound
_GRID = 400  # p0 and d both move on the grid i / 400: the nuisance sup is taken over its 401 points


@lru_cache(maxsize=8)
def _grid_tables(n: int) -> tuple[tuple[tuple[float, ...], ...], tuple[tuple[float, ...], ...]]:
    """pmf and cdf of Binomial(n, i / _GRID) for every grid point i."""
    pmfs, cdfs = [], []
    for i in range(_GRID + 1):
        f = binom_pmf(n, i / _GRID)
        c, s = [], 0.0
        for v in f:
            s += v
            c.append(min(1.0, s))
        pmfs.append(tuple(f))
        cdfs.append(tuple(c))
    return tuple(pmfs), tuple(cdfs)


def _pvalue_upper(t_obs: int, n1: int, n0: int, j: int) -> float:
    """sup over grid p0 of P(X1*n0 - X0*n1 <= t_obs), X1 ~ Bin(n1, p0 + d), X0 ~ Bin(n0, p0), d = j / _GRID: the
    exact unconditional p-value of H0: p1 - p0 >= d (small = the observed TPR - FPR is too low for a difference d)."""
    pm0, _ = _grid_tables(n0)
    _, cd1 = _grid_tables(n1)
    ks = [math.floor((t_obs + x0 * n1) / n0) for x0 in range(n0 + 1)]  # X1 <= k  <=>  X1*n0 - x0*n1 <= t_obs
    best = 0.0
    for i in range(max(0, -j), min(_GRID, _GRID - j) + 1):
        c1 = cd1[i + j]
        total = 0.0
        for w, k in zip(pm0[i], ks, strict=True):
            if k >= n1:
                total += w
            elif k >= 0:
                total += w * c1[k]
        best = max(best, total)
    return min(1.0, best)


@dataclass(frozen=True)
class DetectabilityBound:
    tp: int
    n_salt: int
    fp: int
    n_real: int
    d_hat: float
    upper: float  # exact one-sided (1 - alpha) upper confidence bound on TPR - FPR
    alpha: float


def detectability_upper_bound(tp: int, n_salt: int, fp: int, n_real: int, alpha: float) -> DetectabilityBound:
    """d_hat = TPR - FPR and its exact unconditional one-sided upper confidence bound: the confidence set is every d
    whose exact test (nuisance FPR maximised over a 401-point grid) is not rejected at level alpha; every d on the
    1/400 grid from d_hat up is tested (no monotonicity assumed), and the bound is the next grid point above the
    largest accepted d, so it errs high (a stricter exam). Approximation, labelled: the nuisance sup is a grid max."""
    _check_counts(tp, n_salt)
    _check_counts(fp, n_real)
    if not 0 < alpha < 1:
        raise ValueError("alpha must be in (0, 1)")
    t_obs = tp * n_real - fp * n_salt
    d_hat = tp / n_salt - fp / n_real
    start = math.floor(d_hat * _GRID)
    accepted = start
    for j in range(start, _GRID + 1):
        if _pvalue_upper(t_obs, n_salt, n_real, j) > alpha:
            accepted = j
    upper = min(1.0, (accepted + 1) / _GRID)
    return DetectabilityBound(tp, n_salt, fp, n_real, d_hat, max(upper, d_hat), alpha)


# ---------------------------------------------------------------- positive salts: the effect interval
@dataclass(frozen=True)
class EffectReport:
    """A reported effect (candidate minus baseline, higher = better) with its two-sided interval."""

    estimate: float
    lo: float
    hi: float
    confidence: float

    def __post_init__(self) -> None:
        if not (self.lo <= self.estimate <= self.hi) or not all(map(math.isfinite, (self.lo, self.hi))):
            raise ValueError(f"malformed effect report {self}")


def paired_accuracy_effect(y: Sequence[str], pred: Sequence[str], base: Sequence[str],
                           confidence: float) -> EffectReport:
    """Accuracy gain of `pred` over `base` on labels `y`, with a normal-approximation (CLT) interval on the paired
    per-row differences. Approximate, not exact: fine at the thousands of test rows a salt has."""
    if not (len(y) == len(pred) == len(base)) or not y:
        raise ValueError("labels, predictions and baseline predictions must be non-empty and aligned")
    d = [(p == t) - (b == t) for t, p, b in zip(y, pred, base, strict=True)]
    n = len(d)
    m = sum(d) / n
    var = sum((v - m) ** 2 for v in d) / (n - 1) if n > 1 else 0.0
    half = NormalDist().inv_cdf(1 - (1 - confidence) / 2) * math.sqrt(var / n)
    return EffectReport(m, m - half, m + half, confidence)
