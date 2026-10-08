"""Prediction ledger scoring: calibration, the Validated Discovery Score and forecasting skill (GOAL §4.5, §16.4).

Every pre-registration is a prediction: "with probability q, this claim will be certified on its held-out
targets". It resolves when a certificate for the claim is issued (outcome = certified or not; the latest
certificate wins), or EXPIRES as a miss when resolve_by passes with no certificate, so hiding failures by
never certifying them does not help.

VDS as an e-process (§16.4, fixes F2). p0 is FIXED AT REGISTRATION (the prereg entry's `fixed.p0`): an upper bound on
P(certified | no effect), the claim's registered level. The Kernel refuses q < p0. Each resolved prediction scores
    LR = q / p0                  if the claim was certified
       = (1 - q) / (1 - p0)      otherwise,         information gain = log2 LR.
If P(certified | past, global null) <= p0 and q >= p0, then E[LR | past] <= 1, so 2^VDS is a nonnegative
supermartingale under the global null "no registered claim has a real effect", and Ville's inequality gives
P(sup VDS >= log2(1/alpha)) <= alpha (`ville_threshold`). A miss never scores positive. A legacy prediction with q < p0
scores 0 either way (LR = 1). Assumption: K4's tests are not anti-conservative (they hold their level).

Skill is reported separately: log2 of the probability the system gave the observed outcome over the probability the
pinned reference forecaster gave it (`fixed.p_ref`, the Laplace base rate of certificates resolved before the
registration). VDS says whether the system produces real effects; skill whether it knows which claims will hold.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .ledger import Ledger

_CLAMP = 1e-6
# closed reasons for a ledger entry the scorer cannot read (fail closed PER CLAIM, never a crash for the whole store;
# studio-to-kstats-02): a certificate entry without a boolean `resolution.outcome` scores its claim as a MISS (no
# credit); a pre-registration whose terms are unreadable is reported unscored
INVALID_RESOLUTION = "invalid_resolution"
INVALID_PREREG = "invalid_prereg"


@dataclass(frozen=True)
class PredictionScore:
    claim_hash: str
    q: float  # declared probability of certification
    p0: float  # baseline probability, fixed at registration
    outcome: bool | None  # None = unresolved and not yet expired
    expired: bool
    info_gain_bits: float | None
    skill_bits: float | None = None  # vs the pinned reference forecaster
    invalid: str | None = None  # INVALID_RESOLUTION / INVALID_PREREG, else None


def _clamp(p: float) -> float:
    return min(1 - _CLAMP, max(_CLAMP, p))


def _registered_p0(body: dict[str, Any]) -> tuple[float, float]:
    """(p0, p_ref) frozen in the prereg entry. A pre-registration made before they were recorded gets the same terms
    from its registered claim alone: p0 = alpha / n_comparisons for data claims, else 0.5; p_ref = 0.5."""
    fixed = body.get("fixed")
    if isinstance(fixed, dict):
        return float(fixed["p0"]), float(fixed["p_ref"])
    claim = body["claim"]
    plan = claim.get("analysis_plan", {})
    if claim.get("evidence_class", "data") == "data":
        return float(plan.get("alpha", 0.05)) / max(1, int(plan.get("n_comparisons", 1))), 0.5
    return 0.5, 0.5


def outcome_of(body: dict[str, Any]) -> bool | None:
    """The certificate entry's outcome, or None when it has no boolean `resolution.outcome` (malformed)."""
    res = body.get("resolution")
    out = res.get("outcome") if isinstance(res, dict) else None
    return out if isinstance(out, bool) else None


def score_predictions(ledger: Ledger, now: float) -> list[PredictionScore]:
    latest: dict[str, bool | None] = {}  # claim hash -> outcome of its latest certificate (None = malformed entry)
    for e in ledger.entries("certificate"):
        if isinstance(e.body.get("claim_hash"), str):
            latest[e.body["claim_hash"]] = outcome_of(e.body)
    seen: set[str] = set()
    out: list[PredictionScore] = []
    for e in ledger.entries("prereg"):
        h = e.body.get("claim_hash")
        if not isinstance(h, str) or h in seen:
            continue
        seen.add(h)
        try:
            claim = e.body["claim"]
            q = _clamp(float(claim["effect"]["probability"]))
            p0, p_ref = (_clamp(x) for x in _registered_p0(e.body))
            resolve_by = float(claim["resolve_by"])
        except (KeyError, TypeError, ValueError):
            out.append(PredictionScore(h, 0.0, 0.0, None, False, None, None, INVALID_PREREG))
            continue
        resolved = h in latest
        expired = not resolved and now > resolve_by
        if not resolved and not expired:
            out.append(PredictionScore(h, q, p0, None, False, None))
            continue
        res = latest.get(h)
        invalid = INVALID_RESOLUTION if resolved and res is None else None
        outcome = bool(res)  # expired, or a malformed certificate entry: a miss (fail closed, never credit)
        q_lr = max(q, p0)  # q < p0 (only on legacy entries): LR = 1, never a positive score
        gain = math.log2(q_lr / p0) if outcome else math.log2((1 - q_lr) / (1 - p0))
        skill = math.log2(q / p_ref) if outcome else math.log2((1 - q) / (1 - p_ref))
        out.append(PredictionScore(h, q, p0, outcome, expired, gain, skill, invalid))
    return out


def vds(scores: list[PredictionScore]) -> float:
    return sum(s.info_gain_bits for s in scores if s.info_gain_bits is not None)


def skill(scores: list[PredictionScore]) -> float:
    return sum(s.skill_bits for s in scores if s.skill_bits is not None)


def ville_threshold(alpha: float) -> float:
    """VDS at or above this many bits rejects the global null at level alpha (Ville's inequality)."""
    return math.log2(1 / alpha)


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
