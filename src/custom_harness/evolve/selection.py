"""Evaluation allocation (racing, PLAN §6.4) and the deterministic acceptance gate (PLAN L6.5).

All comparisons are PAIRED: candidate and parent are evaluated on the same (task, seed) units,
so task difficulty cancels out.

A score of None means the unit was EXCLUDED (infrastructure failure, see errors.py). Such a unit is
dropped from BOTH arms of the pair, never scored as 0; the number of exclusions is reported and the
gate fails closed when too many units were excluded to trust the comparison (CR P0-1, P0-5).
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Callable, Hashable

Unit = tuple[str, int]  # (task_id, seed)
Score = float | None  # None = excluded (infra failure)


@dataclass
class PairedSample:
    """Paired differences candidate - parent over an explicit, aligned list of units."""

    units: list[Unit]
    diffs: list[float]
    excluded: list[Unit] = field(default_factory=list)

    @property
    def n_units(self) -> int:
        return len(self.units)

    @property
    def excluded_frac(self) -> float:
        return len(self.excluded) / self.n_units if self.n_units else 1.0


def paired_sample(units: list[Unit], candidate: Callable[[Unit], Score], parent: Callable[[Unit], Score]
                  ) -> PairedSample:
    """Unit-alignment check (CR P0-5, the Self-Harness denominator bug): both arms are evaluated on
    exactly the same unit list, units are unique, and an exclusion on either side drops the pair."""
    if len(set(units)) != len(units):
        raise ValueError("duplicate units in a paired comparison")
    diffs, excluded = [], []
    for u in units:
        c, p = candidate(u), parent(u)
        if c is None or p is None:
            excluded.append(u)
            continue
        diffs.append(c - p)
    return PairedSample(list(units), diffs, excluded)


def paired_ci(diffs: list[float], *, n_boot: int = 4000, alpha: float = 0.05, seed: int = 0
              ) -> tuple[float, float, float]:
    """Mean and percentile-bootstrap CI of paired differences. One unit -> CI is (-inf, inf)."""
    if not diffs:
        return 0.0, -math.inf, math.inf
    mean = statistics.fmean(diffs)
    if len(diffs) == 1:
        return mean, -math.inf, math.inf
    rng = random.Random(seed)
    boots = sorted(statistics.fmean(rng.choice(diffs) for _ in diffs) for _ in range(n_boot))
    return mean, boots[int(alpha / 2 * n_boot)], boots[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]


@dataclass
class RaceResult:
    survivors: list[Hashable]
    stats: dict[Hashable, dict]  # per candidate: mean_diff, lo, hi, n_units, eliminated_round
    evaluations: int  # candidate evaluations actually spent (excl. parent)
    evaluations_fixed_k: int  # what evaluating every candidate on every unit would have cost


def race(candidates: list[Hashable], units: list[Unit], evaluate: Callable[[Hashable, Unit], Score],
         parent_scores: dict[Unit, Score], *, rounds: int = 3, keep: int = 1) -> RaceResult:
    """Successive halving with a paired-CI elimination rule.

    After each round (a chunk of units), a candidate is dropped if its paired-difference CI upper
    bound is below 0 (clearly worse than parent); then only the better half by mean difference
    continues, never fewer than `keep`. Units excluded on either side are dropped from that pair.
    """
    alive = list(candidates)
    chunks = [units[i::rounds] for i in range(rounds)]
    if set(units) - set(parent_scores):
        raise ValueError("parent was not evaluated on every race unit")
    diffs: dict[Hashable, list[float]] = {c: [] for c in candidates}
    excluded: dict[Hashable, int] = {c: 0 for c in candidates}
    stats: dict[Hashable, dict] = {}
    spent = 0
    for r, chunk in enumerate(chunks):
        if not chunk:
            continue
        for c in alive:
            for u in chunk:
                score = evaluate(c, u)
                spent += 1
                if score is None or parent_scores[u] is None:
                    excluded[c] += 1
                    continue
                diffs[c].append(score - parent_scores[u])
        ranked = []
        for c in alive:
            mean, lo, hi = paired_ci(diffs[c], seed=r)
            stats[c] = {"mean_diff": mean, "lo": lo, "hi": hi, "n_units": len(diffs[c]),
                        "excluded": excluded[c], "eliminated_round": None}
            ranked.append((mean, c))
        ranked.sort(key=lambda t: -t[0])
        is_last = r == len(chunks) - 1
        # a candidate with no valid unit yet has CI (-inf, inf): it is never eliminated on no evidence,
        # and never wins on it either (mean 0 ranks it behind any real improvement)
        survivors = [c for _, c in ranked if stats[c]["hi"] >= 0 or is_last]
        if not is_last:
            survivors = survivors[:max(keep, math.ceil(len(survivors) / 2))]
        for c in alive:
            if c not in survivors:
                stats[c]["eliminated_round"] = r
        alive = survivors
        if not alive:
            break
    alive = sorted(alive, key=lambda c: -stats[c]["mean_diff"])[:keep]
    return RaceResult(alive, stats, spent, len(candidates) * len(units))


@dataclass
class GateDecision:
    accepted: bool
    reason: str
    evolve: dict = field(default_factory=dict)
    gate: dict = field(default_factory=dict)


def _as_sample(x: PairedSample | list[float]) -> PairedSample:
    if isinstance(x, PairedSample):
        return x
    return PairedSample([("_", i) for i in range(len(x))], list(x), [])


def gate(evolve: PairedSample | list[float], gate_: PairedSample | list[float], *,
         non_regression_eps: float = 0.0, require_evolve_ci: bool = True, max_excluded_frac: float = 0.25,
         min_units: int = 2) -> GateDecision:
    """Accept only if:
      0. alignment: each split has >= min_units valid paired units, and at most max_excluded_frac of
         its units were excluded for infrastructure failures (else the decision is not trustworthy:
         reject, fail closed; this also stops a candidate that breaks the environment from winning);
      1. evolve evidence: paired CI lower bound > 0 (if require_evolve_ci) — a real improvement;
      2. gate split (never shown to the proposer): mean diff > 0 and CI lower bound >= -eps — no regression.
    With few tasks these CIs are wide, so the gate is conservative by construction."""
    ev_s, gt_s = _as_sample(evolve), _as_sample(gate_)
    em, elo, ehi = paired_ci(ev_s.diffs, seed=1)
    gm, glo, ghi = paired_ci(gt_s.diffs, seed=2)
    ev = {"mean_diff": em, "lo": elo, "hi": ehi, "n": len(ev_s.diffs), "excluded": len(ev_s.excluded)}
    gt = {"mean_diff": gm, "lo": glo, "hi": ghi, "n": len(gt_s.diffs), "excluded": len(gt_s.excluded)}
    for name, smp in (("evolve", ev_s), ("gate", gt_s)):
        if smp.excluded_frac > max_excluded_frac:
            return GateDecision(False, f"{name}: {len(smp.excluded)}/{smp.n_units} units excluded for "
                                       f"infrastructure failures (> {max_excluded_frac:.0%}); decision unreliable",
                                ev, gt)
        if len(smp.diffs) < min_units:
            return GateDecision(False, f"{name}: only {len(smp.diffs)} valid paired units (< {min_units})", ev, gt)
    if require_evolve_ci and not elo > 0:
        return GateDecision(False, f"evolve improvement not significant (CI lo={elo:.4g})", ev, gt)
    if not gm > 0:
        return GateDecision(False, f"no improvement on gate split (mean={gm:.4g})", ev, gt)
    if not glo >= -non_regression_eps:
        return GateDecision(False, f"possible regression on gate split (CI lo={glo:.4g} < -{non_regression_eps})",
                            ev, gt)
    return GateDecision(True, "accepted: significant on evolve, non-regressing and positive on gate", ev, gt)
