"""L4.4 Hypothesis diversity (GOAL §5 L4 stage 4): measured by behaviour, bounded by declared topic quotas, with
far-domain inspiration available as a prompt-side option that is never evidence.

- **Diversity = error correlation of out-of-fold dev predictions, not vendor count.** Two hypotheses whose dev
  errors are correlated above `duplicate_r` (GOAL: "SDE r>0.8") make the same mistakes: they are one behaviour,
  whichever model or vendor proposed them. `diversity_report` computes the pairwise Pearson correlation of the error
  vectors (classification: the 0/1 error indicator; regression: the signed residual), flags every pair above the
  bar and deduplicates greedily in the given order (pass the candidates best-first: the better one of a pair is
  kept). The `vendor` field is reported and deliberately ignored. An error vector with no variance (a candidate
  with no dev error, or every row wrong) has no correlation: such a pair is a duplicate only if the two error
  vectors are identical, otherwise the pair is reported as undefined and both are kept (stated, not hidden).
  Only DEV out-of-fold predictions are used; nothing here reads held-out data.
- **Topic quotas.** `TopicQuota` declares the topics and a cap per topic BEFORE generation. A hypothesis without a
  declared topic is dropped (fail closed: an undeclared topic would escape every cap), and hypotheses beyond a
  topic's cap are dropped in order. Each drop is recorded with its reason.
- **Distance-3 cross-domain inspiration (prompt-side option only).** `inspiration` picks, from a declared three-level
  domain taxonomy (field / discipline / area), an area whose distance from the task's area is exactly `distance`.
  Definition used here (a design choice, stated): the distance between two areas is the number of levels from the
  task's area up to their lowest common ancestor, so 1 = a sibling area, 2 = another discipline in the same field,
  3 = another field. The result is a prompt fragment marked `is_evidence=False` (a Literal: it cannot be set True).
  Nothing in this module or the Kernel path reads it: it can change WHICH hypotheses are proposed, never how any
  of them is judged (dedup, quotas, dev racing and the Kernel ignore it).

Not wired into the research loop yet (the hypothesize prompt does not ask for a topic; no diversity node): see the
mailbox post for the hooks it needs.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..verify import metrics as M
from .formulator import DEFAULTS, L4Config


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- error correlation
def error_vector(metric: str, labels: Sequence[str], preds: Sequence[str]) -> list[float]:
    """Per-row dev error: 0/1 mismatch for classification metrics, signed residual (pred - label) otherwise."""
    if len(labels) != len(preds):
        raise ValueError(f"{len(preds)} predictions for {len(labels)} dev labels")
    if metric in M.CLASSIFICATION:
        return [0.0 if p == y else 1.0 for p, y in zip(preds, labels, strict=True)]
    return [float(p) - float(y) for p, y in zip(preds, labels, strict=True)]


def error_correlation(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Pearson correlation of two error vectors; 1.0 for identical vectors; None when undefined (a constant
    vector, unless both are identical)."""
    if len(a) != len(b):
        raise ValueError("error vectors of different lengths")
    if list(a) == list(b):
        return 1.0
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sab = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    saa = sum((x - ma) ** 2 for x in a)
    sbb = sum((y - mb) ** 2 for y in b)
    if saa <= 0 or sbb <= 0:
        return None
    return max(-1.0, min(1.0, sab / math.sqrt(saa * sbb)))


class HypothesisRun(_Frozen):
    """One hypothesis's out-of-fold DEV predictions (row order = the dev labels' order)."""

    id: str
    preds: tuple[str, ...]
    topic: str | None = None
    vendor: str | None = None  # reported only: diversity is NOT measured by it


class DuplicateDrop(_Frozen):
    id: str
    duplicate_of: str
    r: float


class DiversityReport(_Frozen):
    ids: tuple[str, ...]
    r: dict[str, dict[str, float | None]]  # pairwise error correlation (None = undefined)
    flagged: tuple[tuple[str, str, float], ...]  # pairs with r > threshold
    undefined: tuple[tuple[str, str], ...]
    kept: tuple[str, ...]
    dropped: tuple[DuplicateDrop, ...]
    threshold: float
    mean_r: float | None  # mean defined off-diagonal correlation
    effective_n: float | None  # n / (1 + (n - 1) * max(0, mean_r)): independent-equivalent count
    vendors: int  # distinct declared vendors: reported, NOT used to measure diversity
    measure: str = "error correlation of out-of-fold dev predictions"


def diversity_report(metric: str, labels: Sequence[str], runs: Sequence[HypothesisRun], *,
                     threshold: float | None = None, config: L4Config = DEFAULTS) -> DiversityReport:
    """Pairwise dev-error correlation, near-duplicate flags (r > threshold) and a greedy dedup in the given order."""
    thr = config.duplicate_r if threshold is None else threshold
    ids = [h.id for h in runs]
    if len(set(ids)) != len(ids):
        raise ValueError("hypothesis ids must be unique")
    errs = {h.id: error_vector(metric, labels, h.preds) for h in runs}
    r: dict[str, dict[str, float | None]] = {i: {} for i in ids}
    flagged, undefined, vals = [], [], []
    for x in range(len(ids)):
        for y in range(x + 1, len(ids)):
            a, b = ids[x], ids[y]
            c = error_correlation(errs[a], errs[b])
            r[a][b] = r[b][a] = c
            if c is None:
                undefined.append((a, b))
                continue
            vals.append(c)
            if c > thr:
                flagged.append((a, b, c))
    kept: list[str] = []
    dropped: list[DuplicateDrop] = []
    for i in ids:
        dup = next(((k, c) for k in kept if (c := r[i][k]) is not None and c > thr), None)
        if dup is None:
            kept.append(i)
        else:
            dropped.append(DuplicateDrop(id=i, duplicate_of=dup[0], r=dup[1]))
    mean_r = sum(vals) / len(vals) if vals else None
    n = len(ids)
    eff = n / (1 + (n - 1) * max(0.0, mean_r)) if mean_r is not None and n else None
    return DiversityReport(ids=tuple(ids), r=r, flagged=tuple(flagged), undefined=tuple(undefined),
                           kept=tuple(kept), dropped=tuple(dropped), threshold=thr, mean_r=mean_r, effective_n=eff,
                           vendors=len({h.vendor for h in runs if h.vendor}))


# ---------------------------------------------------------------- topic quotas
class TopicQuota(_Frozen):
    """Declared before generation: the allowed topics, a default cap and per-topic caps."""

    topics: tuple[str, ...] = Field(min_length=1)
    cap: int = Field(default=DEFAULTS.topic_cap, ge=1)
    caps: dict[str, int] = Field(default_factory=dict)


def _norm(t: Any) -> str | None:
    return t.strip().lower() if isinstance(t, str) and t.strip() else None


def apply_topic_quota(hypotheses: Sequence[Mapping[str, Any]], quota: TopicQuota
                      ) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    """(kept, dropped) in the given order. Dropped: undeclared or missing topic, or beyond the topic's cap."""
    declared = {_norm(t) for t in quota.topics}
    unknown = [t for t in quota.caps if _norm(t) not in declared]
    if unknown:
        raise ValueError(f"caps for undeclared topics: {unknown}")
    caps = {_norm(t): c for t, c in quota.caps.items()}
    used: dict[str, int] = {}
    kept: list[Mapping[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for h in hypotheses:
        t = _norm(h.get("topic"))
        if t is None or t not in declared:
            dropped.append({"id": h.get("id"), "topic": h.get("topic"), "reason": "undeclared topic"})
            continue
        cap = caps.get(t, quota.cap)
        if used.get(t, 0) >= cap:
            dropped.append({"id": h.get("id"), "topic": h.get("topic"), "reason": f"over the topic cap ({cap})"})
            continue
        used[t] = used.get(t, 0) + 1
        kept.append(h)
    return kept, dropped


# ---------------------------------------------------------------- cross-domain inspiration (prompt-side only)
#: field -> discipline -> areas. Declared, small, editable by the owner; a task names its area.
DEFAULT_TAXONOMY: dict[str, dict[str, tuple[str, ...]]] = {
    "physical sciences": {"physics": ("statistical mechanics", "optics", "fluid dynamics"),
                          "chemistry": ("reaction kinetics", "crystallography")},
    "life sciences": {"biology": ("ecology", "population genetics", "immunology"),
                      "medicine": ("epidemiology", "pharmacology")},
    "engineering": {"control": ("feedback control", "signal processing"),
                    "operations": ("queueing", "scheduling")},
    "computing": {"machine learning": ("tabular learning", "ensemble methods", "representation learning"),
                  "algorithms": ("combinatorial optimisation", "streaming algorithms")},
    "social sciences": {"economics": ("auction theory", "econometrics"),
                        "psychology": ("perception", "decision making")},
}


def _path(area: str, taxonomy: Mapping[str, Mapping[str, Sequence[str]]]) -> tuple[str, str, str]:
    hits = [(f, d, a) for f, ds in taxonomy.items() for d, areas in ds.items() for a in areas if a == area]
    if len(hits) != 1:
        raise ValueError(f"area {area!r} appears {len(hits)} times in the taxonomy (must be exactly once)")
    return hits[0]


def domain_distance(a: str, b: str, taxonomy: Mapping[str, Mapping[str, Sequence[str]]] = DEFAULT_TAXONOMY) -> int:
    """Levels from area `a` up to the lowest common ancestor with `b`: 0 same, 1 sibling, 2 same field, 3 other."""
    pa, pb = _path(a, taxonomy), _path(b, taxonomy)
    if pa == pb:
        return 0
    if pa[:2] == pb[:2]:
        return 1
    if pa[0] == pb[0]:
        return 2
    return 3


class Inspiration(_Frozen):
    task_area: str
    source_area: str
    distance: int
    prompt: str
    is_evidence: Literal[False] = False


def inspiration(task_area: str, *, seed: int, distance: int = DEFAULTS.inspiration_distance,
                taxonomy: Mapping[str, Mapping[str, Sequence[str]]] = DEFAULT_TAXONOMY) -> Inspiration:
    """A prompt fragment naming one area at exactly `distance` from the task's area (deterministic in `seed`)."""
    _path(task_area, taxonomy)
    pool = sorted(a for ds in taxonomy.values() for areas in ds.values() for a in areas
                  if domain_distance(task_area, a, taxonomy) == distance)
    if not pool:
        raise ValueError(f"no area at distance {distance} from {task_area!r}")
    src = random.Random(f"inspiration:{task_area}:{distance}:{seed}").choice(pool)
    text = (f"Optional inspiration (NOT evidence; it cannot support any claim): think about how a mechanism or "
            f"method from {src} might transfer to this {task_area} problem. Any hypothesis it suggests is judged "
            f"only by dev cross-validation and the Kernel, like every other hypothesis.")
    return Inspiration(task_area=task_area, source_area=src, distance=distance, prompt=text)
