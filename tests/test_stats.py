"""Statistical primitives: reference values and coverage simulations (M0 review fixes)."""

import random
import statistics

import pytest

from custom_harness.verify import metrics as M
from custom_harness.verify.stats import bca_interval, mean_t_interval, paired_example_interval, t_cdf, t_ppf


@pytest.mark.parametrize(("q", "df", "ref"), [(0.975, 1, 12.706205), (0.975, 2, 4.302653), (0.975, 9, 2.262157),
                                              (0.975, 30, 2.042272), (0.95, 4, 2.131847), (0.999, 5, 5.893430)])
def test_t_quantiles_match_reference_tables(q, df, ref):
    assert abs(t_ppf(q, df) - ref) < 1e-5
    assert abs(t_cdf(ref, df) - q) < 1e-6


def test_t_interval_coverage_is_nominal_at_small_n():
    rng = random.Random(1)
    trials, hits = 2000, 0
    for _ in range(trials):
        xs = [rng.gauss(0.3, 1.0) for _ in range(4)]
        ci = mean_t_interval(xs)
        hits += ci.lo <= 0.3 <= ci.hi
    assert 0.93 <= hits / trials <= 0.97


def test_degenerate_cases_are_flagged_not_trusted():
    assert mean_t_interval([0.2, 0.2, 0.2]).degenerate
    assert mean_t_interval([0.2]).lo == float("-inf")
    assert bca_interval(1, lambda idx: 0.0).degenerate
    assert bca_interval(10, lambda idx: 1.0).degenerate


def test_bca_paired_accuracy_interval_coverage():
    """Two classifiers with true accuracies 0.80 vs 0.75 on n=200 examples (correlated errors)."""
    rng = random.Random(3)
    trials, hits = 150, 0
    for t in range(trials):
        y = ["1"] * 200
        a, b = [], []
        for _ in range(200):
            u = rng.random()
            a.append("1" if u < 0.80 else "0")
            b.append("1" if (u < 0.75 if rng.random() < 0.8 else rng.random() < 0.75) else "0")
        ci = paired_example_interval(M.accuracy, y, [a], [b], higher_is_better=True, n_boot=400, seed=t)
        hits += ci.lo <= 0.05 <= ci.hi
    assert hits / trials >= 0.90  # nominal 0.95; 150 trials -> SE ~ 0.018


def test_paired_example_interval_averages_over_seeds_and_respects_direction():
    y = ["1.0", "2.0", "3.0", "4.0"] * 10
    good = [["1.0", "2.0", "3.0", "4.0"] * 10, ["1.1", "2.0", "3.0", "4.0"] * 10]
    bad = [["2.0", "3.0", "4.0", "5.0"] * 10, ["2.0", "3.0", "4.0", "5.0"] * 10]
    ci = paired_example_interval(M.mae, y, good, bad, higher_is_better=False, n_boot=300)
    assert ci.estimate > 0.9 and ci.lo > 0
    with pytest.raises(ValueError):
        paired_example_interval(M.mae, y, good, bad[:1], higher_is_better=False)


def test_metrics_fail_closed():
    with pytest.raises(ValueError):
        M.accuracy([], [])
    with pytest.raises(ValueError):
        M.rmse(["1", "2"], ["1"])
    with pytest.raises(ValueError):
        M.mae(["1", "2"], ["nan", "2"])
    with pytest.raises(ValueError):
        M.score("not_a_metric", ["1"], ["1"])
    assert statistics.fmean([M.accuracy(["a"], ["a"])]) == 1.0
