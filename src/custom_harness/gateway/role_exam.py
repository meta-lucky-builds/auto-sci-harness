"""Role exams that admit a second model family to a judging role (GOAL §16.8 last bullet, §10 invariant 5).

A model may take a judging role (tournament judge, critic) or the replicator role only after it passes that role's
exam, built from DEV / PUBLIC inputs only, and only if its misses are not too correlated with the proposer's
(phi < max_phi). Everything fails closed: an unparseable answer is a miss, an unknown or mock family is never
admitted, an undefined phi (a constant miss vector) never counts as "diverse".

Status (honest): this is the CODE PATH only. No second-family backend is installed on this machine, so nothing has
been admitted; `second_family_status` says so. The replicator exam needs real runs and is NOT BUILT (interface only).
This module is not wired into `policy.py` (the workstream lead decides that).

Ledger: admission decisions are written with `EvidenceStore.append_record("role_exam", body)` (the records table).
The Kernel ledger kinds are owned by workstream kstats; a dedicated ledger kind has been requested, and until it
exists these records are provenance, not Kernel evidence.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..policy import model_family
from .base import BackendError, ChatRequest, Message

ALLOWED_DATASET_ROLES = frozenset({"dev", "public"})

# Closed flaw taxonomy for the critic's typed output. The first four are planted by the builders; the rest exist so a
# critic can (wrongly) flag a sound plan.
FLAW_TAGS: tuple[str, ...] = ("test_label_peek", "unscaled_distance", "wrong_metric_direction", "target_leak_feature",
                              "id_column_feature", "no_validation", "preprocessing_fit_on_all_data")
DEFECTS: tuple[str, ...] = FLAW_TAGS[:4]
_HIGHER_IS_BETTER = {"accuracy": True, "f1_macro": True, "rmse": False, "mae": False}
_CLASSIFICATION = frozenset({"accuracy", "f1_macro"})


@dataclass(frozen=True)
class ExamThresholds:
    """Every admission bar, in ONE place. Conservative defaults; none has been calibrated on a real model."""
    score_bar: float = 0.80  # UNCALIBRATED (owner decision, GOAL §13.8)
    order_consistency_bar: float = 0.85  # UNCALIBRATED (owner decision, GOAL §13.8)
    critic_tag_recall_bar: float = 0.6  # UNCALIBRATED (owner decision, GOAL §13.8)
    critic_false_flag_max: float = 0.3  # UNCALIBRATED (owner decision, GOAL §13.8)
    max_phi: float = 0.8  # stated in GOAL §16.8 (miss correlation vs the proposer must be below it)


DEFAULT_THRESHOLDS = ExamThresholds()


class ExamSourceError(ValueError):
    """An exam builder was given an input that is not marked dev/public (private, held-out, gate, sealed, unmarked)."""


# --- data model -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ExamItem:
    id: str
    kind: str  # "judge_pair" | "critic"
    system: str
    prompt: str
    schema: dict[str, Any]
    expected: dict[str, Any]
    source: dict[str, Any]  # provenance: dataset_role (dev/public), ref, task_id, profile_sha256, builder
    group: str | None = None  # pair exams: both orders of one pair share a group
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Exam:
    role: str  # "judge" | "critic" | "replicator"
    kind: str
    items: tuple[ExamItem, ...]

    @property
    def content_hash(self) -> str:
        """sha256 of the canonical JSON of the items (any change to any item changes it)."""
        return _sha([dataclasses.asdict(it) for it in self.items])


@dataclass(frozen=True)
class ReplicatorItem:
    """Replicator exam item: a finished dev-only claim to re-implement through `replica_dev`. NOT BUILT."""
    claim_id: str
    replica_dev_ref: str
    source: dict[str, Any]


@dataclass(frozen=True)
class ExamResult:
    role: str
    kind: str
    exam_hash: str
    backend: dict[str, str]
    correct: tuple[bool, ...]
    n_unparsed: int
    order_consistency: float | None = None  # pair exams only
    tag_recall: float | None = None  # critic only
    false_flag_rate: float | None = None  # critic only

    @property
    def misses(self) -> tuple[bool, ...]:
        return tuple(not c for c in self.correct)

    @property
    def score(self) -> float:
        return sum(self.correct) / len(self.correct) if self.correct else 0.0


@dataclass(frozen=True)
class Admission:
    role: str
    admitted: bool
    reasons: tuple[str, ...]
    exam_hash: str
    score: float
    phi: float | None
    candidate_family: str
    proposer_family: str
    order_consistency: float | None = None


# --- helpers ----------------------------------------------------------------------------------------------------

def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def _check_source(source: Any) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        raise ExamSourceError("exam source must be a mapping with dataset_role and ref")
    role, ref = source.get("dataset_role"), source.get("ref")
    if role not in ALLOWED_DATASET_ROLES:
        raise ExamSourceError(f"exam inputs must be dev or public data, got dataset_role={role!r}")
    if not isinstance(ref, str) or not ref.strip():
        raise ExamSourceError("exam source needs a non-empty ref")
    parts = {p.lower() for p in ref.replace("\\", "/").split("/")}
    if parts & {"private", "held_out", "heldout", "sealed", "gate"}:
        raise ExamSourceError(f"exam source ref points into a private/held-out location: {ref!r}")
    return {"dataset_role": role, "ref": ref}


def _check_marks(obj: Any, where: str) -> None:
    """Refuse any nested `dataset_role` that is not dev/public (an input marked private/held-out anywhere)."""
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if k == "dataset_role" and v not in ALLOWED_DATASET_ROLES:
                raise ExamSourceError(f"{where} carries dataset_role={v!r}: only dev/public inputs are allowed")
            _check_marks(v, where)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _check_marks(v, where)


def _as_dict(task: Any) -> dict[str, Any]:
    if isinstance(task, Mapping):
        return dict(task)
    if hasattr(task, "model_dump"):
        return dict(task.model_dump())
    raise TypeError("task must be a mapping or a TaskSpec-like model")


def _prepare(task: Any, profile: Mapping[str, Any], source: Any, builder: str) -> tuple[dict[str, Any], dict]:
    src = _check_source(source)
    t = _as_dict(task)
    _check_marks(t, "task")
    _check_marks(profile, "profile")
    if t.get("metric") not in _HIGHER_IS_BETTER:
        raise ValueError(f"unknown metric {t.get('metric')!r}")
    prov = {**src, "task_id": str(t.get("id", "")), "profile_sha256": _sha(profile), "builder": builder}
    return t, prov


def _features(profile: Mapping[str, Any], target: str) -> list[dict[str, Any]]:
    cols = [c for c in profile.get("columns", []) if c.get("name") != target and c.get("type") == "numeric"
            and c.get("role") not in ("id-like", "constant", "target")]
    rank = {f["name"]: i for i, f in enumerate((profile.get("target") or {}).get("top_features", []))}
    cols.sort(key=lambda c: (rank.get(c["name"], len(rank)), c["name"]))
    if not cols:
        raise ValueError("profile has no usable numeric feature to build an exam from")
    return cols


_VARIANTS = ((5, "1..15", 3), (10, "3..31 (odd)", 5), (5, "5..51 (odd)", None))


def _knn_plan(t: dict[str, Any], feats: list[dict[str, Any]], variant: tuple, defect: str | None) -> str:
    folds, grid, n_feat = variant
    target, metric = t["target_column"], t["metric"]
    higher = _HIGHER_IS_BETTER[metric]
    use = feats[:n_feat] if n_feat else feats
    names = ", ".join(f"'{c['name']}'" for c in use)
    kind = "classifier" if metric in _CLASSIFICATION else "regressor"
    test_labels = t.get("test_labels_file", "test_y.csv")
    s_feat = f"Use the numeric features {names} of the dev training split."
    if defect == "target_leak_feature":
        s_feat = (f"Use the numeric features {names} of the dev training split, plus a feature '{target}_enc' "
                  f"computed from each row's own '{target}' value.")
    s_scale = "Standardise every feature (z-score), fitting the scaler inside each training fold only."
    if defect == "unscaled_distance":
        ranges = "; ".join(f"'{c['name']}' spans {c.get('min')}..{c.get('max')}" for c in use)
        s_scale = f"Use the raw feature values with no scaling ({ranges})."
    s_model = f"Fit a k-nearest-neighbours {kind} with Euclidean distance."
    s_tune = f"Choose k from {grid} by {folds}-fold cross-validation on the dev training split."
    if defect == "test_label_peek":
        s_tune = f"Choose k from {grid} by scoring each candidate's predictions against the labels in {test_labels}."
    best = "highest" if higher else "lowest"
    if defect == "wrong_metric_direction":
        best = "lowest" if higher else "highest"
    s_sel = f"Select the k with the {best} mean cross-validated {metric}."
    s_fit = "Refit on the full dev training split and predict the test features."
    return "\n".join(f"{i}. {s}" for i, s in enumerate((s_feat, s_scale, s_model, s_tune, s_sel, s_fit), 1))


def _linear_plan(t: dict[str, Any], feats: list[dict[str, Any]], folds: int) -> str:
    names = ", ".join(f"'{c['name']}'" for c in feats)
    model = "logistic regression" if t["metric"] in _CLASSIFICATION else "ridge regression"
    best = "highest" if _HIGHER_IS_BETTER[t["metric"]] else "lowest"
    steps = (f"Use the numeric features {names} of the dev training split.",
             "Standardise every feature (z-score), fitting the scaler inside each training fold only.",
             f"Fit {model}; choose the regularisation strength from 0.01..100 (log grid) by {folds}-fold "
             "cross-validation on the dev training split.",
             f"Select the strength with the {best} mean cross-validated {t['metric']}.",
             "Refit on the full dev training split and predict the test features.")
    return "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))


_JUDGE_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"better": {"type": "string", "enum": ["A", "B"]},
                                                                   "reason": {"type": "string"}},
                                 "required": ["better"]}
_CRITIC_SCHEMA: dict[str, Any] = {"type": "object", "properties": {
    "flaws": {"type": "array", "items": {"type": "string", "enum": list(FLAW_TAGS)}},
    "questions": {"type": "array", "items": {"type": "string"}}}, "required": ["flaws"]}
_JUDGE_SYSTEM = ("You judge two analysis plans for the same supervised-learning task. Answer which plan is more "
                 "likely to give a valid, honestly measured result.")
_CRITIC_SYSTEM = ("You critique an analysis plan for a supervised-learning task. List every flaw you find, using only "
                  "the given flaw tags (an empty list if the plan is sound), plus questions for the author.")


# --- builders ---------------------------------------------------------------------------------------------------

def build_judge_exam(task: Any, profile: Mapping[str, Any], *, source: Any) -> Exam:
    """Tournament-judge exam: sound plan vs the same plan with ONE planted defect, each pair in BOTH orders.
    Deterministic in (task, profile, source). Refuses inputs not marked dev/public (ExamSourceError)."""
    t, prov = _prepare(task, profile, source, "judge_pair/1")
    feats = _features(profile, t["target_column"])
    items = []
    for v, variant in enumerate(_VARIANTS):
        sound = _knn_plan(t, feats, variant, None)
        for defect in DEFECTS:
            bad = _knn_plan(t, feats, variant, defect)
            group = f"{prov['task_id']}/v{v}/{defect}"
            for order, (a, b, right) in (("sound_first", (sound, bad, "A")), ("sound_second", (bad, sound, "B"))):
                prompt = (f"Task: {t.get('description', '')}\nMetric: {t['metric']}; target column: "
                          f"'{t['target_column']}'.\n\nPlan A:\n{a}\n\nPlan B:\n{b}\n\nWhich plan is better?")
                items.append(ExamItem(id=f"{group}/{order}", kind="judge_pair", system=_JUDGE_SYSTEM, prompt=prompt,
                                      schema=_JUDGE_SCHEMA, expected={"better": right}, source=dict(prov),
                                      group=group, meta={"defect": defect, "order": order}))
    return Exam(role="judge", kind="judge_pair", items=tuple(items))


def build_critic_exam(task: Any, profile: Mapping[str, Any], *, source: Any) -> Exam:
    """Critic exam: plans with one planted flaw (expected tag) and sound plans (expected: no tag). Graded on typed
    flaw tags from the closed FLAW_TAGS taxonomy and on the false-flag rate over sound plans."""
    t, prov = _prepare(task, profile, source, "critic/1")
    feats = _features(profile, t["target_column"])
    plans: list[tuple[str, str, list[str]]] = []
    for v, variant in enumerate(_VARIANTS):
        plans.append((f"v{v}/sound", _knn_plan(t, feats, variant, None), []))
        plans.extend((f"v{v}/{d}", _knn_plan(t, feats, variant, d), [d]) for d in DEFECTS)
    for folds in (3, 5, 10):
        plans.append((f"linear{folds}/sound", _linear_plan(t, feats, folds), []))
    items = []
    for pid, plan, flaws in plans:
        prompt = (f"Task: {t.get('description', '')}\nMetric: {t['metric']}; target column: '{t['target_column']}'."
                  f"\nFlaw tags: {', '.join(FLAW_TAGS)}.\n\nPlan:\n{plan}")
        items.append(ExamItem(id=f"{prov['task_id']}/{pid}", kind="critic", system=_CRITIC_SYSTEM, prompt=prompt,
                              schema=_CRITIC_SCHEMA, expected={"flaws": flaws}, source=dict(prov),
                              meta={"defect": flaws[0] if flaws else None}))
    return Exam(role="critic", kind="critic", items=tuple(items))


def build_replicator_exam(items: Sequence[ReplicatorItem]) -> Exam:
    """NOT BUILT: the replicator exam needs >= 10 finished dev-only claims re-run through `replica_dev`, i.e. real
    runs; it cannot be tested honestly with fakes."""
    raise NotImplementedError("replicator exam NOT BUILT: needs real finished claims run through replica_dev")


def grade_replicator_item(item: ReplicatorItem, replica_outcome: Mapping[str, Any]) -> bool:
    """Grading hook for one replicator item (success, and similarity below the 0.8 limit). NOT BUILT."""
    raise NotImplementedError("replicator exam grading NOT BUILT: needs real replica_dev outcomes")


# --- running and grading ----------------------------------------------------------------------------------------

def backend_identity(backend: Any) -> dict[str, str]:
    return {"backend": str(getattr(backend, "name", "") or ""), "provider": str(getattr(backend, "provider", "") or ""),
            "model": str(getattr(backend, "model", "") or ""), "family": model_family(backend)}


def _parse(item: ExamItem, text: str) -> Any:
    """Strict, fail-closed parse: the whole reply must be the JSON object; returns None when unusable."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    if item.kind == "judge_pair":
        return obj["better"] if obj.get("better") in ("A", "B") else None
    flaws = obj.get("flaws")
    if not isinstance(flaws, list) or not all(isinstance(f, str) and f in FLAW_TAGS for f in flaws):
        return None
    return set(flaws)


def run_exam(backend: Any, exam: Exam) -> ExamResult:
    """One model call per item with the item's JSON schema. Unparseable output or a backend error is a miss."""
    answers: list[Any] = []
    for it in exam.items:
        req = ChatRequest(messages=[Message(role="system", content=it.system), Message(role="user", content=it.prompt)],
                          json_schema=it.schema, temperature=0)
        try:
            answers.append(_parse(it, backend.complete(req).text))
        except BackendError:
            answers.append(None)
    n_unparsed = sum(a is None for a in answers)
    ident = backend_identity(backend)
    if exam.kind == "judge_pair":
        correct = tuple(a is not None and a == it.expected["better"] for it, a in zip(exam.items, answers, strict=True))
        groups: dict[str, list[tuple[bool, Any]]] = {}
        for it, a, c in zip(exam.items, answers, correct, strict=True):
            groups.setdefault(str(it.group), []).append((c, a))
        pairs = [g for g in groups.values() if len(g) == 2]
        consistent = sum(all(a is not None for _, a in g) and g[0][0] == g[1][0] for g in pairs)
        return ExamResult(role=exam.role, kind=exam.kind, exam_hash=exam.content_hash, backend=ident,
                          correct=correct, n_unparsed=n_unparsed,
                          order_consistency=consistent / len(pairs) if pairs else 0.0)
    if exam.kind == "critic":
        correct_l, hits, flawed, flags, sound = [], 0, 0, 0, 0
        for it, a in zip(exam.items, answers, strict=True):
            planted = set(it.expected["flaws"])
            if planted:
                flawed += 1
                ok = a is not None and planted <= a
                hits += ok
            else:
                sound += 1
                ok = a is not None and not a
                flags += not ok  # an unparseable answer on a sound plan counts as a false flag (fail closed)
            correct_l.append(ok)
        return ExamResult(role=exam.role, kind=exam.kind, exam_hash=exam.content_hash, backend=ident,
                          correct=tuple(correct_l), n_unparsed=n_unparsed,
                          tag_recall=hits / flawed if flawed else 0.0,
                          false_flag_rate=flags / sound if sound else 1.0)
    raise ValueError(f"cannot run exam kind {exam.kind!r}")


def miss_phi(misses_a: Sequence[bool], misses_b: Sequence[bool]) -> float | None:
    """Phi (Matthews) correlation of two miss vectors over the same items. None when undefined (empty input or a
    constant vector); callers must treat None as "diversity not shown", never as diverse."""
    if len(misses_a) != len(misses_b):
        raise ValueError("miss vectors must cover the same items")
    n11 = sum(1 for a, b in zip(misses_a, misses_b, strict=True) if a and b)
    n10 = sum(1 for a, b in zip(misses_a, misses_b, strict=True) if a and not b)
    n01 = sum(1 for a, b in zip(misses_a, misses_b, strict=True) if not a and b)
    n00 = len(misses_a) - n11 - n10 - n01
    denom = (n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00)
    if denom == 0:
        return None
    return (n11 * n00 - n10 * n01) / math.sqrt(denom)


def _family_ok(family: str) -> bool:
    return bool(family) and not family.startswith("unknown:") and family != "mock"


def admit(role: str, candidate_result: ExamResult, proposer_result: ExamResult, candidate_family: str,
          proposer_family: str, thresholds: ExamThresholds = DEFAULT_THRESHOLDS) -> Admission:
    """Admit a candidate backend to `role` (families from `policy.model_family(backend)`). Admitted only if every
    check passes; each failed check adds a reason. Roles: "judge" (tournament), "critic"; "replicator" is NOT BUILT
    and always refused. (policy.py's `judge` role covers both critic and tournament; the mapping is the lead's.)"""
    reasons: list[str] = []
    if role == "replicator":
        reasons.append("replicator exam NOT BUILT: cannot admit")
    elif candidate_result.role != role:
        reasons.append(f"exam is for role {candidate_result.role!r}, not {role!r}")
    if candidate_result.exam_hash != proposer_result.exam_hash:
        reasons.append("candidate and proposer took different exams: miss correlation is not comparable")
    if not _family_ok(candidate_family) or not _family_ok(proposer_family) or candidate_family == proposer_family:
        reasons.append(f"family check failed: candidate {candidate_family!r} must be a known, non-mock family "
                       f"different from the proposer's {proposer_family!r}")
    t = thresholds
    if candidate_result.kind == "critic":
        recall, ff = candidate_result.tag_recall or 0.0, candidate_result.false_flag_rate
        if recall < t.critic_tag_recall_bar:
            reasons.append(f"typed-flaw recall {recall:.3f} < {t.critic_tag_recall_bar}")
        if ff is None or ff > t.critic_false_flag_max:
            reasons.append(f"false-flag rate {ff} > {t.critic_false_flag_max}")
    else:
        if candidate_result.score < t.score_bar:
            reasons.append(f"score {candidate_result.score:.3f} < {t.score_bar}")
        oc = candidate_result.order_consistency
        if oc is None or oc < t.order_consistency_bar:
            reasons.append(f"order consistency {oc} < {t.order_consistency_bar}")
    phi = None
    if len(candidate_result.misses) == len(proposer_result.misses):
        phi = miss_phi(candidate_result.misses, proposer_result.misses)
    if phi is None:
        reasons.append("miss phi undefined (constant or mismatched miss vector): diversity not shown")
    elif phi >= t.max_phi:
        reasons.append(f"miss phi {phi:.3f} >= {t.max_phi} vs the proposer: diversity not shown")
    return Admission(role=role, admitted=not reasons, reasons=tuple(reasons), exam_hash=candidate_result.exam_hash,
                     score=candidate_result.score, phi=phi, candidate_family=candidate_family,
                     proposer_family=proposer_family, order_consistency=candidate_result.order_consistency)


# --- ledger -----------------------------------------------------------------------------------------------------

RECORD_KIND = "role_exam"


def record_admission(store: Any, backend: Any, admission: Admission) -> dict[str, Any]:
    """Write one admission decision (admitted or refused) via store.append_record("role_exam", body)."""
    ident = backend_identity(backend)
    if ident["family"] != admission.candidate_family:
        raise ValueError(f"backend family {ident['family']!r} != the admission's {admission.candidate_family!r}")
    body = {**ident, "role": admission.role, "exam_hash": admission.exam_hash, "score": admission.score,
            "phi": admission.phi, "order_consistency": admission.order_consistency,
            "proposer_family": admission.proposer_family, "admitted": admission.admitted,
            "reasons": list(admission.reasons), "schema": "role_exam/1"}
    store.append_record(RECORD_KIND, body)
    return body


def admitted_roles(store: Any, backend: Any, *, exam_hashes: Mapping[str, str]) -> set[str]:
    """Roles this EXACT backend (name + provider + model + family) is admitted to under the CURRENT exam of each role
    (`exam_hashes`: role -> content hash). The latest decision per (role, exam) wins; no record means not admitted."""
    ident = backend_identity(backend)
    latest: dict[str, bool] = {}
    for r in store.records(RECORD_KIND):
        if all(r.get(k) == v for k, v in ident.items()) and exam_hashes.get(r.get("role")) == r.get("exam_hash"):
            latest[r["role"]] = r.get("admitted") is True
    return {role for role, ok in latest.items() if ok}


def _is_mock(backend: Any) -> bool:
    mode = str(getattr(backend, "mode", "") or "")
    return str(getattr(backend, "provider", "")) == "mock" or mode in ("mock", "AccessMode.MOCK")


def second_family_status(backends: Mapping[str, Any]) -> str:
    """Honest status of invariant 5's second family for a role -> backend mapping."""
    needs = ("needs owner: install a local model (e.g. via Ollama) or provide an allowlisted API; no second-family "
             "backend is configured")
    proposer = backends.get("proposer") or backends.get("executor")
    if proposer is None:
        return needs + " (and no proposer backend is configured)"
    pf = model_family(proposer)
    cands = sorted({f"{getattr(b, 'name', '?')} ({model_family(b)})" for b in backends.values()
                    if not _is_mock(b) and _family_ok(model_family(b)) and model_family(b) != pf})
    if not cands:
        return needs
    return (f"second-family candidate(s) configured: {', '.join(cands)}; not admitted until each passes its role "
            "exam (run_exam + admit, recorded as role_exam)")
