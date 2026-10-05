"""One harness-evolution generation (PLAN L6 pipeline):

parent evaluated on evolve split -> diagnose -> propose typed edits -> static checks ->
race candidates on evolve split -> gate best survivor on gate split -> record lineage (incl. rejects),
harness-science ledger and proposer calibration.

The sealed split is never touched here (PLAN E3).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..gateway import Backend
from ..runtime.spec import HarnessSpec
from ..store import EvidenceStore
from .edits import DEFAULT_PROTECTED_NODES, Edit, EditRejected, apply_edit
from .evaluate import RunOutcome, SandboxFactory, SuiteTask, run_harness
from .propose import diagnose, propose_edits
from .selection import Unit, gate, race


@dataclass
class EvolveConfig:
    seeds: tuple[int, ...] = (0,)
    n_edits: int = 3
    race_rounds: int = 2
    non_regression_eps: float = 0.0
    require_evolve_ci: bool = True
    protected_nodes: tuple[str, ...] = DEFAULT_PROTECTED_NODES
    budget_per_run: dict[str, float] | None = None


@dataclass
class GenerationResult:
    parent_hash: str
    new_parent: HarnessSpec
    accepted: bool
    decision: str
    candidates: list[dict] = field(default_factory=list)
    evaluations: int = 0
    evaluations_fixed_k: int = 0


class Evaluator:
    """Runs (spec, task, seed) and caches outcomes so parent results are reused across stages."""

    def __init__(self, backends: dict[str, Backend], store: EvidenceStore, sandbox_factory: SandboxFactory,
                 tasks: list[SuiteTask], budget_per_run: dict[str, float] | None = None):
        self.backends, self.store, self.sandbox_factory = backends, store, sandbox_factory
        self.tasks = {t.task_id: t for t in tasks}
        self.budget_per_run = budget_per_run
        self.cache: dict[tuple[str, str, int], RunOutcome] = {}
        self.calls = 0  # actual harness runs executed (cache misses)

    def outcome(self, spec: HarnessSpec, unit: Unit) -> RunOutcome:
        key = (spec.spec_hash(), unit[0], unit[1])
        if key not in self.cache:
            self.calls += 1
            self.cache[key] = run_harness(spec, self.tasks[unit[0]], unit[1], backends=self.backends,
                                          store=self.store, sandbox_factory=self.sandbox_factory,
                                          budget_limits=self.budget_per_run)
        return self.cache[key]

    def score(self, spec: HarnessSpec, unit: Unit) -> float:
        return self.outcome(spec, unit).score


def evolve_generation(parent: HarnessSpec, tasks: list[SuiteTask], *, evaluator: Evaluator,
                      proposer: Backend, store: EvidenceStore, cfg: EvolveConfig = EvolveConfig()
                      ) -> GenerationResult:
    evolve_units = [(t.task_id, s) for t in tasks if t.split == "evolve" for s in cfg.seeds]
    gate_units = [(t.task_id, s) for t in tasks if t.split == "gate" for s in cfg.seeds]
    if not evolve_units or not gate_units:
        raise ValueError("need at least one evolve task and one gate task")
    parent_hash = parent.spec_hash()
    calls_before = evaluator.calls

    parent_outcomes = [evaluator.outcome(parent, u) for u in evolve_units]
    parent_scores = {u: o.score for u, o in zip(evolve_units, parent_outcomes)}
    digest = diagnose(parent_outcomes, store)
    ledger = store.records("harness_edit")

    edits, problems, _ = propose_edits(proposer, parent, digest, ledger, n_edits=cfg.n_edits,
                                       protected=cfg.protected_nodes)
    candidates: dict[str, tuple[Edit, HarnessSpec]] = {}
    records: list[dict[str, Any]] = [{"status": "malformed", "problem": p} for p in problems]
    for edit in edits:
        try:
            spec = apply_edit(parent, edit, cfg.protected_nodes)
        except EditRejected as e:
            records.append({"status": "rejected_static", "edit": edit.model_dump(), "reason": str(e)})
            continue
        h = spec.spec_hash()
        if h == parent_hash or h in candidates:
            records.append({"status": "no_op_or_duplicate", "edit": edit.model_dump()})
            continue
        candidates[h] = (edit, spec)
        store.add_harness_version(spec.id, spec.model_dump(mode="json"), parent_hash=parent_hash)

    accepted_spec, accepted, decision = parent, False, "no valid candidates"
    fixed_k = 0
    if candidates:
        rr = race(list(candidates), evolve_units, lambda h, u: evaluator.score(candidates[h][1], u),
                  parent_scores, rounds=cfg.race_rounds, keep=1)
        fixed_k = rr.evaluations_fixed_k
        for h, (edit, _) in candidates.items():
            st = rr.stats.get(h, {})
            records.append({"status": "raced", "candidate": h, "edit": edit.model_dump(), "race": st,
                            "survived": h in rr.survivors})
        if rr.survivors:
            best = rr.survivors[0]
            edit, spec = candidates[best]
            evolve_diffs = [evaluator.score(spec, u) - parent_scores[u] for u in evolve_units]
            gate_diffs = [evaluator.score(spec, u) - evaluator.score(parent, u) for u in gate_units]
            d = gate(evolve_diffs, gate_diffs, non_regression_eps=cfg.non_regression_eps,
                     require_evolve_ci=cfg.require_evolve_ci)
            decision = d.reason
            records.append({"status": "accepted" if d.accepted else "rejected_gate", "candidate": best,
                            "edit": edit.model_dump(), "evolve": d.evolve, "gate": d.gate})
            # Harness-science ledger + proposer calibration (§8.5, §8.10)
            store.append_record("harness_edit", {
                "at": time.time(), "parent": parent_hash, "candidate": best, "type": edit.type.value,
                "rationale": edit.rationale, "accepted": d.accepted, "decision": d.reason,
                "predicted_delta": edit.predicted.expected_delta, "actual_evolve_delta": d.evolve["mean_diff"],
                "actual_gate_delta": d.gate["mean_diff"],
                "clusters_claimed": edit.predicted.failure_clusters_fixed})
            if d.accepted:
                accepted_spec, accepted = spec, True
    for r in records:
        store.append_record("lineage", {"parent": parent_hash, **r})
    return GenerationResult(parent_hash, accepted_spec, accepted, decision, records,
                            evaluator.calls - calls_before, fixed_k)


def proposer_calibration(store: EvidenceStore) -> dict[str, float]:
    """Mean absolute error and sign agreement between predicted and measured edit effects."""
    rows = [r for r in store.records("harness_edit") if r.get("actual_evolve_delta") is not None]
    if not rows:
        return {"n": 0}
    errs = [abs(r["predicted_delta"] - r["actual_evolve_delta"]) for r in rows]
    sign = [(r["predicted_delta"] > 0) == (r["actual_evolve_delta"] > 0) for r in rows]
    return {"n": len(rows), "mae": sum(errs) / len(errs), "sign_agreement": sum(sign) / len(sign)}
