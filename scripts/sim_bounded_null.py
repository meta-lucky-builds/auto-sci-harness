"""Monte Carlo study of bounded-null certificates (GOAL §16.10; docs/bounded_null.md).

Runs the Kernel's own code (kernel/ml.py: criterion, plasmode; verify/stats.py: the registered intervals) on
synthetic paired classification data with a KNOWN true effect, outside the Kernel world (no store, no sandbox):

  A. false exclusion: true effect = delta. How often is "effect < delta" certified (criterion; criterion + plasmode)?
     Must be <= alpha (one-sided, the K4 level alpha_eff).
  B. vacuous delta: true effect 0 on a tiny target where delta is NOT detectable at the declared power, and a delta
     no effect can reach. Certification rate with and without the plasmode.
  C. power: true effect 0, a grid of sample sizes. The procedure's true power (P(delta excluded) and P(an effect of
     delta detected, lower limit > 0), from fresh datasets) vs the plasmode's pass rate at declared power 0.8.

Every rate is reported with its Monte Carlo standard error sqrt(p (1 - p) / R). Deterministic (fixed seeds).
Usage: python scripts/sim_bounded_null.py [R] (default 200; part A uses 10 R).
"""

from __future__ import annotations

import hashlib
import math
import random
import sys
import time

from custom_harness.kernel import ml
from custom_harness.kernel.claim import ClaimKind
from custom_harness.verify import metrics as M
from custom_harness.verify.stats import mean_t_interval, paired_example_interval

ALPHA = 0.05  # one-sided level alpha_eff (the registered intervals are two-sided at 2 * alpha)
N_BOOT = 1000  # the Kernel's minimum (KernelConfig.min_boot)
SEEDS = 3  # seed-invariant programs: identical predictions on every seed (the seed interval is degenerate)
GAMMA = 0.1  # KernelConfig.plasmode_gamma


class _Claim:  # the two fields ml.criterion reads
    def __init__(self, delta: float) -> None:
        self.kind = ClaimKind.NEGATIVE_RESULT
        self.effect = type("E", (), {"min_effect": delta})()


def dataset(rng: random.Random, n: int, p_comp: float, p_subj: float) -> tuple[list[str], list[str], list[str]]:
    """Binary labels; each arm independently correct per example with its accuracy."""
    y = [rng.choice("01") for _ in range(n)]

    def arm(p: float) -> list[str]:
        return [t if rng.random() < p else ("1" if t == "0" else "0") for t in y]

    return y, arm(p_subj), arm(p_comp)


def stats(y: list[str], s: list[str], c: list[str]) -> ml.TargetStats:
    """The registered test exactly as ml.statistics runs it (t over seeds, BCa over examples, level 2 alpha)."""
    seed_ci = mean_t_interval([M.accuracy(y, s) - M.accuracy(y, c)] * SEEDS, alpha=2 * ALPHA)
    ex = paired_example_interval(M.accuracy, y, [s] * SEEDS, [c] * SEEDS, higher_is_better=True, alpha=2 * ALPHA,
                                 n_boot=N_BOOT, seed=0)
    return ml.TargetStats(seed_ci, ex, ALPHA, False)


def plantings(power: float) -> int:
    return max(1, math.ceil(math.log(GAMMA) / math.log(power) - 1e-12))


def run(y: list[str], s: list[str], c: list[str], delta: float, power: float, tag: str) -> tuple[bool, bool, bool]:
    """(criterion met, plasmode passed, lower limit > 0) for one dataset."""
    st = stats(y, s, c)
    met = ml.criterion(_Claim(delta), st.seed, st.example)  # type: ignore[arg-type]
    seed = hashlib.sha256(tag.encode()).digest()
    why = ml.plasmode("accuracy", True, y, [s] * SEEDS, [c] * SEEDS, delta, st, N_BOOT, plantings(power), seed)
    return met, why is None, (not st.example.degenerate and st.example.lo > 0)


def rate(hits: int, r: int) -> str:
    p = hits / r
    return f"{p:.3f} +- {math.sqrt(p * (1 - p) / r):.3f}"


def main() -> None:
    r = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    t0 = time.time()
    rng = random.Random(20261014)
    print(f"alpha (one-sided) {ALPHA}, n_boot {N_BOOT}, gamma {GAMMA}, declared power 0.8 -> {plantings(0.8)} "
          f"plantings; R = {r} (part A: {10 * r}); rate +- Monte Carlo SE")

    print("\nA. false exclusion at true effect = delta = 0.1 (accuracy 0.70 vs 0.80)")
    for n in (100, 400):
        ra = 10 * r
        crit = cert = 0
        for i in range(ra):
            y, s, c = dataset(rng, n, 0.70, 0.80)
            st = stats(y, s, c)
            if ml.criterion(_Claim(0.1), st.seed, st.example):  # type: ignore[arg-type]
                crit += 1
                why = ml.plasmode("accuracy", True, y, [s] * SEEDS, [c] * SEEDS, 0.1, st, N_BOOT, plantings(0.8),
                                  hashlib.sha256(f"A{n}-{i}".encode()).digest())
                cert += why is None
        print(f"  n={n}: criterion met {rate(crit, ra)}; certified (criterion + plasmode) {rate(cert, ra)}")

    print("\nB. vacuous delta, true effect 0 (accuracy 0.70 vs 0.70)")
    for n, delta in ((20, 0.3), (60, 0.9)):
        crit = cert = plant_ok = 0
        for i in range(r):
            y, s, c = dataset(rng, n, 0.70, 0.70)
            met, ok, _ = run(y, s, c, delta, 0.8, f"B{n}-{i}")
            crit += met
            plant_ok += ok
            cert += met and ok
        print(f"  n={n}, delta={delta}: criterion alone (the v1 rule without K7) {rate(crit, r)}; plasmode passed "
              f"{rate(plant_ok, r)}; certified {rate(cert, r)}")

    print("\nC. power at declared 0.8, delta = 0.15, accuracy 0.70 (both arms; fresh datasets per row)")
    print("  n    | P(delta excluded | effect 0) | P(lower limit > 0 | effect delta) | plasmode passed (effect 0) "
          "| certified (effect 0)")
    for n in (40, 80, 120, 200, 400):
        excl = det = plant_ok = cert = 0
        for i in range(r):
            y, s, c = dataset(rng, n, 0.70, 0.70)
            met, ok, _ = run(y, s, c, 0.15, 0.8, f"C{n}-{i}")
            excl += met
            plant_ok += ok
            cert += met and ok
            y, s, c = dataset(rng, n, 0.70, 0.85)
            det += stats(y, s, c).example.lo > 0
        print(f"  {n:<4} | {rate(excl, r):<27} | {rate(det, r):<33} | {rate(plant_ok, r):<26} | {rate(cert, r)}")
    print(f"\nwall time {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
