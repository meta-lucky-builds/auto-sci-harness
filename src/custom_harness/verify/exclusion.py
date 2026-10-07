"""Exclusion accounting for paired comparisons (STATUS §D: "a candidate-only infra exclusion is not counted against
the candidate").

A unit whose candidate run OR parent run was an infrastructure failure is dropped from both arms (errors.py, the
gate in evolve/selection.py), and the gate fails closed above a fixed exclusion fraction. Below that cap, a candidate
that could turn its own bad units into "infrastructure" failures would hide them. This module adds the missing
accounting WITHOUT changing the statistics: count exclusions per arm and test whether the candidate-only losses
exceed what outages that do not depend on the arm explain.

Rule. Among the m units lost on exactly one arm, random outages make the candidate-only count X ~ Binomial(m, 1/2)
(both arms run the same units under the same environment). If P(X >= candidate_only) <= EXCLUSION_ALPHA (one-sided
exact sign test) the exclusions are asymmetric and the comparison must fail closed (the candidate is not accepted).
Being exact and discrete, the rule fires on symmetric random outages with probability <= EXCLUSION_ALPHA; with
alpha = 0.01 it needs at least 7 candidate-only losses (and none on the parent) to fire.

Assumption (stated): equal exposure. If the parent's runs are cached from an earlier time window and outages are
clustered in time, one-arm losses are not exchangeable; the rule then errs either way with unknown size.
Wired (2026-10-07): evolve/selection.py (paired_sample per-arm counts, exclusion_check) + evolve/loop.py (fail
closed, recorded as `exclusions`, rejected_on "exclusion_asymmetry"); tests/test_exclusion_asymmetry.py.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Hashable
from dataclasses import dataclass

EXCLUSION_ALPHA = 0.01


@dataclass(frozen=True)
class ExclusionCounts:
    candidate_only: int
    parent_only: int
    both: int


def count_exclusions(units: list[Hashable], candidate: Callable[[Hashable], float | None],
                     parent: Callable[[Hashable], float | None]) -> ExclusionCounts:
    """Per-arm exclusion counts over aligned units (None = excluded for an infrastructure failure)."""
    c_only = p_only = both = 0
    for u in units:
        c, p = candidate(u) is None, parent(u) is None
        c_only += c and not p
        p_only += p and not c
        both += c and p
    return ExclusionCounts(c_only, p_only, both)


def sign_test_p(candidate_only: int, parent_only: int) -> float:
    """P(X >= candidate_only) for X ~ Binomial(candidate_only + parent_only, 1/2)."""
    m = candidate_only + parent_only
    if m == 0:
        return 1.0
    return sum(math.comb(m, k) for k in range(candidate_only, m + 1)) / 2 ** m


def exclusion_asymmetry(candidate_only: int, parent_only: int, *, alpha: float = EXCLUSION_ALPHA) -> str | None:
    """The fail-closed reason when exclusions fall on the candidate beyond chance, else None."""
    p = sign_test_p(candidate_only, parent_only)
    if p <= alpha:
        return (f"{candidate_only} unit(s) excluded on the candidate only vs {parent_only} on the parent only "
                f"(sign test p={p:.3g} <= {alpha}): exclusions asymmetric beyond random outages (fail closed)")
    return None
