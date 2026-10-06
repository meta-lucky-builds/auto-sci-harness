"""One harness-evolution generation (PLAN L6 pipeline):

parent evaluated on evolve split -> diagnose -> propose typed edits -> static checks ->
race candidates on evolve split -> RE-MEASURE the race winner and the parent on fresh runs (fresh seeds,
same evolve tasks) -> gate on (fresh evolve evidence, gate split) -> record lineage (incl. rejects),
harness-science ledger and proposer calibration.

Why re-measure (CR P0-2, winner's curse): the race picks the candidate with the best observed mean, so
its race data are biased upward; reusing them as acceptance evidence inflates false accepts. The gate
only ever sees evolve evidence the selection step did not look at.

The sealed split is never touched here (PLAN E3).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..budget import Budget, BudgetExceeded
from ..gateway import Backend
from ..runtime.engine import RetryPolicy
from ..runtime.spec import HarnessSpec
from ..store import EvidenceStore
from .edits import DEFAULT_PROTECTED_NODES, Edit, EditRejected, apply_edit
from .evaluate import RunOutcome, SandboxFactory, SuiteTask, run_harness
from .propose import diagnose, propose_edits
from .selection import Unit, gate, paired_sample, race


@dataclass
class EvolveConfig:
    seeds: tuple[int, ...] = (0,)
    # seeds for the fresh re-measurement of the race winner; default = seeds shifted by 1000.
    # Must be disjoint from `seeds`.
    remeasure_seeds: tuple[int, ...] | None = None
    n_edits: int = 3
    race_rounds: int = 2
    non_regression_eps: float = 0.0
    require_evolve_ci: bool = True
    max_excluded_frac: float = 0.25  # gate fails closed above this share of infra-excluded units
    infra_retries: int = 1  # fresh re-runs of a unit whose run failed for infrastructure reasons
    protected_nodes: tuple[str, ...] = DEFAULT_PROTECTED_NODES
    budget_per_run: dict[str, float | None] | None = None


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
                 tasks: list[SuiteTask], budget_per_run: dict[str, float | None] | None = None,
                 infra_retries: int = 1, retry: RetryPolicy | None = None,
                 campaign_budget: Budget | None = None):
        self.backends, self.store, self.sandbox_factory = backends, store, sandbox_factory
        self.tasks = {t.task_id: t for t in tasks}
        self.budget_per_run = budget_per_run
        self.infra_retries = infra_retries
        self.retry = retry
        # campaign-level cap across all runs (persisted when built with budget.persistent_budget); checked
        # before every run, charged after it; exhaustion stops the campaign cleanly (BudgetExceeded)
        self.campaign_budget = campaign_budget
        self.cache: dict[tuple[str, str, int], RunOutcome] = {}
        self.calls = 0  # actual harness runs executed (cache misses, incl. infra re-runs)
        self.infra_failures = 0  # runs that ended INFRA_FAILED (each one excluded, never scored)

    def outcome(self, spec: HarnessSpec, unit: Unit) -> RunOutcome:
        """A unit whose run failed for infrastructure reasons is re-run (fresh run, same seed) up to
        `infra_retries` times; if it still fails it stays excluded (score None)."""
        key = (spec.spec_hash(), unit[0], unit[1])
        if key not in self.cache:
            for _ in range(1 + self.infra_retries):
                if self.campaign_budget is not None:
                    self.campaign_budget.check()
                self.calls += 1
                out = run_harness(spec, self.tasks[unit[0]], unit[1], backends=self.backends,
                                  store=self.store, sandbox_factory=self.sandbox_factory,
                                  budget_limits=self.budget_per_run, retry=self.retry)
                if self.campaign_budget is not None:
                    try:
                        self.campaign_budget.charge(out.cost)
                    except BudgetExceeded:
                        pass  # recorded; the next check() stops the campaign before any further run
                if not out.infra_failed:
                    break
                self.infra_failures += 1
            self.cache[key] = out
        return self.cache[key]

    def score(self, spec: HarnessSpec, unit: Unit) -> float | None:
        return self.outcome(spec, unit).score


def evolve_generation(parent: HarnessSpec, tasks: list[SuiteTask], *, evaluator: Evaluator,
                      proposer: Backend, store: EvidenceStore, cfg: EvolveConfig | None = None
                      ) -> GenerationResult:
    cfg = cfg or EvolveConfig()
    evolve_units = [(t.task_id, s) for t in tasks if t.split == "evolve" for s in cfg.seeds]
    gate_units = [(t.task_id, s) for t in tasks if t.split == "gate" for s in cfg.seeds]
    if not evolve_units or not gate_units:
        raise ValueError("need at least one evolve task and one gate task")
    if {u[0] for u in evolve_units} & {u[0] for u in gate_units}:
        raise ValueError("a task id appears in both the evolve and the gate split")
    fresh_seeds = cfg.remeasure_seeds if cfg.remeasure_seeds is not None else tuple(s + 1000 for s in cfg.seeds)
    if not fresh_seeds or set(fresh_seeds) & set(cfg.seeds):
        raise ValueError("remeasure_seeds must be non-empty and disjoint from seeds")
    fresh_units = [(t.task_id, s) for t in tasks if t.split == "evolve" for s in fresh_seeds]
    parent_hash = parent.spec_hash()
    calls_before = evaluator.calls

    parent_outcomes = [evaluator.outcome(parent, u) for u in evolve_units]
    parent_scores = {u: o.score for u, o in zip(evolve_units, parent_outcomes, strict=True)}
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
        rr = race(list(candidates), evolve_units, lambda h, u: evaluator.score(candidates[str(h)][1], u),
                  parent_scores, rounds=cfg.race_rounds, keep=1)
        fixed_k = rr.evaluations_fixed_k
        for h, (edit, _) in candidates.items():
            st = rr.stats.get(h, {})
            records.append({"status": "raced", "candidate": h, "edit": edit.model_dump(), "race": st,
                            "survived": h in rr.survivors})
        if rr.survivors:
            best = str(rr.survivors[0])
            edit, spec = candidates[best]
            # fresh runs for BOTH arms: the race data that selected this winner are never acceptance evidence
            evolve_s = paired_sample(fresh_units, lambda u: evaluator.score(spec, u),
                                     lambda u: evaluator.score(parent, u))
            gate_s = paired_sample(gate_units, lambda u: evaluator.score(spec, u),
                                   lambda u: evaluator.score(parent, u))
            d = gate(evolve_s, gate_s, non_regression_eps=cfg.non_regression_eps,
                     require_evolve_ci=cfg.require_evolve_ci, max_excluded_frac=cfg.max_excluded_frac)
            decision = d.reason
            records.append({"status": "accepted" if d.accepted else "rejected_gate", "candidate": best,
                            "edit": edit.model_dump(), "race": rr.stats.get(best, {}), "evolve_fresh": d.evolve,
                            "gate": d.gate})
            # Harness-science ledger + proposer calibration (§8.5, §8.10)
            store.append_record("harness_edit", {
                "at": time.time(), "parent": parent_hash, "candidate": best, "type": edit.type.value,
                "rationale": edit.rationale, "accepted": d.accepted, "decision": d.reason,
                "predicted_delta": edit.predicted.expected_delta, "actual_evolve_delta": d.evolve["mean_diff"],
                "race_evolve_delta": rr.stats.get(best, {}).get("mean_diff"),
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
