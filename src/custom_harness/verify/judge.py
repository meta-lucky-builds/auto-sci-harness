"""L3 judge panel (T5) and its validation harness (GOAL §5 L3, §1.1, §10 invariant 5).

Design (AI-Scientist v1's panel with its bugs fixed; rubric decomposition from MM-Agent; BT with intervals):
- **>= 3 model families.** The panel refuses to exist with fewer distinct, known, non-mock families than
  `config.PANEL_MIN_FAMILIES`, or with any member (or the meta-reviewer) from the author's family (invariant 5).
  The refusal happens at construction, before any model call.
- **Both orders, rubric decomposition.** Every member judges every rubric criterion in BOTH presentation orders.
  A criterion whose two orders disagree counts as a tie (0.5) and is reported as order-dependent; an unparseable or
  replayed answer is an abstention (fail closed; invariant 21: cassette output is never evidence). Answers are
  strict JSON, never substring-matched (L3 "Never").
- **Ensemble.** Criterion-weighted score per member, averaged within a family, then across families (one family
  with many members cannot outvote the others). A verdict needs answers from >= PANEL_MIN_FAMILIES families.
- **Meta-review.** A separate reviewer reads the per-family, per-criterion breakdown. It can only DOWNGRADE (veto);
  an unparseable meta-review is a downgrade.
- **Validated before use.** `evaluate`/`rank` refuse unless a `PanelValidation` for this exact panel configuration
  (hash of members, rubric, meta-reviewer, prompt version) passed against HUMAN labels: agreement, Cohen's kappa and
  calibration (ECE) against thresholds in config.py (all UNCALIBRATED owner decisions).
- **Capped at T5, never grants.** A panel verdict is T5 evidence (`cap_tier`), and `panel_gate` can only keep or
  lower a policy decision, never raise it (GOAL §1.1: an LLM may only downgrade, never grant).

Status (honest): only one model family is configured on this machine, so `panel_status` reports the panel as
REFUSED ("needs owner: model families + human labels"). Everything here is tested with scripted fake backends only.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Any

from ..gateway.base import ChatRequest, Message
from ..policy import model_family
from ..tiers import PolicyDecision, Tier
from .ranking import BTFit, Choice, binom_two_sided_p, fit_bt, pair_choice

PROMPT_VERSION = "judge-panel-v1"
NEEDS_OWNER = "needs owner: model families + human labels"


class PanelRefused(RuntimeError):
    """The panel may not run (too few families, same family as the author, mock/unknown family)."""


class PanelNotValidated(PanelRefused):
    """No passing human-label validation for this exact panel configuration."""


@dataclass(frozen=True)
class Criterion:
    name: str
    question: str
    weight: float = 1.0


@dataclass(frozen=True)
class PanelMember:
    name: str
    backend: Any

    @property
    def family(self) -> str:
        return model_family(self.backend)

    def identity(self) -> dict[str, str]:
        b = self.backend
        return {"name": self.name, "backend": str(getattr(b, "name", "")), "provider": str(getattr(b, "provider", "")),
                "model": str(getattr(b, "model", "") or ""), "family": self.family}


def _family_ok(family: str) -> bool:
    return bool(family) and not family.startswith("unknown:") and family != "mock"


def panel_refusals(members: Sequence[PanelMember], meta: PanelMember | None, *, author_family: str,
                   min_families: int | None = None) -> list[str]:
    from ..config import PANEL_MIN_FAMILIES
    need = PANEL_MIN_FAMILIES if min_families is None else min_families
    reasons = []
    names = [m.name for m in members]
    if len(set(names)) != len(names):
        reasons.append("member names must be unique")
    for m in members:
        if not _family_ok(m.family):
            reasons.append(f"member {m.name!r} has an unknown or mock family ({m.family!r})")
        elif m.family == author_family:
            reasons.append(f"member {m.name!r} is from the author's family {author_family!r} (invariant 5)")
    fams = sorted({m.family for m in members if _family_ok(m.family) and m.family != author_family})
    if len(fams) < need:
        reasons.append(f"a judge panel needs >= {need} distinct model families other than the author's; "
                       f"it has {len(fams)} ({', '.join(fams) or 'none'}) ({NEEDS_OWNER})")
    if meta is None:
        reasons.append("a meta-reviewer is required")
    elif not _family_ok(meta.family) or meta.family == author_family:
        reasons.append(f"meta-reviewer family {meta.family!r} must be known, non-mock and differ from the "
                       f"author's {author_family!r} (invariant 5)")
    if not _family_ok(author_family):
        reasons.append(f"author family {author_family!r} must be known (separation cannot be checked otherwise)")
    return reasons


def panel_status(backends: Mapping[str, Any], *, author_role: str = "proposer") -> dict[str, Any]:
    """Honest status of the panel for the configured role -> backend map (no model call)."""
    from ..config import PANEL_MIN_FAMILIES
    author = backends.get(author_role) or backends.get("executor")
    af = model_family(author) if author is not None else "unknown:"
    fams = sorted({model_family(b) for b in backends.values()
                   if _family_ok(model_family(b)) and model_family(b) != af})
    ok = len(fams) >= PANEL_MIN_FAMILIES
    return {"author_family": af, "other_families": fams, "min_families": PANEL_MIN_FAMILIES,
            "status": "families available; still needs a passing human-label validation" if ok else
            f"REFUSED: {len(fams)} non-author famil{'y' if len(fams) == 1 else 'ies'} configured; {NEEDS_OWNER}"}


# ---------------------------------------------------------------- answers
def _parse_winner(text: str) -> str | None:
    """Strict: the whole response is a JSON object {"winner": "A"|"B"} (surrounding whitespace allowed)."""
    try:
        obj = json.loads(text.strip())
    except (ValueError, AttributeError):
        return None
    if not isinstance(obj, dict) or set(obj) - {"winner", "reason"}:
        return None
    w = obj.get("winner")
    return w if w in ("A", "B") else None


@dataclass
class PairJudgement:
    prob_a: float | None                       # ensemble P(a better); None = no quorum
    prefers: str | None                        # "a" / "b" / None (tie or no quorum)
    families_voting: int
    by_family: dict[str, float | None]
    by_criterion: dict[str, float | None]      # rubric decomposition (ensemble per criterion)
    order_dependent: int                       # member-criterion cells whose two orders disagreed
    abstentions: int                           # member-criterion cells with an invalid/replayed answer
    first_wins: int
    first_trials: int
    cells: list[dict[str, Any]] = field(default_factory=list)

    @property
    def position_p_value(self) -> float:
        return binom_two_sided_p(self.first_wins, self.first_trials, 0.5) if self.first_trials else 1.0


@dataclass(frozen=True)
class MetaReview:
    downgrade: bool
    concerns: tuple[str, ...]
    valid: bool


@dataclass(frozen=True)
class PanelVerdict:
    supported: bool
    judgement: PairJudgement
    meta: MetaReview
    reasons: tuple[str, ...]
    validation_hash: str

    @property
    def tier(self) -> Tier:
        return Tier.T5_LLM_JUDGE  # never stronger, whatever the panel says

    @property
    def grants(self) -> bool:
        return False  # an LLM verdict never grants (GOAL §1.1)


def cap_tier(tier: Tier) -> Tier:
    """Evidence resting on the panel can never be stronger than T5."""
    return Tier(max(int(tier), int(Tier.T5_LLM_JUDGE)))


_RANK = {PolicyDecision.DENY: 0, PolicyDecision.NEEDS_HUMAN: 1, PolicyDecision.ALLOW: 2}


def panel_gate(decision: PolicyDecision, verdict: PanelVerdict | None) -> PolicyDecision:
    """Combine a code-made policy decision with a panel verdict. A supporting verdict leaves the decision as it is;
    anything else (no verdict, not supported, downgraded) lowers ALLOW to NEEDS_HUMAN. Never raises a decision."""
    if verdict is not None and verdict.supported:
        return decision
    return decision if _RANK[decision] <= _RANK[PolicyDecision.NEEDS_HUMAN] else PolicyDecision.NEEDS_HUMAN


# ---------------------------------------------------------------- the panel
class JudgePanel:
    def __init__(self, members: Sequence[PanelMember], rubric: Sequence[Criterion], *, meta: PanelMember | None,
                 author_family: str, min_families: int | None = None) -> None:
        from ..config import PANEL_MIN_FAMILIES
        reasons = panel_refusals(members, meta, author_family=author_family, min_families=min_families)
        if not rubric:
            reasons.append("a rubric with >= 1 criterion is required")
        if any(not (math.isfinite(c.weight) and c.weight > 0) for c in rubric):
            reasons.append("criterion weights must be positive")
        if len({c.name for c in rubric}) != len(rubric):
            reasons.append("criterion names must be unique")
        if reasons:
            raise PanelRefused("; ".join(reasons))
        assert meta is not None
        self.members = tuple(members)
        self.rubric = tuple(rubric)
        self.meta = meta
        self.author_family = author_family
        self.min_families = PANEL_MIN_FAMILIES if min_families is None else min_families

    @property
    def config_hash(self) -> str:
        cfg = {"members": [m.identity() for m in self.members], "meta": self.meta.identity(),
               "rubric": [[c.name, c.question, c.weight] for c in self.rubric], "prompt": PROMPT_VERSION,
               "author_family": self.author_family, "min_families": self.min_families}
        return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()

    # ------------------------------------------------------------ raw judging (no validation check)
    def _ask(self, member: PanelMember, crit: Criterion, first: str, second: str) -> str | None:
        prompt = (f"Criterion: {crit.name}. {crit.question}\n\nOption A:\n{first}\n\nOption B:\n{second}\n\n"
                  'Judge ONLY this criterion. Return JSON {"winner": "A" or "B"}.')
        schema = {"type": "object", "properties": {"winner": {"type": "string", "enum": ["A", "B"]}},
                  "required": ["winner"]}
        resp = member.backend.complete(ChatRequest(messages=[Message(role="user", content=prompt)],
                                                   json_schema=schema))
        if getattr(resp, "replayed", False):
            return None  # invariant 21: replayed output is never evidence
        return _parse_winner(resp.text)

    def compare(self, a: str, b: str) -> PairJudgement:
        """Both orders x every criterion x every member. Used by `evaluate` and by the validation harness."""
        cells: list[dict[str, Any]] = []
        member_scores: dict[str, list[float]] = {}
        crit_scores: dict[str, list[float]] = {c.name: [] for c in self.rubric}
        order_dep = abst = fw = ft = 0
        total_w = sum(c.weight for c in self.rubric)
        for m in self.members:
            num = den = 0.0
            for c in self.rubric:
                ab = self._ask(m, c, a, b)  # "A" -> a wins
                ba = self._ask(m, c, b, a)  # "A" -> b wins
                for w in (ab, ba):
                    if w is not None:
                        ft += 1
                        fw += w == "A"
                if ab is None or ba is None:
                    abst += 1
                    cells.append({"member": m.name, "criterion": c.name, "score_a": None})
                    continue
                win_ab, win_ba = ab == "A", ba == "B"  # does a win in each order?
                s = 1.0 if (win_ab and win_ba) else 0.0 if not (win_ab or win_ba) else 0.5
                order_dep += win_ab != win_ba
                num += c.weight * s
                den += c.weight
                crit_scores[c.name].append(s)
                cells.append({"member": m.name, "criterion": c.name, "score_a": s})
            if den > 0 and den >= total_w / 2:  # a member counts only if it answered most of the rubric weight
                member_scores.setdefault(m.family, []).append(num / den)
        by_family: dict[str, float | None] = {}
        for f in sorted({m.family for m in self.members}):
            v = member_scores.get(f)
            by_family[f] = sum(v) / len(v) if v else None
        votes = [v for v in by_family.values() if v is not None]
        prob = sum(votes) / len(votes) if len(votes) >= self.min_families else None
        prefers = None if prob is None or prob == 0.5 else ("a" if prob > 0.5 else "b")
        by_crit = {k: (sum(v) / len(v) if v else None) for k, v in crit_scores.items()}
        return PairJudgement(prob_a=prob, prefers=prefers, families_voting=len(votes), by_family=by_family,
                             by_criterion=by_crit, order_dependent=order_dep, abstentions=abst, first_wins=fw,
                             first_trials=ft, cells=cells)

    def meta_review(self, a: str, b: str, j: PairJudgement) -> MetaReview:
        summary = {"prob_a": j.prob_a, "by_family": j.by_family, "by_criterion": j.by_criterion,
                   "order_dependent_cells": j.order_dependent, "abstentions": j.abstentions}
        prompt = (f"You are the meta-reviewer of a judge panel. Option A:\n{a}\n\nOption B:\n{b}\n\n"
                  f"Panel breakdown: {json.dumps(summary, sort_keys=True)}\n"
                  "List concrete concerns that should block trusting the panel's preference (inconsistency, "
                  "criteria that contradict the overall result, missing evidence). You cannot change the "
                  'preference, only veto it. Return JSON {"downgrade": true|false, "concerns": [strings]}.')
        schema = {"type": "object", "properties": {"downgrade": {"type": "boolean"},
                                                   "concerns": {"type": "array", "items": {"type": "string"}}},
                  "required": ["downgrade", "concerns"]}
        resp = self.meta.backend.complete(ChatRequest(messages=[Message(role="user", content=prompt)],
                                                      json_schema=schema))
        try:
            obj = None if getattr(resp, "replayed", False) else json.loads(resp.text.strip())
        except (ValueError, AttributeError):
            obj = None
        if (not isinstance(obj, dict) or not isinstance(obj.get("downgrade"), bool)
                or not isinstance(obj.get("concerns"), list) or not all(isinstance(x, str) for x in obj["concerns"])):
            return MetaReview(downgrade=True, concerns=("meta-review unparseable or replayed (fail closed)",),
                              valid=False)
        return MetaReview(downgrade=obj["downgrade"], concerns=tuple(obj["concerns"]), valid=True)

    # ------------------------------------------------------------ validated use
    def _require_validation(self, validation: PanelValidation | None) -> PanelValidation:
        if validation is None:
            raise PanelNotValidated(f"judge panel not validated against human labels ({NEEDS_OWNER})")
        if validation.config_hash != self.config_hash:
            raise PanelNotValidated("the validation was for a different panel configuration")
        if validation.label_source != "human":
            raise PanelNotValidated(f"validation labels are {validation.label_source!r}, not human ({NEEDS_OWNER})")
        if not validation.passed:
            raise PanelNotValidated("panel failed its human-label validation: " + "; ".join(validation.reasons))
        return validation

    def evaluate(self, candidate: str, reference: str, *, validation: PanelValidation | None) -> PanelVerdict:
        """Is `candidate` better than `reference`? Supported only if the validated panel prefers it with quorum
        and the meta-review does not veto. The verdict is T5 evidence and grants nothing."""
        v = self._require_validation(validation)
        j = self.compare(candidate, reference)
        meta = self.meta_review(candidate, reference, j)
        reasons = []
        if j.prob_a is None:
            reasons.append(f"no quorum: {j.families_voting} families answered (< {self.min_families})")
        elif j.prefers != "a":
            reasons.append(f"panel does not prefer the candidate (P={j.prob_a:.3f})")
        if meta.downgrade:
            reasons.append("meta-review downgrade: " + "; ".join(meta.concerns))
        return PanelVerdict(supported=not reasons, judgement=j, meta=meta, reasons=tuple(reasons),
                            validation_hash=v.config_hash)

    def rank(self, items: Mapping[str, str], *, validation: PanelValidation | None) -> BTFit:
        """BT with a position term and intervals over all pairs. Each member's criterion-weighted answer in each
        order is one choice. Members judging the same pair are not independent draws of the items' quality, so the
        intervals describe the panel's own noise, not uncertainty about the items' true merit."""
        self._require_validation(validation)
        ids = list(items)
        choices: list[Choice] = []
        for x, y in combinations(ids, 2):
            for first, second in ((x, y), (y, x)):
                for m in self.members:
                    score = 0.0
                    den = 0.0
                    for c in self.rubric:
                        w = self._ask(m, c, items[first], items[second])
                        if w is not None:
                            score += c.weight * (w == "A")
                            den += c.weight
                    if den and score / den != 0.5:
                        choices.append(pair_choice(first, second, first if score / den > 0.5 else second))
        if not choices:
            raise PanelRefused("the panel produced no valid judgements")
        return fit_bt(ids, choices, position_effect=True)


# ---------------------------------------------------------------- validation harness (human labels)
@dataclass(frozen=True)
class HumanLabel:
    id: str
    a: str
    b: str
    label: str  # "a" / "b" / "tie"


@dataclass(frozen=True)
class LabelSet:
    source: str          # "human" only when a person produced the labels; fixtures say otherwise
    labels: tuple[HumanLabel, ...]
    note: str = ""

    @property
    def content_hash(self) -> str:
        body = {"source": self.source, "labels": [[x.id, x.a, x.b, x.label] for x in self.labels]}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def load_labels(path: str | Path) -> LabelSet:
    """Strict loader: {"source": str, "note": str?, "labels": [{"id", "a", "b", "human": "a"|"b"|"tie"}]}."""
    obj = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(obj, dict) or not isinstance(obj.get("source"), str) or not isinstance(obj.get("labels"), list):
        raise ValueError("label file needs a string 'source' and a list 'labels'")
    out = []
    for r in obj["labels"]:
        if (not isinstance(r, dict) or not all(isinstance(r.get(k), str) for k in ("id", "a", "b", "human"))
                or r["human"] not in ("a", "b", "tie")):
            raise ValueError(f"malformed label row: {r!r}")
        out.append(HumanLabel(id=r["id"], a=r["a"], b=r["b"], label=r["human"]))
    if len({x.id for x in out}) != len(out):
        raise ValueError("label ids must be unique")
    return LabelSet(source=obj["source"], labels=tuple(out), note=str(obj.get("note", "")))


def cohen_kappa(x: Sequence[str], y: Sequence[str]) -> float:
    """Cohen's kappa for two raters over the same items. NaN when chance agreement is 1 (undefined)."""
    if len(x) != len(y) or not x:
        raise ValueError("kappa needs two equal-length, non-empty label lists")
    n = len(x)
    cats = sorted(set(x) | set(y))
    po = sum(a == b for a, b in zip(x, y, strict=True)) / n
    pe = sum((sum(a == c for a in x) / n) * (sum(b == c for b in y) / n) for c in cats)
    return float("nan") if pe >= 1.0 else (po - pe) / (1 - pe)


def calibration(probs: Sequence[float], outcomes: Sequence[int], *, bins: int | None = None) -> dict[str, Any]:
    """Expected calibration error over equal-width bins, plus the Brier score."""
    from ..config import PANEL_CALIBRATION_BINS
    nb = PANEL_CALIBRATION_BINS if bins is None else bins
    if len(probs) != len(outcomes) or not probs:
        raise ValueError("calibration needs equal-length, non-empty inputs")
    if any(not (0.0 <= p <= 1.0) for p in probs) or any(o not in (0, 1) for o in outcomes):
        raise ValueError("probabilities must be in [0, 1] and outcomes in {0, 1}")
    n = len(probs)
    table = []
    ece = 0.0
    for k in range(nb):
        lo, hi = k / nb, (k + 1) / nb
        idx = [i for i, p in enumerate(probs) if lo <= p < hi or (k == nb - 1 and p == 1.0)]
        if not idx:
            continue
        conf = sum(probs[i] for i in idx) / len(idx)
        acc = sum(outcomes[i] for i in idx) / len(idx)
        ece += len(idx) / n * abs(conf - acc)
        table.append({"lo": lo, "hi": hi, "n": len(idx), "mean_p": conf, "rate": acc})
    brier = sum((p - o) ** 2 for p, o in zip(probs, outcomes, strict=True)) / n
    return {"ece": ece, "brier": brier, "bins": table}


@dataclass(frozen=True)
class PanelValidation:
    config_hash: str
    label_source: str
    label_hash: str
    n: int
    ties_skipped: int
    agreement: float
    kappa: float
    ece: float
    brier: float
    abstain_rate: float
    passed: bool
    reasons: tuple[str, ...]


def validate_panel(panel: JudgePanel, labels: LabelSet, *, min_labels: int | None = None,
                   min_agreement: float | None = None, min_kappa: float | None = None,
                   max_ece: float | None = None) -> PanelValidation:
    """Run the panel on every human-labelled pair and measure agreement, Cohen's kappa (categories a / b /
    abstain: an abstention is a disagreement) and calibration of the ensemble probability. Ties in the human labels
    are skipped and counted. Passing requires labels whose source is "human"."""
    from ..config import PANEL_MAX_ECE, PANEL_MIN_AGREEMENT, PANEL_MIN_KAPPA, PANEL_MIN_LABELS
    min_labels = PANEL_MIN_LABELS if min_labels is None else min_labels
    min_agreement = PANEL_MIN_AGREEMENT if min_agreement is None else min_agreement
    min_kappa = PANEL_MIN_KAPPA if min_kappa is None else min_kappa
    max_ece = PANEL_MAX_ECE if max_ece is None else max_ece
    human, panel_l, probs, outs = [], [], [], []
    ties = 0
    for lab in labels.labels:
        if lab.label == "tie":
            ties += 1
            continue
        j = panel.compare(lab.a, lab.b)
        human.append(lab.label)
        panel_l.append(j.prefers or "abstain")
        probs.append(0.5 if j.prob_a is None else j.prob_a)
        outs.append(1 if lab.label == "a" else 0)
    n = len(human)
    reasons = []
    if labels.source != "human":
        reasons.append(f"labels are {labels.source!r}, not human")
    if n < min_labels:
        reasons.append(f"{n} labelled pairs < {min_labels}")
    if n == 0:
        return PanelValidation(panel.config_hash, labels.source, labels.content_hash, 0, ties, 0.0, float("nan"),
                               float("nan"), float("nan"), 1.0, False, tuple(reasons))
    agreement = sum(h == p for h, p in zip(human, panel_l, strict=True)) / n
    kappa = cohen_kappa(human, panel_l)
    cal = calibration(probs, outs)
    abstain = sum(p == "abstain" for p in panel_l) / n
    if not agreement >= min_agreement:
        reasons.append(f"agreement {agreement:.3f} < {min_agreement}")
    if not kappa >= min_kappa:  # NaN fails
        reasons.append(f"kappa {kappa:.3f} < {min_kappa}")
    if not cal["ece"] <= max_ece:
        reasons.append(f"ECE {cal['ece']:.3f} > {max_ece}")
    return PanelValidation(config_hash=panel.config_hash, label_source=labels.source, label_hash=labels.content_hash,
                           n=n, ties_skipped=ties, agreement=agreement, kappa=kappa, ece=cal["ece"],
                           brier=cal["brier"], abstain_rate=abstain, passed=not reasons, reasons=tuple(reasons))
