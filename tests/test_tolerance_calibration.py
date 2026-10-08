"""GOAL §5 L3 calibrated tolerances: measured from noise floors (nominal / perturbed / rebuild runs, seed spread),
replacing hand-set tolerances where the claim class allows. The tolerance must cover honest re-runs at the declared
rate and reject a planted drift."""

from __future__ import annotations

import math
import random
from fractions import Fraction

import pytest

from custom_harness.kernel.claim import EvidenceClass
from custom_harness.verify import l3_config as C
from custom_harness.verify import tolerance as T

SD = {"nominal": 0.0005, "perturbed": 0.001, "rebuild": 0.002, "seed": 0.004}  # rebuild is the noisiest re-run


def draw(rng: random.Random, mu: float, source: str) -> float:
    return mu + rng.gauss(0.0, SD[source])


def floor(rng: random.Random, n: int, mu: float = 0.8) -> T.NoiseFloor:
    ref = draw(rng, mu, "nominal")
    return T.NoiseFloor(reference=ref, **{s: tuple(draw(rng, mu, s) for _ in range(n)) for s in SD})


def test_min_runs_is_the_conformal_requirement():
    assert T.min_runs(0.10) == 9 and T.min_runs(0.05) == 19
    for a in ("1/10", "1/20", "1/50"):
        q, n = 1 - Fraction(a), T.min_runs(float(Fraction(a)))
        assert math.ceil((n + 1) * q) <= n and math.ceil(n * q) > n - 1  # n suffices, n - 1 does not


def test_tolerance_covers_honest_reruns_of_every_source_at_the_declared_rate():
    rng, alpha, trials = random.Random(16), 0.10, 600
    misses = {s: 0 for s in ("nominal", "perturbed", "rebuild")}
    naive_rebuild_misses = 0
    for _ in range(trials):
        f = floor(rng, T.min_runs(alpha))
        cal = T.calibrate(f, miss_rate=alpha)
        for s in misses:
            misses[s] += not cal.accepts(draw(rng, 0.8, s))
        # the naive alternative: a tolerance from repeated NOMINAL runs only (what a hand calibration would do)
        nominal_only = sorted(abs(v - f.reference) for v in f.nominal)[-1]
        naive_rebuild_misses += abs(draw(rng, 0.8, "rebuild") - f.reference) > nominal_only
    bound = alpha + 3 * math.sqrt(alpha * (1 - alpha) / trials)
    for s, m in misses.items():
        assert m / trials <= bound, (s, m / trials)
    # the attack the per-source rule exists for: nominal-only tolerances reject honest rebuilds far too often
    assert naive_rebuild_misses / trials > 0.3


def test_calibrated_tolerance_rejects_a_planted_drift_the_hand_set_ceiling_accepts():
    rng = random.Random(7)
    f = floor(rng, T.min_runs(0.05))
    cal = T.calibrate(f, miss_rate=0.05)
    drift = 0.01
    assert drift <= C.TOL_CEILING  # the attack: the Kernel's hand-set ceiling would let this drift through
    assert abs(f.reference + drift - f.reference) <= C.TOL_CEILING
    assert cal.tolerance < drift and not cal.accepts(f.reference + drift) and not cal.accepts(f.reference - drift)
    assert cal.accepts(f.reference)


def test_seed_purpose_includes_seed_spread_and_reproduce_does_not():
    f = floor(random.Random(3), 19)
    rep, seed = T.calibrate(f, miss_rate=0.05), T.calibrate(f, miss_rate=0.05, purpose="seed")
    assert set(rep.per_source) == {"nominal", "perturbed", "rebuild"}
    assert set(seed.per_source) == {"nominal", "perturbed", "rebuild", "seed"}
    assert seed.tolerance == max(seed.per_source.values()) >= seed.per_source["seed"]
    assert rep.tolerance == max(rep.per_source.values()) < seed.tolerance


def test_identical_reruns_calibrate_to_zero():
    f = T.NoiseFloor(reference=0.5, nominal=(0.5,) * 9, perturbed=(0.5,) * 9, rebuild=(0.5,) * 9, seed=(0.5,) * 9)
    assert T.calibrate(f, miss_rate=0.1).tolerance == 0.0


@pytest.mark.parametrize("change, alpha, why", [
    ({"rebuild": (0.8,) * 5}, 0.10, "runs"),  # too few rebuild runs for the declared rate
    ({"perturbed": ()}, 0.10, "runs"),
    ({}, 0.5, "miss rate"),  # looser than TOL_MAX_MISS_RATE
    ({}, 0.0, "miss rate"),
    ({"nominal": (0.8,) * 8 + (float("nan"),)}, 0.10, "finite"),
    ({"reference": float("inf")}, 0.10, "finite"),
    ({"rebuild": tuple(0.8 + 0.05 * (-1) ** i for i in range(9))}, 0.10, "ceiling"),  # noise above K1's ceiling
])
def test_calibration_refuses(change, alpha, why):
    base = {"reference": 0.8, **{s: tuple(0.8 + 1e-4 * i for i in range(9)) for s in SD}}
    with pytest.raises(T.ToleranceError, match=why):
        T.calibrate(T.NoiseFloor(**{**base, **change}), miss_rate=alpha)
    with pytest.raises(T.ToleranceError, match="purpose"):
        T.calibrate(T.NoiseFloor(**base), miss_rate=0.1, purpose="whatever")


def test_calibrated_replaces_hand_set_only_where_the_class_allows():
    rng = random.Random(11)
    cal = T.calibrate(floor(rng, 9), miss_rate=0.10)
    too_tight = 0.0001
    # the attack on a too-tight hand-set value: honest rebuilds fail it most of the time
    assert sum(abs(draw(rng, 0.8, "rebuild") - 0.8) > too_tight for _ in range(200)) > 150
    r = T.resolve_tolerance(EvidenceClass.DATA, too_tight, cal)
    assert r.value == cal.tolerance and r.source == "calibrated" and r.reasons == ()
    r = T.resolve_tolerance(EvidenceClass.SIMULATED, 0.02, cal)
    assert r.value == cal.tolerance and r.source == "calibrated"
    r = T.resolve_tolerance(EvidenceClass.DATA, 0.01, None)
    assert r.value == 0.01 and r.source == "hand_set"
    for exact in (EvidenceClass.COMPUTATIONAL, EvidenceClass.FORMAL):
        r = T.resolve_tolerance(exact, 0.0, cal)
        assert r.value == 0.0 and r.source == "exact" and any("exact" in x for x in r.reasons)
        assert T.resolve_tolerance(exact, 0.01, None).value is None  # an exact class never takes a tolerance
    assert T.resolve_tolerance(EvidenceClass.PHYSICAL, 0.01, cal).value is None
    assert T.resolve_tolerance(EvidenceClass.DATA, C.TOL_CEILING * 2, None).value is None
