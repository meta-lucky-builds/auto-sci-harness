"""Relative judgment (PLAN P5, GOAL §1.1): K-way rankings / pairwise judgements -> Bradley-Terry strengths.

Two layers:
- `rankings_to_wins` + `bradley_terry`: the original point fit (rank-breaking + MM with a pseudo-count prior). Kept
  for callers that want only an order. It has NO intervals: rank-breaking treats the K(K-1)/2 pairs of one ranking as
  independent, which overstates the information (see `fit_bt`).
- `fit_bt`: a conditional-logit fit with Wald intervals. A pairwise judgement is a 2-way choice (Bradley-Terry); a
  K-way ranking is a sequence of choices (Plackett-Luce: stage s picks ranking[s] from ranking[s:]), which is BT's
  exact K-way generalisation, so the intervals use the right likelihood instead of rank-breaking. An optional
  position term gamma (log-odds bonus for the first-shown option) is fitted jointly in both-order pairwise mode.
  Stdlib only.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import combinations
from statistics import NormalDist


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


# ---------------------------------------------------------------- conditional-logit BT / Plackett-Luce with intervals
@dataclass(frozen=True)
class Choice:
    """One judged choice: `chosen` was preferred over every other member of `options`. `first` is the option shown
    first (pairwise mode), which carries the position term when the fit asks for one. `weight` counts repeats."""

    options: tuple[str, ...]
    chosen: str
    first: str | None = None
    weight: float = 1.0


def ranking_to_choices(ranking: Sequence[str]) -> list[Choice]:
    """Plackett-Luce decomposition of one best-first ranking (no position term: shuffled presentation)."""
    r = tuple(ranking)
    return [Choice(options=r[s:], chosen=r[s]) for s in range(len(r) - 1)]


def pair_choice(first: str, second: str, winner: str) -> Choice:
    return Choice(options=(first, second), chosen=winner, first=first)


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gauss-Jordan with partial pivoting (a is small and positive definite here)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-300:
            raise ArithmeticError("singular system")
        m[c], m[p] = m[p], m[c]
        piv = m[c][c]
        m[c] = [v / piv for v in m[c]]
        for r in range(n):
            if r != c and m[r][c] != 0.0:
                f = m[r][c]
                m[r] = [vr - f * vc for vr, vc in zip(m[r], m[c], strict=True)]
    return [m[i][n] for i in range(n)]


def _inverse(a: list[list[float]]) -> list[list[float]]:
    n = len(a)
    cols = [_solve(a, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def _strongly_connected(items: Sequence[str], edges: set[tuple[str, str]]) -> bool:
    """Ford's condition for a finite BT/PL MLE: the 'beat' digraph is strongly connected."""
    if len(items) <= 1:
        return True
    fwd: dict[str, set[str]] = {i: set() for i in items}
    bwd: dict[str, set[str]] = {i: set() for i in items}
    for a, b in edges:
        fwd[a].add(b)
        bwd[b].add(a)

    def reach(adj: dict[str, set[str]]) -> int:
        seen, stack = {items[0]}, [items[0]]
        while stack:
            for nxt in adj[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return len(seen)

    return reach(fwd) == len(items) == reach(bwd)


@dataclass(frozen=True)
class BTFit:
    """theta = centred log-strengths (sum 0; identifiable only up to a shift). `cov` is the Wald covariance of
    (theta..., gamma) under the ridge-penalised likelihood, projected onto sum(theta)=0. `mle_exists` is Ford's
    condition: False means some group of items never lost (or never won) against the rest, so the unpenalised MLE is
    infinite and the intervals are prior-dominated (report them as unreliable)."""

    items: tuple[str, ...]
    theta: dict[str, float]
    cov: tuple[tuple[float, ...], ...]
    position: float | None
    mle_exists: bool
    position_mle_exists: bool | None
    n_choices: float
    iterations: int

    @property
    def strengths(self) -> dict[str, float]:
        z = {i: math.exp(t) for i, t in self.theta.items()}
        s = sum(z.values())
        return {i: v / s for i, v in z.items()}

    def _idx(self, item: str) -> int:
        return self.items.index(item)

    def se(self, item: str) -> float:
        k = self._idx(item)
        return math.sqrt(max(self.cov[k][k], 0.0))

    def interval(self, item: str, level: float = 0.95) -> tuple[float, float]:
        z = NormalDist().inv_cdf(0.5 + level / 2)
        t, s = self.theta[item], self.se(item)
        return (t - z * s, t + z * s)

    def diff_interval(self, a: str, b: str, level: float = 0.95) -> tuple[float, float]:
        """Interval for theta_a - theta_b (the log-odds that a beats b)."""
        i, j = self._idx(a), self._idx(b)
        sd = math.sqrt(max(self.cov[i][i] + self.cov[j][j] - 2 * self.cov[i][j], 0.0))
        z = NormalDist().inv_cdf(0.5 + level / 2)
        d = self.theta[a] - self.theta[b]
        return (d - z * sd, d + z * sd)

    def position_interval(self, level: float = 0.95) -> tuple[float, float] | None:
        if self.position is None:
            return None
        k = len(self.items)
        z = NormalDist().inv_cdf(0.5 + level / 2)
        s = math.sqrt(max(self.cov[k][k], 0.0))
        return (self.position - z * s, self.position + z * s)

    def p_beats(self, a: str, b: str) -> float:
        return 1.0 / (1.0 + math.exp(self.theta[b] - self.theta[a]))

    def summary(self, level: float = 0.95) -> dict[str, object]:
        out: dict[str, object] = {
            "theta": dict(self.theta), "strengths": self.strengths,
            "ci": {i: list(self.interval(i, level)) for i in self.items}, "ci_level": level,
            "mle_exists": self.mle_exists, "n_choices": self.n_choices}
        if self.position is not None:
            out["position"] = self.position
            out["position_ci"] = list(self.position_interval(level) or ())
            out["position_mle_exists"] = self.position_mle_exists
        return out


def _aggregate(choices: Iterable[Choice], idx: dict[str, int], position_effect: bool) -> list[Choice]:
    agg: dict[tuple[tuple[str, ...], str, str | None], float] = defaultdict(float)
    for c in choices:
        if len(c.options) < 2 or len(set(c.options)) != len(c.options):
            raise ValueError(f"a choice needs >= 2 distinct options: {c.options!r}")
        if c.chosen not in c.options or any(o not in idx for o in c.options):
            raise ValueError(f"choice refers to an unknown item or a chosen item not among its options: {c!r}")
        if not (math.isfinite(c.weight) and c.weight > 0):
            raise ValueError(f"choice weight must be positive and finite: {c.weight!r}")
        first = c.first if position_effect else None
        if position_effect and first not in c.options:
            raise ValueError("a position-effect fit needs `first` on every choice")
        key = (tuple(sorted(c.options)) if first is None else c.options, c.chosen, first)
        agg[key] += c.weight
    return [Choice(options=o, chosen=ch, first=f, weight=w) for (o, ch, f), w in agg.items()]


def fit_bt(items: Sequence[str], choices: Iterable[Choice], *, position_effect: bool = False,
           ridge: float | None = None, max_iter: int = 200, tol: float = 1e-10) -> BTFit:
    """Penalised Newton fit of the conditional logit eta_t = theta_t + gamma*[t shown first]. The ridge (a Gaussian
    prior on theta, precision `ridge`) keeps the fit finite when Ford's condition fails; with it the MAP is exactly
    centred. Raises ValueError on malformed input (fail closed)."""
    from ..config import BT_RIDGE
    lam = BT_RIDGE if ridge is None else ridge
    items = tuple(items)
    if len(items) < 2 or len(set(items)) != len(items):
        raise ValueError("fit_bt needs >= 2 distinct items")
    if not (math.isfinite(lam) and lam > 0):
        raise ValueError("ridge must be positive")
    idx = {i: k for k, i in enumerate(items)}
    obs = _aggregate(choices, idx, position_effect)
    if not obs:
        raise ValueError("fit_bt needs at least one choice")
    n = len(items)
    p = n + (1 if position_effect else 0)
    # gamma is identified once the first-shown option both wins and loses; a token prior keeps it finite
    gam_ridge = 1e-6

    # each observation: (weight, chosen row, rows) with rows = sparse feature lists [(param, value), ...]
    designs: list[tuple[float, int, list[list[tuple[int, float]]]]] = []
    for c in obs:
        rows = []
        for o in c.options:
            feats = [(idx[o], 1.0)]
            if position_effect and o == c.first:
                feats.append((n, 1.0))
            rows.append(feats)
        designs.append((c.weight, c.options.index(c.chosen), rows))

    def penalised(b: list[float]) -> float:
        ll = 0.0
        for w, ch, rows in designs:
            etas = [sum(b[j] * v for j, v in f) for f in rows]
            mx = max(etas)
            ll += w * (etas[ch] - mx - math.log(sum(math.exp(e - mx) for e in etas)))
        pen = lam / 2 * sum(v * v for v in b[:n])
        if position_effect:
            pen += gam_ridge / 2 * b[n] ** 2
        return ll - pen

    def grad_hess(b: list[float]) -> tuple[list[float], list[list[float]]]:
        g = [0.0] * p
        h = [[0.0] * p for _ in range(p)]
        for w, ch, rows in designs:
            etas = [sum(b[j] * v for j, v in f) for f in rows]
            mx = max(etas)
            ex = [math.exp(e - mx) for e in etas]
            z = sum(ex)
            mean = [0.0] * p
            for e, f in zip(ex, rows, strict=True):
                pi = e / z
                for j, v in f:
                    mean[j] += pi * v
                    for k2, v2 in f:
                        h[j][k2] += w * pi * v * v2
            for j, v in rows[ch]:
                g[j] += w * v
            nz = [j for j in range(p) if mean[j] != 0.0]
            for j in nz:
                g[j] -= w * mean[j]
                for k2 in nz:
                    h[j][k2] -= w * mean[j] * mean[k2]
        for j in range(n):
            g[j] -= lam * b[j]
            h[j][j] += lam
        if position_effect:
            g[n] -= gam_ridge * b[n]
            h[n][n] += gam_ridge
        return g, h

    beta = [0.0] * p
    cur = penalised(beta)
    iters = 0
    for iters in range(1, max_iter + 1):  # noqa: B007
        g, h = grad_hess(beta)
        step = _solve(h, g)
        t = 1.0
        while True:
            cand = [bv + t * sv for bv, sv in zip(beta, step, strict=True)]
            val = penalised(cand)
            if val >= cur - 1e-12 or t < 1e-8:
                break
            t /= 2
        beta, cur = cand, val
        if max(abs(t * s) for s in step) < tol:
            break
    _, h = grad_hess(beta)
    cov = _inverse(h)
    # project theta onto sum 0 (the identifiable contrasts): C' = P C P', P = I - 11'/n on the theta block
    proj = [[(1.0 if i == j else 0.0) - (1.0 / n if i < n and j < n else 0.0) for j in range(p)] for i in range(p)]
    pc = [[sum(proj[i][k] * cov[k][j] for k in range(p)) for j in range(p)] for i in range(p)]
    cov_p = [[sum(pc[i][k] * proj[j][k] for k in range(p)) for j in range(p)] for i in range(p)]
    mean_t = sum(beta[:n]) / n
    theta = {items[k]: beta[k] - mean_t for k in range(n)}
    edges = {(c.chosen, o) for c in obs for o in c.options if o != c.chosen}
    pos_exists = None
    if position_effect:
        pos_exists = any(c.chosen == c.first for c in obs) and any(c.chosen != c.first for c in obs)
    return BTFit(items=items, theta=theta, cov=tuple(tuple(r) for r in cov_p),
                 position=beta[n] if position_effect else None,
                 mle_exists=_strongly_connected(items, edges), position_mle_exists=pos_exists,
                 n_choices=sum(c.weight for c in obs), iterations=iters)


# ---------------------------------------------------------------- exact binomial test + Wilson interval
def binom_two_sided_p(k: int, n: int, p: float) -> float:
    """Exact two-sided binomial p-value: P(pmf(X) <= pmf(k)), X ~ Bin(n, p)."""
    if n <= 0:
        return 1.0
    if not 0 <= k <= n or not 0 < p < 1:
        raise ValueError("need 0 <= k <= n and 0 < p < 1")

    def logpmf(x: int) -> float:
        return (math.lgamma(n + 1) - math.lgamma(x + 1) - math.lgamma(n - x + 1)
                + x * math.log(p) + (n - x) * math.log1p(-p))

    lk = logpmf(k)
    return min(1.0, sum(math.exp(lp) for lp in map(logpmf, range(n + 1)) if lp <= lk + 1e-9))


def wilson_interval(k: int, n: int, level: float = 0.95) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    z = NormalDist().inv_cdf(0.5 + level / 2)
    ph = k / n
    den = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / den
    hw = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - hw), min(1.0, c + hw))
