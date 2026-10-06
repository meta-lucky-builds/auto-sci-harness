"""Statistical verifier (T2, PLAN D3). Stdlib only, deterministic given a seed.

Design (fixes found in M0 review):
- Seeds are PAIRED (both arms run on the same seeds): seed-level evidence is a Student-t interval on the
  per-seed differences, exact under normality and honest at small n (a bootstrap over 3 values is not).
- Zero spread across seeds (a seed-invariant method) makes the seed interval DEGENERATE: it is reported as
  such and never counted as evidence; uncertainty must then come from test-example resampling.
- Test-example resampling uses BCa (bias-corrected and accelerated) intervals, averaged over all seeds,
  instead of percentile intervals on one seed's predictions.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass

_NORMAL = statistics.NormalDist()
_REL_TOL = 1e-9  # spread below this (relative to the scale of the values) is floating-point noise, not variance


def _negligible(spread: float, scale: float) -> bool:
    return spread <= _REL_TOL * max(1.0, abs(scale))


# ---------------------------------------------------------------- Student t distribution (stdlib only)
def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta function (modified Lentz)."""
    tiny, eps = 1e-300, 3e-16
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc_reg(a: float, b: float, x: float) -> float:
    if not 0.0 <= x <= 1.0:
        raise ValueError("x must be in [0, 1]")
    if x in (0.0, 1.0):
        return x
    ln_front = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(ln_front) * _betacf(a, b, x) / a
    return 1.0 - math.exp(ln_front) * _betacf(b, a, 1.0 - x) / b


def t_cdf(t: float, df: float) -> float:
    if df <= 0:
        raise ValueError("df must be positive")
    if math.isinf(t):
        return 1.0 if t > 0 else 0.0
    tail = 0.5 * betainc_reg(df / 2.0, 0.5, df / (df + t * t))
    return 1.0 - tail if t > 0 else tail


def t_ppf(q: float, df: float) -> float:
    """Inverse CDF by bisection (monotone, robust; ~60 iterations to machine precision)."""
    if not 0.0 < q < 1.0:
        raise ValueError("q must be in (0, 1)")
    lo, hi = -1e6, 1e6
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12 * max(1.0, abs(mid)):
            break
    return (lo + hi) / 2.0


# ---------------------------------------------------------------- intervals
@dataclass(frozen=True)
class Interval:
    estimate: float
    lo: float
    hi: float
    n: int
    alpha: float  # two-sided level: [lo, hi] is a (1 - alpha) interval; lo alone is a (1 - alpha/2) bound
    method: str
    degenerate: bool = False  # zero observed spread: the interval carries no uncertainty information


def mean_t_interval(values: Sequence[float], *, alpha: float = 0.05) -> Interval:
    """Two-sided Student-t interval for the mean. n < 2 -> (-inf, inf); zero spread -> degenerate."""
    n = len(values)
    if n == 0:
        return Interval(math.nan, -math.inf, math.inf, 0, alpha, "t", degenerate=True)
    mean = statistics.fmean(values)
    if n < 2:
        return Interval(mean, -math.inf, math.inf, n, alpha, "t", degenerate=True)
    sd = statistics.stdev(values)
    if not math.isfinite(sd):
        return Interval(math.nan, -math.inf, math.inf, n, alpha, "t", degenerate=True)
    if _negligible(sd, mean):
        return Interval(mean, mean, mean, n, alpha, "t", degenerate=True)
    half = t_ppf(1.0 - alpha / 2.0, n - 1) * sd / math.sqrt(n)
    return Interval(mean, mean - half, mean + half, n, alpha, "t")


def bca_interval(n: int, stat: Callable[[Sequence[int]], float], *, alpha: float = 0.05, n_boot: int = 2000,
                 seed: int = 0, max_jackknife_blocks: int = 200) -> Interval:
    """BCa bootstrap interval for a statistic of n resampled units (Efron 1987).

    `stat(indices)` evaluates the statistic on a multiset of unit indices. The acceleration uses a
    (grouped, for large n) jackknife. All bootstrap values equal -> degenerate point interval."""
    if n < 2:
        return Interval(math.nan, -math.inf, math.inf, n, alpha, "bca", degenerate=True)
    full = list(range(n))
    theta = stat(full)
    rng = random.Random(seed)
    population = range(n)
    boots = sorted(stat(rng.choices(population, k=n)) for _ in range(n_boot))
    if _negligible(boots[-1] - boots[0], theta):
        return Interval(theta, theta, theta, n, alpha, "bca", degenerate=True)
    below = sum(b < theta for b in boots) + 0.5 * sum(b == theta for b in boots)
    p0 = min(max(below / n_boot, 1.0 / (n_boot + 1)), n_boot / (n_boot + 1))
    z0 = _NORMAL.inv_cdf(p0)
    blocks = min(n, max_jackknife_blocks)
    size = math.ceil(n / blocks)
    jack = []
    for start in range(0, n, size):
        jack.append(stat(full[:start] + full[start + size:]))
    jbar = statistics.fmean(jack)
    num = sum((jbar - j) ** 3 for j in jack)
    den = 6.0 * (sum((jbar - j) ** 2 for j in jack) ** 1.5)
    acc = num / den if den > 0 else 0.0

    def adj(z_alpha: float) -> float:
        x = z0 + z_alpha
        return _NORMAL.cdf(z0 + x / (1.0 - acc * x))

    def pick(p: float) -> float:
        k = min(n_boot - 1, max(0, int(p * n_boot)))
        return boots[k]

    lo = pick(adj(_NORMAL.inv_cdf(alpha / 2.0)))
    hi = pick(adj(_NORMAL.inv_cdf(1.0 - alpha / 2.0)))
    return Interval(theta, lo, hi, n, alpha, "bca")


def paired_example_interval(metric_fn: Callable[[list[str], list[str]], float], y_true: list[str],
                            preds_a: Sequence[list[str]], preds_b: Sequence[list[str]], *, higher_is_better: bool,
                            alpha: float = 0.05, n_boot: int = 2000, seed: int = 0) -> Interval:
    """BCa interval for [metric(a) - metric(b)] under resampling of TEST EXAMPLES, averaged over seeds
    (preds_a[k], preds_b[k] are the two arms' predictions for seed k). Positive = a is better."""
    if not preds_a or len(preds_a) != len(preds_b):
        raise ValueError("need the same, non-zero number of seeds for both arms")
    sign = 1.0 if higher_is_better else -1.0
    pairs = list(zip(preds_a, preds_b, strict=True))
    from . import metrics as _m
    name = next((k for k, f in _m.METRICS.items() if f is metric_fn), None)
    if name is not None and _m.per_example(name, y_true, preds_a[0]) is not None:
        # fast path for decomposable metrics: per-example values once, then resampling only averages
        cols: list[tuple[list[float], list[float]]] = []
        for pa, pb in pairs:
            va, vb = _m.per_example(name, y_true, pa), _m.per_example(name, y_true, pb)
            if va is None or vb is None:
                raise AssertionError("decomposable metric returned no per-example values")
            cols.append((va, vb))
        if name != "rmse":  # linear aggregate: average over seeds per example first, then resample once
            d = [sign * statistics.fmean(va[i] - vb[i] for va, vb in cols) for i in range(len(y_true))]
            return bca_interval(len(y_true), lambda idx: sum(d[i] for i in idx) / len(idx), alpha=alpha,
                                n_boot=n_boot, seed=seed)

        def fast(idx: Sequence[int]) -> float:
            k = len(idx)
            total = 0.0
            for va, vb in cols:
                sa = sum(va[i] for i in idx) / k
                sb = sum(vb[i] for i in idx) / k
                total += _m.aggregate(name, sa) - _m.aggregate(name, sb)
            return sign * total / len(cols)

        return bca_interval(len(y_true), fast, alpha=alpha, n_boot=n_boot, seed=seed)

    def stat(idx: Sequence[int]) -> float:
        t = [y_true[i] for i in idx]
        return sign * statistics.fmean(metric_fn(t, [pa[i] for i in idx]) - metric_fn(t, [pb[i] for i in idx])
                                       for pa, pb in pairs)

    return bca_interval(len(y_true), stat, alpha=alpha, n_boot=n_boot, seed=seed)


# ---------------------------------------------------------------- backwards-compatible helpers
@dataclass(frozen=True)
class DiffCI:
    mean_diff: float  # treatment - baseline, sign-corrected so positive = better
    lo: float
    hi: float
    n_treatment: int
    n_baseline: int
    alpha: float


def bootstrap_diff_ci(treatment: list[float], baseline: list[float], *, higher_is_better: bool = True,
                      n_boot: int = 10_000, alpha: float = 0.05, seed: int = 0) -> DiffCI:
    """Percentile bootstrap for mean(treatment) - mean(baseline) with INDEPENDENT groups.
    Use only when the groups are not paired; paired designs use mean_t_interval on differences."""
    if not treatment or not baseline:
        raise ValueError("both groups need at least one value")
    sign = 1.0 if higher_is_better else -1.0
    rng = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        t = [rng.choice(treatment) for _ in treatment]
        b = [rng.choice(baseline) for _ in baseline]
        diffs.append(sign * (statistics.fmean(t) - statistics.fmean(b)))
    diffs.sort()
    lo = diffs[int((alpha / 2) * n_boot)]
    hi = diffs[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    mean = sign * (statistics.fmean(treatment) - statistics.fmean(baseline))
    return DiffCI(mean, lo, hi, len(treatment), len(baseline), alpha)


@dataclass(frozen=True)
class T2Verdict:
    verified: bool
    reason: str
    ci: Interval | None
    degenerate: bool = False


def t2_verdict(treatment: list[float], baseline: list[float], *, higher_is_better: bool,
               min_seeds: int = 3, alpha: float = 0.05) -> T2Verdict:
    """Seed-level T2 check on PAIRED seeds (treatment[k] and baseline[k] used the same seed k).

    verified iff >= min_seeds pairs and the Student-t interval of the per-seed differences excludes 0.
    A degenerate interval (zero spread across seeds) never verifies: it carries no uncertainty
    information, so the caller must rely on test-example resampling and report the seed check as
    uninformative."""
    if len(treatment) != len(baseline):
        return T2Verdict(False, f"seeds not paired ({len(treatment)} vs {len(baseline)} values)", None)
    if len(treatment) < min_seeds:
        return T2Verdict(False, f"need >= {min_seeds} paired seeds (have {len(treatment)})", None)
    sign = 1.0 if higher_is_better else -1.0
    diffs = [sign * (t - b) for t, b in zip(treatment, baseline, strict=True)]
    ci = mean_t_interval(diffs, alpha=alpha)
    if ci.degenerate:
        return T2Verdict(False, f"seed check uninformative: identical difference {ci.estimate:+.4g} on every seed "
                                f"(seed-invariant method)", ci, degenerate=True)
    if ci.lo > 0:
        return T2Verdict(True, f"improvement {ci.estimate:+.4g}, paired-seed t {1 - alpha:.0%} CI "
                               f"[{ci.lo:+.4g}, {ci.hi:+.4g}]", ci)
    if ci.hi < 0:
        return T2Verdict(False, f"significantly WORSE: {ci.estimate:+.4g}, CI [{ci.lo:+.4g}, {ci.hi:+.4g}]", ci)
    return T2Verdict(False, f"inconclusive: {ci.estimate:+.4g}, CI [{ci.lo:+.4g}, {ci.hi:+.4g}] includes 0", ci)
