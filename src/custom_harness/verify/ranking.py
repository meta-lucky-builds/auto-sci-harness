"""Relative judgment (PLAN P5): K-way rankings -> pairwise wins -> Bradley-Terry strengths."""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations


def rankings_to_wins(rankings: list[list[str]]) -> dict[tuple[str, str], int]:
    """Each ranking is best-first. Returns wins[(a, b)] = times a ranked above b."""
    wins: dict[tuple[str, str], int] = defaultdict(int)
    for r in rankings:
        for i, j in combinations(range(len(r)), 2):
            wins[(r[i], r[j])] += 1
    return dict(wins)


def bradley_terry(items: list[str], wins: dict[tuple[str, str], int], iters: int = 200,
                  prior: float = 0.5) -> dict[str, float]:
    """MM algorithm (Hunter 2004) with a small symmetric prior so unbeaten items stay finite.
    Returns strengths normalised to sum to 1."""
    p = {i: 1.0 / len(items) for i in items}
    w = defaultdict(float)
    n = defaultdict(float)
    for a in items:
        for b in items:
            if a == b:
                continue
            w[(a, b)] = wins.get((a, b), 0) + prior
    for a, b in combinations(items, 2):
        n[(a, b)] = n[(b, a)] = w[(a, b)] + w[(b, a)]
    for _ in range(iters):
        new = {}
        for a in items:
            total_wins = sum(w[(a, b)] for b in items if b != a)
            denom = sum(n[(a, b)] / (p[a] + p[b]) for b in items if b != a)
            new[a] = total_wins / denom if denom else p[a]
        s = sum(new.values())
        p = {k: v / s for k, v in new.items()}
    return p
