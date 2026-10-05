"""Evaluation allocation (racing, PLAN §6.4) and the deterministic acceptance gate (PLAN L6.5).

All comparisons are PAIRED: candidate and parent are evaluated on the same (task, seed) units,
so task difficulty cancels out.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Callable, Hashable

Unit = tuple[str, int]  # (task_id, seed)


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


def race(candidates: list[Hashable], units: list[Unit], evaluate: Callable[[Hashable, Unit], float],
         parent_scores: dict[Unit, float], *, rounds: int = 3, keep: int = 1) -> RaceResult:
    """Successive halving with a paired-CI elimination rule.

    After each round (a chunk of units), a candidate is dropped if its paired-difference CI upper
    bound is below 0 (clearly worse than parent); then only the better half by mean difference
    continues, never fewer than `keep`.
    """
    alive = list(candidates)
    chunks = [units[i::rounds] for i in range(rounds)]
    diffs: dict[Hashable, list[float]] = {c: [] for c in candidates}
    stats: dict[Hashable, dict] = {}
    spent = 0
    for r, chunk in enumerate(chunks):
        if not chunk:
            continue
        for c in alive:
            for u in chunk:
                diffs[c].append(evaluate(c, u) - parent_scores[u])
                spent += 1
        ranked = []
        for c in alive:
            mean, lo, hi = paired_ci(diffs[c], seed=r)
            stats[c] = {"mean_diff": mean, "lo": lo, "hi": hi, "n_units": len(diffs[c]), "eliminated_round": None}
            ranked.append((mean, c))
        ranked.sort(key=lambda t: -t[0])
        is_last = r == len(chunks) - 1
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


def gate(evolve_diffs: list[float], gate_diffs: list[float], *, non_regression_eps: float = 0.0,
         require_evolve_ci: bool = True) -> GateDecision:
    """Accept only if:
      1. evolve split: paired CI lower bound > 0 (if require_evolve_ci) — a real improvement where we searched;
      2. gate split (never shown to the proposer): mean diff > 0 and CI lower bound >= -eps — no regression.
    With few tasks these CIs are wide, so the gate is conservative by construction."""
    em, elo, ehi = paired_ci(evolve_diffs, seed=1)
    gm, glo, ghi = paired_ci(gate_diffs, seed=2)
    ev = {"mean_diff": em, "lo": elo, "hi": ehi, "n": len(evolve_diffs)}
    gt = {"mean_diff": gm, "lo": glo, "hi": ghi, "n": len(gate_diffs)}
    if require_evolve_ci and not elo > 0:
        return GateDecision(False, f"evolve improvement not significant (CI lo={elo:.4g})", ev, gt)
    if not gm > 0:
        return GateDecision(False, f"no improvement on gate split (mean={gm:.4g})", ev, gt)
    if not glo >= -non_regression_eps:
        return GateDecision(False, f"possible regression on gate split (CI lo={glo:.4g} < -{non_regression_eps})",
                            ev, gt)
    return GateDecision(True, "accepted: significant on evolve, non-regressing and positive on gate", ev, gt)
