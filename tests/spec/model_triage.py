"""Triage layer over the clean-room model (kernel_model.py stays exactly as its clean-room author wrote it).

Each override below is a disagreement found by the differential run (model vs real Kernel), triaged by reading the
code and GOAL. Every one was judged a MODEL-side gap (the spec was silent, or the model's guess was not fail-closed);
none changes a certificate label except T2. Disagreements judged to be KERNEL-side are NOT patched here: they stay
visible as test failures/xfails in test_kernel_differential.py.

T1 unregistered claim: the Kernel fails K3..K9 as "not evaluated" (certify must not be a free look, kernel_doc §1 /
   exploit K-3); the model passed K3/K5/K8/K9 by assumption.
T2 K1: targets also listed as development datasets fail K1 (GOAL §4.2 "generalization_targets ... NOT used in
   development"); the model missed it (this one also changes labels: model said CERTIFIED where Kernel REJECTED).
T3 a record whose (registered) target has no private label file, or is not a claim target, makes the Kernel's
   label read raise: K3, K8 and K9 fail closed (K9 only when the target is a claim target); the model assumed they pass.
T4 K0 fails when a record's input/target dataset is not registered (or there are no records): "every number
   resolves to an artifact" (GOAL §4.3 K0).
T5 the effect criterion is K6's verdict in the code (checks.py k4 docstring), while GOAL's K4 row lists the CI
   condition under K4. Label-equivalent (both REJECT); recorded as a spec/code wording divergence for the owner.
T6 statistics are not computed when a (arm, seed) record is duplicated or an arm's seeds differ from the plan;
   K7 also fails when the effect has no spread
   (strong=False gives identical arms, a degenerate interval): K7's noise model is undefined there (fail closed).
T8 K3 history: an earlier certificate that failed K3 for a T3 reason also blocks later ones (the model recursed
   with its own, untriaged K3).
T9 K4 counts evaluate reads as a MULTISET: an evaluation logged twice but reported once is selective reporting
   (the model compared sets).
T10 K5 requires a registered BASELINE comparator only for improvement claims (an ablation or negative result may
   compare two methods); the model required it for every kind.
T11 K0 drops records whose target is unregistered; K3 re-executes the rest (the model failed K3 outright).
T7 K1: subject and comparator must be different artifacts (GOAL §4.3 K5 "baseline re-run"); the model missed it.
"""

from __future__ import annotations

from collections import Counter

import kernel_model as model
from history import Certify, ClaimSpec, History, Prereg, Read, Record, Verdict

_REGISTERED = ("K0", "K1", "K2")


def _registered(history: History, key: str) -> bool:
    return any(isinstance(e, Prereg) and e.claim.key == key for e in history)


def _stats(history: History, c: ClaimSpec, records: tuple[Record, ...], t: str) -> bool:
    keys = [(r.arm, r.seed) for r in records if r.target == t]
    exact = all(sorted(s for a, s in keys if a == arm) == sorted(c.seeds) for arm in ("subject", "comparator"))
    return model._stats_computable(history, c, records, t) and len(keys) == len(set(keys)) and exact  # T6


def verdict(history: History, c: ClaimSpec, records: tuple[Record, ...]) -> Verdict:
    base = model.certify(history, c.key, records) if _registered(history, c.key) else \
        model.certify_unregistered(history, c, records)
    ok = {k: v == "pass" for k, v in base.checks.items()}
    ds = model._datasets(history)
    arts = model._artifacts(history)
    if c.kind != "improvement" and c.comparator in arts:  # T10
        ok["K5"] = True
    if set(c.targets) & set(c.development) or c.subject == c.comparator:  # T2, T7
        ok["K1"] = False
    if not records or any(r.target not in ds for r in records):  # T4
        ok["K0"] = False
    if _registered(history, c.key):
        # T11: K0 drops a record whose target is unregistered; K3 judges the records that resolved (fails if none)
        ok["K3"] = any(r.target in ds for r in records)
        unreadable = [r for r in records if r.target in ds and (r.target not in c.targets
                                                                 or ds[r.target].private is None)]
        if unreadable:  # T3
            ok["K3"] = ok["K8"] = False
            if any(r.target in c.targets for r in unreadable):
                ok["K9"] = False
        for i, e in enumerate(history):  # T8
            if isinstance(e, Certify) and e.claim.key == c.key and _registered(history[:i], c.key) and (
                    verdict(history[:i], e.claim, e.records).checks["K3"] == "fail"):
                ok["K3"] = False
        logged = Counter((e.arm, e.seed, e.dataset) for e in history
                         if isinstance(e, Read) and e.purpose == "evaluate" and e.claim == c.key)
        if logged - Counter((r.arm, r.seed, r.target) for r in records):  # T9
            ok["K4"] = False
        stats = {t: _stats(history, c, records, t) for t in c.targets}  # T6
        crit = {t: stats[t] and c.strong and c.kind in model.EFFECT_KINDS for t in c.targets}
        ok["K4"] = ok["K4"] or (base.checks["K4"] == "fail" and _k4_procedural(history, c, records))  # T5
        ok["K6"] = ok["K6"] and all(crit.values())
        ok["K7"] = ok["K7"] and all(crit.values())
    else:  # T1
        for k in ok:
            if k not in _REGISTERED:
                ok[k] = False
    checks = {k: "pass" if v else "fail" for k, v in ok.items()}
    return Verdict(checks=checks, label=model.label_from(checks, history, c), family=base.family)


def _k4_procedural(history: History, c: ClaimSpec, records: tuple[Record, ...]) -> bool:
    """The model's K4 with the criterion oracle forced true (T5): plan, logging and reporting only."""
    strong = ClaimSpec(**{**c.__dict__, "strong": True, "kind": "improvement"})
    if _registered(history, c.key):
        history = tuple(Prereg(strong) if isinstance(e, Prereg) and e.claim.key == c.key else e for e in history)
    return all(_stats(history, strong, records, t) for t in c.targets) and \
        model._k4(history, strong, records)
