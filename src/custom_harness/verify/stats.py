"""Statistical verifier (T2, PLAN D3). Stdlib only, deterministic given a seed."""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass


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
    """Percentile bootstrap CI for mean(treatment) - mean(baseline), resampling each group independently."""
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


def paired_bootstrap_metric(metric_fn, y_true: list[str], pred_a: list[str], pred_b: list[str], *,
                            higher_is_better: bool, n_boot: int = 2_000, alpha: float = 0.05,
                            seed: int = 0) -> DiffCI:
    """CI for metric(a) - metric(b) under resampling of TEST EXAMPLES (paired).
    Captures test-set sampling variance, which seed-only CIs miss."""
    n = len(y_true)
    sign = 1.0 if higher_is_better else -1.0
    rng = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        t = [y_true[i] for i in idx]
        diffs.append(sign * (metric_fn(t, [pred_a[i] for i in idx]) - metric_fn(t, [pred_b[i] for i in idx])))
    diffs.sort()
    mean = sign * (metric_fn(y_true, pred_a) - metric_fn(y_true, pred_b))
    return DiffCI(mean, diffs[int((alpha / 2) * n_boot)], diffs[min(n_boot - 1, int((1 - alpha / 2) * n_boot))],
                  n, n, alpha)


@dataclass(frozen=True)
class T2Verdict:
    verified: bool
    reason: str
    ci: DiffCI | None


def t2_verdict(treatment: list[float], baseline: list[float], *, higher_is_better: bool,
               min_seeds: int = 3, seed: int = 0) -> T2Verdict:
    """PLAN D3: >= min_seeds per arm and the 95% CI of the improvement excludes zero.

    Honest caveat: with 3-5 seeds a percentile bootstrap is coarse; it is a floor, not proof.
    """
    if len(treatment) < min_seeds or len(baseline) < min_seeds:
        return T2Verdict(False, f"need >= {min_seeds} seeds per arm "
                                f"(have {len(treatment)} vs {len(baseline)})", None)
    ci = bootstrap_diff_ci(treatment, baseline, higher_is_better=higher_is_better, seed=seed)
    if ci.lo > 0:
        return T2Verdict(True, f"improvement {ci.mean_diff:+.4g}, 95% CI [{ci.lo:+.4g}, {ci.hi:+.4g}]", ci)
    if ci.hi < 0:
        return T2Verdict(False, f"significantly WORSE: {ci.mean_diff:+.4g}, CI [{ci.lo:+.4g}, {ci.hi:+.4g}]", ci)
    return T2Verdict(False, f"inconclusive: {ci.mean_diff:+.4g}, CI [{ci.lo:+.4g}, {ci.hi:+.4g}] includes 0", ci)
