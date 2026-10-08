"""Value-of-information evaluation allocation for the race (GOAL §5 L1, §8 novel item 7; MDA's expected-information
idea, here as a knowledge gradient).

The race only SELECTS which candidate goes on to the acceptance rule; it decides nothing. This module replaces the
race's ORDER and ALLOCATION of evaluations, never the acceptance rule: the selected candidate is re-measured on fresh
seeds and gated exactly as before (evolve/loop.py: paired Student-t CI on the fresh evolve units, the never-shown
gate split, LOFO/LODO, cost and exclusion checks are all untouched). Because the acceptance evidence is fresh, any
selection rule leaves the false-accept guarantee of that rule as it is; what VOI can change is which candidate is
sent (power) and how many race evaluations are spent.

Policy (deterministic; no random choice is made, so no `commitment.draw` is involved):
- every candidate walks the race's unit order (the round-robin chunks of `evolve/selection.race`), so candidates are
  compared on the same units (paired), and units excluded for infrastructure failures (None on either arm) are
  dropped from the pair and never scored, as in the race;
- each candidate first gets `n_init` units; then, one evaluation at a time, the candidate with the largest knowledge
  gradient for the decision "which arm is best, the parent (difference 0) included" is evaluated on its next unit:
      KG_c = s_c~ * f(-|m_c - m*_c| / s_c~),  f(z) = z * Phi(z) + phi(z),
  where m_c is the mean paired difference so far, m*_c = max(0, best other candidate's mean), and
  s_c~ = sqrt(s^2 / n_c - s^2 / (n_c + 1)) is how much one more unit moves the estimate (s = pooled within-candidate
  sd of the differences, shrunk towards `prior_sd` with `prior_df` pseudo degrees of freedom). Ties: lowest index;
- it stops when the largest KG is below `stop_frac` * s, when no candidate has units left, or at `max_evals`;
- the winner is the candidate with the highest mean difference (ties: lowest index) among those with >= 1 valid unit.

Inputs: only the race's own evaluations (evolve-split scores the proposer may see anyway) and the parent's scores on
the same units. There is no parameter through which gate or sealed values could reach it (GOAL §16.5.3: held-out
results reach the evolver only as embargoed verdict bits; this policy uses none).

`n_init`, `prior_sd`, `prior_df` and `stop_frac` are UNCALIBRATED (simulation defaults; scripts/sim_allocation.py).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Hashable
from dataclasses import dataclass

from ..evolve.selection import RaceResult, Score, Unit, paired_ci


@dataclass(frozen=True)
class VoiConfig:
    n_init: int = 2  # UNCALIBRATED: units per candidate before the knowledge gradient is used
    prior_sd: float = 0.5  # UNCALIBRATED: sd of one paired difference before any data (objective units)
    prior_df: float = 2.0  # UNCALIBRATED: weight of prior_sd, in pseudo degrees of freedom
    stop_frac: float = 0.01  # UNCALIBRATED: stop when the best KG < stop_frac * pooled sd
    max_evals: int | None = None  # hard cap on candidate evaluations (None = no cap beyond the units)


def _phi(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)


def _Phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


def knowledge_gradient(m: float, best_other: float, s: float, n: int) -> float:
    """Expected improvement of the best posterior mean from one more unit of this candidate (Gaussian KG)."""
    if s <= 0:
        return 0.0
    if n <= 0:
        return math.inf  # no valid unit yet: its value is unknown
    st = math.sqrt(s * s / n - s * s / (n + 1))
    if st <= 0:
        return 0.0
    z = -abs(m - best_other) / st
    return st * (z * _Phi(z) + _phi(z))


def race_order(units: list[Unit], rounds: int) -> list[Unit]:
    """The race's unit order: chunk r = units[r::rounds], chunks in order."""
    return [u for r in range(rounds) for u in units[r::rounds]]


def voi_race(candidates: list[Hashable], units: list[Unit], evaluate: Callable[[Hashable, Unit], Score],
             parent_scores: dict[Unit, Score], *, rounds: int = 3, keep: int = 1,
             cfg: VoiConfig | None = None) -> RaceResult:
    """Drop-in for evolve/selection.race (same inputs, same RaceResult shape): `survivors` = the selected
    candidate(s), `evaluations` = candidate evaluations spent. Stats per candidate as the race reports them (Student-t
    paired CI over its valid units); `eliminated_round` is None (VOI eliminates nobody, it stops sampling)."""
    cfg = cfg or VoiConfig()
    if set(units) - set(parent_scores):
        raise ValueError("parent was not evaluated on every race unit")
    if cfg.n_init < 1 or cfg.prior_sd <= 0 or cfg.prior_df < 0 or cfg.stop_frac < 0:
        raise ValueError("invalid VoiConfig")
    order = race_order(list(units), max(1, rounds))
    pos = {c: 0 for c in candidates}
    diffs: dict[Hashable, list[float]] = {c: [] for c in candidates}
    excluded = {c: 0 for c in candidates}
    spent = 0
    cap = cfg.max_evals if cfg.max_evals is not None else len(candidates) * len(order)

    def step(c: Hashable) -> None:
        nonlocal spent
        u = order[pos[c]]
        pos[c] += 1
        spent += 1
        score, base = evaluate(c, u), parent_scores[u]
        if score is None or base is None:
            excluded[c] += 1
        else:
            diffs[c].append(score - base)

    def pooled_sd() -> float:
        ss, df = cfg.prior_sd ** 2 * cfg.prior_df, cfg.prior_df
        for d in diffs.values():
            if len(d) >= 2:
                mu = sum(d) / len(d)
                ss += sum((x - mu) ** 2 for x in d)
                df += len(d) - 1
        return math.sqrt(ss / df) if df > 0 else cfg.prior_sd

    for c in candidates:  # initial allocation, in candidate order
        while len(diffs[c]) < cfg.n_init and pos[c] < len(order) and spent < cap:
            step(c)
    while spent < cap:
        s = pooled_sd()
        means = {c: (sum(d) / len(d) if d else 0.0) for c, d in diffs.items()}
        best_kg, pick = -1.0, None
        for c in candidates:
            if pos[c] >= len(order):
                continue
            others = [means[o] for o in candidates if o != c and diffs[o]]
            kg = knowledge_gradient(means[c], max([0.0, *others]), s, len(diffs[c]))
            if kg > best_kg:
                best_kg, pick = kg, c
        if pick is None or best_kg < cfg.stop_frac * s:
            break
        step(pick)
    stats: dict[Hashable, dict] = {}
    for c in candidates:
        mean, lo, hi = paired_ci(diffs[c])
        stats[c] = {"mean_diff": mean, "lo": lo, "hi": hi, "n_units": len(diffs[c]), "excluded": excluded[c],
                    "eliminated_round": None}
    ranked = sorted((c for c in candidates if diffs[c]), key=lambda c: -stats[c]["mean_diff"])
    return RaceResult(ranked[:keep], stats, spent, len(candidates) * len(units))
