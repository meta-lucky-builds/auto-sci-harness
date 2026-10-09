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

- **Literature-mined mechanisms** (`mine_mechanisms`): verbatim sentences of papers THIS run retrieved (offline
  snapshot), each citing retrieval-log ids; prompt-side only (`is_evidence=False`).
- **Admission** (`admit`): the front declared before generation is enforced in code. A hypothesis citing an id that
  is not in the run's retrieval log, with an undeclared topic, or over its topic's cap is REJECTED (removed from the
  queue, reason recorded); a set with too few hypotheses that use the forced distance-3 inspiration or a retrieved
  mechanism is rejected as a whole (AdmissionRefused).

Wired into research_v2 (research/front.py): `research.hypothesis_front` (off by default, spec flag), admission in
`research.admit_rank`, and `research.diversity_select` (error-correlation dedup of executed candidates, feeding the
ensemble's member choice). None of them makes a model call.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from collections.abc import Collection, Mapping, Sequence
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


# ---------------------------------------------------------------- literature-mined mechanisms (prompt-side, cited)
#: Declared cue lexicon for mechanism sentences (UNCALIBRATED, owner decision GOAL §13.8): a sentence of a retrieved
#: paper's stored abstract containing one of these (case-insensitive substring) is shown VERBATIM as a mechanism.
MECHANISM_CUES: tuple[str, ...] = ("mechanism", "because", "due to", "driven by", "works by", "explained by",
                                   "we propose", "we introduce", "improves")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


class Mechanism(_Frozen):
    """A verbatim sentence from a paper this run RETRIEVED. `cites` are retrieval-log ids (doi:/arxiv:/pmid:). It can
    change which hypotheses are proposed; it is never evidence for any claim."""

    id: str
    cites: tuple[str, ...]
    title: str
    quote: str
    is_evidence: Literal[False] = False


def mine_mechanisms(records: Sequence[Mapping[str, Any]], retrieved_ids: Collection[str]) -> list[Mechanism]:
    """Mechanism sentences of retrieved records ({ids, title, abstract}); a record none of whose ids is in the
    retrieval log is skipped. Deterministic and order-independent (sorted by id)."""
    out: dict[str, Mechanism] = {}
    for rec in records:
        ids = tuple(sorted(i for i in rec.get("ids") or () if i in retrieved_ids))
        text = rec.get("abstract")
        if not ids or not isinstance(text, str):
            continue
        for sent in (s.strip() for s in _SENTENCE.split(text)):
            if any(c in sent.lower() for c in MECHANISM_CUES):
                mid = "mech-" + hashlib.sha256(f"{ids}|{sent}".encode()).hexdigest()[:12]
                out[mid] = Mechanism(id=mid, cites=ids, title=str(rec.get("title") or ""), quote=sent)
    return [out[k] for k in sorted(out)]


# ---------------------------------------------------------------- admission: quotas enforced in code
class AdmissionRefused(ValueError):
    """The hypothesis SET violates a quota (too few inspired or mechanism-citing hypotheses, or nothing admissible):
    the set is rejected, never just logged."""


def _cites(h: Mapping[str, Any]) -> list[str] | None:
    raw = h.get("mechanism_cites")
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        return None
    return [x.strip() for x in raw]


def admit(hypotheses: Sequence[Mapping[str, Any]], front: Mapping[str, Any], retrieved_ids: Collection[str]
          ) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    """Enforce the hypothesis front declared BEFORE generation (`front`, written by research.hypothesis_front).

    Per hypothesis, in the given order (a rejected hypothesis is removed from the queue, with its reason):
      1. `mechanism_cites`, if given, must be a list of ids that ALL appear in this run's retrieval log (GOAL L4.3
         provenance; the same rule K0/I3 applies to citations); a hypothesis citing anything else is rejected;
      2. its `topic` must be declared, and the topic's cap not yet reached (`apply_topic_quota`).
    For the set (raises AdmissionRefused, so the whole set is rejected):
      3. at least `min_inspired` admitted hypotheses set `inspired_by` to the forced distance-3 source area;
      4. at least `min_mechanisms` admitted hypotheses cite the paper of a SHOWN mechanism (0 when none was mined:
         the waiver is recorded);
      5. at least one hypothesis is admitted."""
    quota = TopicQuota.model_validate(front["quota"])
    rejected: list[dict[str, Any]] = []
    cited_ok: list[Mapping[str, Any]] = []
    retrieved = set(retrieved_ids)
    for h in hypotheses:
        c = _cites(h)
        if c is None:
            rejected.append({"id": h.get("id"), "reason": "mechanism_cites must be a list of id strings"})
            continue
        missing = sorted(set(c) - retrieved)
        if missing:
            rejected.append({"id": h.get("id"), "reason": f"cites id(s) not in this run's retrieval log: {missing}"})
            continue
        cited_ok.append(h)
    kept, dropped = apply_topic_quota(cited_ok, quota)
    rejected.extend(dropped)
    source = _norm((front.get("inspiration") or {}).get("source_area"))
    n_inspired = sum(1 for h in kept if source is not None and _norm(h.get("inspired_by")) == source)
    # a hypothesis counts toward the mechanism quota only if it cites a SHOWN mechanism's paper (citing another
    # retrieved paper is allowed, but is not "using a literature-mined mechanism")
    shown = {i for m in front.get("mechanisms") or () for i in m.get("cites") or ()}
    n_mech = sum(1 for h in kept if set(_cites(h) or ()) & shown)
    min_inspired = int(front.get("min_inspired", 0))
    min_mech = int(front.get("min_mechanisms", 0))
    record = {"enabled": True, "admitted": [h.get("id") for h in kept], "rejected": rejected,
              "inspired": n_inspired, "min_inspired": min_inspired, "mechanism_citing": n_mech,
              "min_mechanisms": min_mech, "retrieved_ids": len(retrieved),
              "mechanisms_waived": bool(front.get("mechanisms_waived"))}
    problems = []
    if not kept:
        problems.append("no hypothesis was admitted")
    if n_inspired < min_inspired:
        problems.append(f"{n_inspired} admitted hypothesis(es) set inspired_by={source!r}; at least {min_inspired} "
                        f"required (forced distance-3 inspiration)")
    if n_mech < min_mech:
        problems.append(f"{n_mech} admitted hypothesis(es) cite a retrieved mechanism; at least {min_mech} required")
    if problems:
        raise AdmissionRefused("hypothesis set rejected by the front quotas: " + "; ".join(problems)
                               + f" (record: {record})")
    return kept, record
