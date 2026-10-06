"""Prediction ledger scoring: calibration and the Validated Discovery Score (GOAL §4.5).

Every pre-registration is a prediction: "with probability q, this claim will be certified on its held-out
targets". It resolves when a certificate for the claim is issued (outcome = certified or not; the latest
certificate wins), or EXPIRES as a miss when resolve_by passes with no certificate, so hiding failures by
never certifying them does not help.

Score (log scoring rule, relative to a baseline probability p0):
    information gain = log2(q / p0)            if the claim was certified
                     = log2((1 - q) / (1 - p0)) otherwise
p0 = the no-effect probability measured by K7 at resolution (or the declared prior, or 0.5 when neither
exists). Confident misses cost more than cautious ones; safe claims (p0 high) earn little.
VDS = sum of information gains over resolved and expired predictions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .ledger import Ledger

_CLAMP = 1e-6


@dataclass(frozen=True)
class PredictionScore:
    claim_hash: str
    q: float  # declared probability of certification
    p0: float  # baseline probability
    outcome: bool | None  # None = unresolved and not yet expired
    expired: bool
    info_gain_bits: float | None


def _clamp(p: float) -> float:
    return min(1 - _CLAMP, max(_CLAMP, p))


def score_predictions(ledger: Ledger, now: float) -> list[PredictionScore]:
    latest: dict[str, dict[str, Any]] = {}
    for e in ledger.entries("certificate"):
        latest[e.body["claim_hash"]] = e.body["resolution"]
    seen: set[str] = set()
    out: list[PredictionScore] = []
    for e in ledger.entries("prereg"):
        h = e.body["claim_hash"]
        if h in seen:
            continue
        seen.add(h)
        claim = e.body["claim"]
        q = _clamp(float(claim["effect"]["probability"]))
        prior = claim.get("risk", {}).get("prior_probability")
        res = latest.get(h)
        expired = res is None and now > float(claim["resolve_by"])
        if res is None and not expired:
            out.append(PredictionScore(h, q, _clamp(prior if prior is not None else 0.5), None, False, None))
            continue
        outcome = bool(res["outcome"]) if res is not None else False
        p_null = res.get("p_null") if res is not None else None
        p0 = _clamp(p_null if p_null is not None else (prior if prior is not None else 0.5))
        gain = math.log2(q / p0) if outcome else math.log2((1 - q) / (1 - p0))
        out.append(PredictionScore(h, q, p0, outcome, expired, gain))
    return out


def vds(scores: list[PredictionScore]) -> float:
    return sum(s.info_gain_bits for s in scores if s.info_gain_bits is not None)


def calibration(scores: list[PredictionScore], bins: int = 5) -> dict[str, object]:
    """Brier score and a reliability table over scored predictions."""
    scored = [s for s in scores if s.outcome is not None]
    if not scored:
        return {"n": 0, "brier": None, "bins": []}
    brier = sum((s.q - float(bool(s.outcome))) ** 2 for s in scored) / len(scored)
    table = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        members = [s for s in scored if lo <= s.q < hi or (b == bins - 1 and s.q == hi)]
        if members:
            table.append({"q_range": [lo, hi], "n": len(members),
                          "mean_q": sum(s.q for s in members) / len(members),
                          "hit_rate": sum(bool(s.outcome) for s in members) / len(members)})
    return {"n": len(scored), "brier": brier, "bins": table}
