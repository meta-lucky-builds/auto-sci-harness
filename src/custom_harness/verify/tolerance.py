"""Calibrated tolerances from measured noise floors (GOAL §5 L3, ScienceIDE row of §9).

A re-execution check compares a re-run's number with the reported one: |rerun - reported| <= tolerance. A hand-set
tolerance is either too tight (honest re-runs fail) or too loose (a real drift passes). Here it is MEASURED.

Noise floor: the reported run's value (`reference`) plus honest re-runs from each noise source:
  nominal   - the same program, same seed, same environment, run again;
  perturbed - the same seed with harmless perturbations (thread count, input row order, BLAS backend ...);
  rebuild   - the same seed in a freshly rebuilt environment (new container, reinstalled dependencies);
  seed      - different seeds (seed spread; used only when the tolerance is for an across-seed comparison).

Rule (split-conformal, distribution-free): for one source with n deviations d_i = |run_i - reference|, the k-th
smallest, k = ceil((n + 1)(1 - a)), bounds the deviation of a NEW honest re-run of that source with probability
>= 1 - a, if that re-run is exchangeable with the n measured ones (the stated assumption). The tolerance is the
MAXIMUM over the sources the purpose needs, so the guarantee holds whichever kind of honest re-run comes (pooling
would let quiet nominal runs hide a noisy rebuild). If k > n the source has too few runs and calibration refuses.

Bounds: the miss rate a caller may declare is at most `l3_config.TOL_MAX_MISS_RATE`; a calibrated tolerance above
the Kernel's K1 ceiling (`TOL_CEILING`) is refused (the claim cannot be checked by re-execution at that noise), never
clipped. `resolve_tolerance` applies the class policy: exact classes never take a tolerance.

Wiring (not done here, other owners): `research/claims.py` would pass `resolve_tolerance(...).value` as
`AnalysisPlan.reproduce_tolerance`; the noise-floor runs must be produced by the runner. Stated in docs/l3_verifiers.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

from ..kernel.claim import EvidenceClass
from . import l3_config as C

SOURCES = ("nominal", "perturbed", "rebuild", "seed")
PURPOSES = {"reproduce": ("nominal", "perturbed", "rebuild"), "seed": SOURCES}


class ToleranceError(ValueError):
    pass


@dataclass(frozen=True)
class NoiseFloor:
    reference: float
    nominal: tuple[float, ...]
    perturbed: tuple[float, ...]
    rebuild: tuple[float, ...]
    seed: tuple[float, ...]


@dataclass(frozen=True)
class Calibration:
    reference: float
    tolerance: float
    miss_rate: float
    purpose: str
    per_source: dict[str, float]
    runs: dict[str, int]

    def accepts(self, value: float) -> bool:
        return math.isfinite(value) and abs(value - self.reference) <= self.tolerance


@dataclass(frozen=True)
class Resolved:
    value: float | None  # None = no tolerance may be used: the claim cannot be checked this way
    source: str  # "calibrated" | "hand_set" | "exact" | "refused"
    reasons: tuple[str, ...]


def _rank(n: int, miss_rate: float) -> int:
    """k = ceil((n + 1)(1 - a)), computed exactly (a float product can land just above an integer)."""
    return math.ceil((n + 1) * (1 - Fraction(repr(miss_rate))))


def min_runs(miss_rate: float) -> int:
    """The fewest re-runs per source for which the conformal bound exists at this miss rate."""
    n = 1
    while _rank(n, miss_rate) > n:
        n += 1
    return n


def calibrate(floor: NoiseFloor, *, miss_rate: float, purpose: str = "reproduce") -> Calibration:
    if purpose not in PURPOSES:
        raise ToleranceError(f"unknown purpose {purpose!r} (one of {sorted(PURPOSES)})")
    if not 0 < miss_rate <= C.TOL_MAX_MISS_RATE:
        raise ToleranceError(f"declared miss rate {miss_rate} must be in (0, {C.TOL_MAX_MISS_RATE}]")
    if not math.isfinite(floor.reference):
        raise ToleranceError("the reference value must be finite")
    per_source, runs = {}, {}
    for s in PURPOSES[purpose]:
        values = getattr(floor, s)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
            raise ToleranceError(f"every {s} run value must be a finite number")
        k = _rank(len(values), miss_rate)
        if k > len(values):
            raise ToleranceError(f"{len(values)} {s} runs are too few for miss rate {miss_rate}: "
                                 f"need >= {min_runs(miss_rate)} runs per source")
        per_source[s] = sorted(abs(v - floor.reference) for v in values)[k - 1]
        runs[s] = len(values)
    tol = max(per_source.values())
    if tol > C.TOL_CEILING:
        raise ToleranceError(f"measured noise floor {tol:.3g} is above the Kernel ceiling {C.TOL_CEILING} "
                             f"(source {max(per_source, key=per_source.__getitem__)}): not checkable by re-execution")
    return Calibration(reference=floor.reference, tolerance=tol, miss_rate=miss_rate, purpose=purpose,
                       per_source=per_source, runs=runs)


def resolve_tolerance(evidence_class: EvidenceClass, hand_set: float, calibration: Calibration | None) -> Resolved:
    """The tolerance a check of this class should use. A valid calibration REPLACES the hand-set value where the
    class allows (in either direction: a too-tight hand-set value fails honest re-runs, a too-loose one passes
    drift); exact classes use 0 (identical results) and take no tolerance at all."""
    if evidence_class in (EvidenceClass.FORMAL, EvidenceClass.COMPUTATIONAL):
        if hand_set != 0.0:
            return Resolved(None, "refused", (f"class {evidence_class.value!r} is exact: a tolerance is refused",))
        return Resolved(0.0, "exact", (f"class {evidence_class.value!r} is exact: no tolerance (calibration "
                                       "ignored)",) if calibration is not None else ())
    if evidence_class not in C.TOL_CALIBRATABLE_CLASSES:
        return Resolved(None, "refused", (f"class {evidence_class.value!r} is not re-run by the system",))
    if calibration is not None:
        return Resolved(calibration.tolerance, "calibrated", ())
    if not (math.isfinite(hand_set) and 0.0 <= hand_set <= C.TOL_CEILING):
        return Resolved(None, "refused", (f"hand-set tolerance {hand_set} outside [0, {C.TOL_CEILING}]",))
    return Resolved(hand_set, "hand_set", ())
