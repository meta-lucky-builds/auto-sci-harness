"""One harness-evolution generation (PLAN L6 pipeline):

parent evaluated on evolve split -> diagnose -> propose typed edits -> static checks ->
race candidates on evolve split -> RE-MEASURE the race winner and the parent on fresh runs (fresh seeds,
same evolve tasks) -> gate on (fresh evolve evidence, gate split) -> leave-one-family-out (and, with >= 2 domains,
leave-one-domain-out) non-regression on EACH held-out family/domain -> record lineage (incl. rejects),
harness-science ledger (incl. per-group results and OOD drop, key "lofo") and proposer calibration.
A campaign without a held-out family (/domain) is refused (GOAL invariant 16, fail closed).

M4 pieces (GOAL §5 L6.3/6.6/6.8/6.10): the diagnosis carries first-divergence causal signatures from evolve-split
runs; each edit's cited cluster/divergence ids are checked against it (`claims_check`); an edit whose normalised
signature equals a previously REJECTED one (static or gate) is refused before any evaluation (novelty gate); the
proposer may decline (recorded, not an error); acceptance also needs the cost term (candidate cost on the paired gate
units <= (1 + max_cost_increase) x parent, fail closed without cost data); the ledger records per-task
flipped/regressed lists, `regressed_unpredicted` and the generation's yield/prune counters (`generation_stats`).
The proposer sees past edits only through `proposer_view` (export.py): gate/held-out details never reach it.

Why re-measure (CR P0-2, winner's curse): the race picks the candidate with the best observed mean, so
its race data are biased upward; reusing them as acceptance evidence inflates false accepts. The gate
only ever sees evolve evidence the selection step did not look at.

The sealed split is never touched here (PLAN E3).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..budget import Budget, BudgetExceeded
from ..gateway import Backend
from ..kerneld.releases import Releases, private_reads
from ..runtime.engine import RetryPolicy
from ..runtime.spec import HarnessSpec
from ..store import EvidenceStore
from .diagnosis import check_claims
from .edits import DEFAULT_PROTECTED_NODES, Edit, EditRejected, apply_edit
from .evaluate import RunOutcome, SandboxFactory, SuiteTask, check_held_out, run_harness
from .export import proposer_view
from .multiplicity import check_run, last_prereg_seq
from .propose import diagnose, propose
from .selection import (
    PairedSample,
    Unit,
    cost_bound,
    exclusion_check,
    gate,
    non_regression,
    paired_sample,
    race,
)

# lineage statuses whose edit signature the novelty gate refuses to see again (GOAL §5 L6.6, HarnessX "reverted").
# Race losers are NOT included: they were eliminated on a few evolve units, never judged by the gate.
REJECTED_STATUSES = ("rejected_static", "rejected_gate")


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
    # cost term (GOAL §5 L6.8, RRSI): candidate total cost on the paired gate units <= (1 + this) x parent's.
    # 1.0 = at most twice the parent's cost: a provisional default, not calibrated on a real campaign.
    max_cost_increase: float = 1.0
    cost_metric: str = "tokens"  # "tokens" (prompt + completion), "wall_s" or "usd" (selection.COST_METRICS)


@dataclass
class GenerationResult:
    parent_hash: str
    new_parent: HarnessSpec
    accepted: bool
    decision: str
    candidates: list[dict] = field(default_factory=list)
    evaluations: int = 0
    evaluations_fixed_k: int = 0
    # GOAL §6.1: VALIDATED only if the gate decision rested entirely on CERTIFIED-VALIDATED evidence (external or
    # future data); otherwise the generation optimised an internal metric: MECHANICAL
    loop_label: str = "MECHANICAL"
    # yield/prune counters of this generation (also appended as a `generation_stats` record)
    counters: dict[str, Any] = field(default_factory=dict)


class Evaluator:
    """Runs (spec, task, seed) and caches outcomes so parent results are reused across stages."""

    def __init__(self, backends: dict[str, Backend], store: EvidenceStore, sandbox_factory: SandboxFactory,
                 tasks: list[SuiteTask], budget_per_run: dict[str, float | None] | None = None,
                 infra_retries: int = 1, retry: RetryPolicy | None = None,
                 campaign_budget: Budget | None = None, releases: Releases | None = None):
        sealed = sorted(t.task_id for t in tasks if t.split == "sealed")
        if sealed:  # invariant 6: the sealed split is touched only by the human-run `finalize` (evolve/finalize.py)
            raise ValueError(f"sealed task(s) {sealed[:5]} cannot be evaluated during evolution (finalize only)")
        self.backends, self.store, self.sandbox_factory = backends, store, sandbox_factory
        self.tasks = {t.task_id: t for t in tasks}
        self.budget_per_run = budget_per_run
        self.infra_retries = infra_retries
        self.retry = retry
        # campaign-level cap across all runs (persisted when built with budget.persistent_budget); checked
        # before every run, charged after it; exhaustion stops the campaign cleanly (BudgetExceeded)
        self.campaign_budget = campaign_budget
        self.releases = releases  # None = live releases on the store's ledger; a replay passes pinned releases
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
                since = last_prereg_seq(self.store)
                out = run_harness(spec, self.tasks[unit[0]], unit[1], backends=self.backends,
                                  store=self.store, sandbox_factory=self.sandbox_factory,
                                  budget_limits=self.budget_per_run, retry=self.retry, releases=self.releases)
                # R2-2: every claim this run certified must have used m = its declared n_comparisons (constant
                # strictness, independent of evaluation order); otherwise MultiplicityDrift stops the campaign
                check_run(self.store, since)
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
                      proposer: Backend, store: EvidenceStore, cfg: EvolveConfig | None = None,
                      releases: Releases | None = None) -> GenerationResult:
    cfg = cfg or EvolveConfig()
    evolve_units = [(t.task_id, s) for t in tasks if t.split == "evolve" for s in cfg.seeds]
    gate_units = [(t.task_id, s) for t in tasks if t.split == "gate" for s in cfg.seeds]
    if not evolve_units or not gate_units:
        raise ValueError("need at least one evolve task and one gate task")
    if {u[0] for u in evolve_units} & {u[0] for u in gate_units}:
        raise ValueError("a task id appears in both the evolve and the gate split")
    # fail closed (GOAL invariant 16): no campaign without a held-out family (and domain, once >= 2 exist)
    heldout_families, heldout_domains = check_held_out(tasks)
    fresh_seeds = cfg.remeasure_seeds if cfg.remeasure_seeds is not None else tuple(s + 1000 for s in cfg.seeds)
    if not fresh_seeds or set(fresh_seeds) & set(cfg.seeds):
        raise ValueError("remeasure_seeds must be non-empty and disjoint from seeds")
    fresh_units = [(t.task_id, s) for t in tasks if t.split == "evolve" for s in fresh_seeds]
    parent_hash = parent.spec_hash()
    calls_before = evaluator.calls

    parent_outcomes = [evaluator.outcome(parent, u) for u in evolve_units]
    parent_scores = {u: o.score for u, o in zip(evolve_units, parent_outcomes, strict=True)}
    evolve_ids = {t.task_id for t in tasks if t.split == "evolve"}
    # first-divergence references: any cached outcome of an EVOLVE task (other seeds, earlier candidates); gate
    # outcomes never feed the diagnosis, which the proposer reads
    references = [o for (_, task_id, _), o in getattr(evaluator, "cache", {}).items() if task_id in evolve_ids]
    digest = diagnose(parent_outcomes, store, references=references)
    ledger = [proposer_view(r, evolve_ids) for r in store.records("harness_edit")]

    proposal = propose(proposer, parent, digest, ledger, n_edits=cfg.n_edits, protected=cfg.protected_nodes)
    edits, problems = proposal.edits, proposal.problems
    rejected_before = rejected_signatures(store)
    candidates: dict[str, tuple[Edit, HarnessSpec]] = {}
    records: list[dict[str, Any]] = [{"status": "malformed", "problem": p} for p in problems]
    if proposal.decline is not None:
        records.append({"status": "declined", "reason": proposal.decline})
    for edit in edits:
        sig = edit_signature(edit)
        base = {"edit": edit.model_dump(), "signature": sig, "claims_check": check_claims(edit, digest)}
        if sig in rejected_before:
            records.append({"status": "rejected_novelty", **base,
                            "reason": "identical (normalised) to a previously rejected edit"})
            continue
        try:
            spec = apply_edit(parent, edit, cfg.protected_nodes)
        except EditRejected as e:
            records.append({"status": "rejected_static", **base, "reason": str(e)})
            continue
        h = spec.spec_hash()
        if h == parent_hash or h in candidates:
            records.append({"status": "no_op_or_duplicate", **base})
            continue
        candidates[h] = (edit, spec)
        store.add_harness_version(spec.id, spec.model_dump(mode="json"), parent_hash=parent_hash)

    accepted_spec, accepted = parent, False
    decision = f"proposer declined: {proposal.decline}" if proposal.decline is not None else "no valid candidates"
    survivors: list[Any] = []
    loop_label = "MECHANICAL"
    fixed_k = 0
    if candidates:
        rr = race(list(candidates), evolve_units, lambda h, u: evaluator.score(candidates[str(h)][1], u),
                  parent_scores, rounds=cfg.race_rounds, keep=1)
        fixed_k = rr.evaluations_fixed_k
        survivors = list(rr.survivors)
        for h, (edit, _) in candidates.items():
            st = rr.stats.get(h, {})
            records.append({"status": "raced", "candidate": h, "edit": edit.model_dump(), "race": st,
                            "survived": h in rr.survivors, "signature": edit_signature(edit)})
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
            # candidate-only exclusions over the acceptance evidence (fresh evolve + gate units; the LOFO groups are
            # subsets of the gate units, the race is selection only and its parent runs predate the candidates')
            excl = exclusion_check(evolve_s, gate_s)
            lofo = _lofo(tasks, heldout_families, heldout_domains, cfg,
                         lambda us: paired_sample(us, lambda u: evaluator.score(spec, u),
                                                  lambda u: evaluator.score(parent, u)))
            # cost term on the gate units valid in BOTH arms (an infra-excluded unit has no comparable cost)
            valid_gate = [u for u in gate_units
                          if evaluator.score(spec, u) is not None and evaluator.score(parent, u) is not None]
            cost = cost_bound(valid_gate, lambda u: evaluator.outcome(spec, u).cost,
                              lambda u: evaluator.outcome(parent, u).cost,
                              max_increase=cfg.max_cost_increase, metric=cfg.cost_metric)
            ok = d.accepted and lofo["passed"] and cost["passed"] and excl["passed"]
            # the accept/reject bit is computed from gate-target verdicts and reaches the proposer (proposer_view):
            # a ledgered verdict-bit release on every target the acceptance runs read (GOAL §16.5.3); pinned in a
            # replayed campaign
            used_runs = {evaluator.outcome(x, u).run_id for x in (spec, parent) for u in gate_units}
            gate_targets = sorted({t for r in used_runs if r for t in private_reads(store, run_id=r)})
            rel = releases or getattr(evaluator, "releases", None) or Releases.live(store)
            ok = bool(rel.emit(ok, targets=gate_targets, channel="evolution_score", alphabet="verdict_bit",
                               note="evolve.gate:decision", key=f"{parent_hash}|{best}"))
            rejected_on: str | None
            if not excl["passed"]:  # the comparison itself is untrustworthy: checked first (fail closed)
                decision, rejected_on = "rejected: " + excl["reason"], "exclusion_asymmetry"
            elif not d.accepted:
                decision = d.reason
                rejected_on = "evolve" if d.reason.startswith("evolve") else "held_out_gate"
            elif not lofo["passed"]:
                decision, rejected_on = "rejected: " + lofo["reason"], "held_out_gate"
            elif not cost["passed"]:
                decision, rejected_on = "rejected: " + cost["reason"], "cost"
            else:
                decision, rejected_on = d.reason, None
            per_task = _per_task(fresh_units + gate_units, tasks, lambda u: evaluator.score(spec, u),
                                 lambda u: evaluator.score(parent, u))
            flipped = sorted(t for t, r in per_task.items() if r["mean_diff"] is not None and r["mean_diff"] > 0)
            regressed = sorted(t for t, r in per_task.items() if r["mean_diff"] is not None and r["mean_diff"] < 0)
            at_risk = set(edit.predicted.tasks_at_risk)
            claims = check_claims(edit, digest)
            # every unit the acceptance decision rested on, BOTH arms: the fresh evolve re-measurement and the gate
            # (R2-4: the gate alone made a decision on internal evolve evidence look VALIDATED)
            used_labels = [evaluator.outcome(x, u).cert_label for x in (spec, parent) for u in fresh_units + gate_units]
            loop_label = "VALIDATED" if all(lb == "CERTIFIED-VALIDATED" for lb in used_labels) else "MECHANICAL"
            records.append({"status": "accepted" if ok else "rejected_gate", "candidate": best,
                            "loop_label": loop_label,
                            "edit": edit.model_dump(), "race": rr.stats.get(best, {}), "evolve_fresh": d.evolve,
                            "gate": d.gate, "pooled_gate_accepted": d.accepted, "lofo": lofo, "cost": cost,
                            "exclusions": excl, "rejected_on": rejected_on, "signature": edit_signature(edit),
                            "claims_check": claims})
            # Harness-science ledger + proposer calibration (§8.5, §8.10)
            store.append_record("harness_edit", {
                "at": time.time(), "parent": parent_hash, "candidate": best, "type": edit.type.value,
                "rationale": edit.rationale, "accepted": ok, "decision": decision,
                "pooled_gate_accepted": d.accepted, "lofo": lofo,
                "predicted_delta": edit.predicted.expected_delta, "actual_evolve_delta": d.evolve["mean_diff"],
                "race_evolve_delta": rr.stats.get(best, {}).get("mean_diff"),
                "actual_gate_delta": d.gate["mean_diff"], "loop_label": loop_label,
                "clusters_claimed": edit.predicted.failure_clusters_fixed, "claims_check": claims,
                "cost": cost, "exclusions": excl, "rejected_on": rejected_on, "signature": edit_signature(edit),
                "per_task": per_task, "flipped": flipped, "regressed": regressed,
                "tasks_at_risk": sorted(at_risk), "regressed_unpredicted": [t for t in regressed if t not in at_risk]})
            if ok:
                accepted_spec, accepted = spec, True
    for r in records:
        store.append_record("lineage", {"parent": parent_hash, **r})
    counters = _counters(records, n_proposed=len(edits) + len(problems), n_raced=len(candidates),
                         n_survivors=len(survivors))
    store.append_record("generation_stats", {"parent": parent_hash, **counters})
    return GenerationResult(parent_hash, accepted_spec, accepted, decision, records,
                            evaluator.calls - calls_before, fixed_k, loop_label, counters)


def edit_signature(edit: Edit) -> str:
    """Normalised edit signature for the novelty gate: the edit type plus its ops sorted by (path, op, value), each
    value in canonical JSON (dict key order irrelevant). Rationale and predictions are not part of it: the same
    change re-worded is the same change."""
    ops = sorted((o.path, o.op, json.dumps(o.value, sort_keys=True, default=str)) for o in edit.ops)
    return hashlib.sha256(json.dumps([edit.type.value, ops]).encode()).hexdigest()


def rejected_signatures(store: EvidenceStore) -> set[str]:
    """Signatures of every edit the lineage records as rejected (static or gate). Records written before signatures
    existed are re-signed from their stored edit; an unreadable one is skipped (it cannot be compared)."""
    out: set[str] = set()
    for r in store.records("lineage"):
        if r.get("status") not in REJECTED_STATUSES:
            continue
        if r.get("signature"):
            out.add(r["signature"])
        elif isinstance(r.get("edit"), dict):
            try:
                out.add(edit_signature(Edit.model_validate(r["edit"])))
            except ValueError:
                continue
    return out


def _per_task(units: list[Unit], tasks: list[SuiteTask], cand: Callable[[Unit], float | None],
              parent: Callable[[Unit], float | None]) -> dict[str, dict[str, Any]]:
    """Per-task attribution (L6.10): mean paired difference over the task's valid units (fresh evolve + gate)."""
    split = {t.task_id: t.split for t in tasks}
    diffs: dict[str, list[float]] = {}
    for u in units:
        c, p = cand(u), parent(u)
        diffs.setdefault(u[0], [])
        if c is not None and p is not None:
            diffs[u[0]].append(c - p)
    return {t: {"split": split.get(t), "n": len(d), "mean_diff": sum(d) / len(d) if d else None}
            for t, d in sorted(diffs.items())}


def _counters(records: list[dict[str, Any]], *, n_proposed: int, n_raced: int, n_survivors: int) -> dict[str, Any]:
    """Yield/prune counters of one generation (L6.10). yield = accepted / proposed; pruned = raced - race survivors."""
    count = {s: sum(r["status"] == s for r in records) for s in (
        "malformed", "declined", "rejected_novelty", "rejected_static", "no_op_or_duplicate", "accepted",
        "rejected_gate")}
    gated = count["accepted"] + count["rejected_gate"]
    return {"proposed": n_proposed, **count, "raced": n_raced, "pruned": n_raced - n_survivors, "gated": gated,
            "yield": count["accepted"] / n_proposed if n_proposed else 0.0}


def _lofo(tasks: list[SuiteTask], families: list[str], domains: list[str], cfg: EvolveConfig,
          sample: Callable[[list[Unit]], PairedSample]) -> dict[str, Any]:
    """Leave-one-family-out / leave-one-domain-out gate (GOAL §5 L6.8): non-regression on EACH held-out family and
    domain separately, over its gate units (same fresh outcomes as the pooled gate). `ood_drop` = mean paired
    difference per held-out group (negative = the candidate is worse out of distribution)."""
    out: dict[str, Any] = {"families": {}, "domains": {}, "ood_drop": {}}
    failed = []
    for kind, names, attr in (("family", families, "family"), ("domain", domains, "domain")):
        for name in names:
            units = [(t.task_id, s) for t in tasks if t.split == "gate" and getattr(t, attr) == name
                     for s in cfg.seeds]
            r = non_regression(sample(units), non_regression_eps=cfg.non_regression_eps,
                               max_excluded_frac=cfg.max_excluded_frac)
            out["families" if kind == "family" else "domains"][name] = r
            out["ood_drop"][f"{kind}:{name}"] = r["mean_diff"]
            if not r["passed"]:
                failed.append(f"held-out {kind} {name}: {r['reason']}")
    out["passed"] = not failed
    out["reason"] = "; ".join(failed) if failed else "every held-out family/domain non-regressing"
    return out


def proposer_calibration(store: EvidenceStore) -> dict[str, float]:
    """Mean absolute error and sign agreement between predicted and measured edit effects."""
    rows = [r for r in store.records("harness_edit") if r.get("actual_evolve_delta") is not None]
    if not rows:
        return {"n": 0}
    errs = [abs(r["predicted_delta"] - r["actual_evolve_delta"]) for r in rows]
    sign = [(r["predicted_delta"] > 0) == (r["actual_evolve_delta"] > 0) for r in rows]
    return {"n": len(rows), "mae": sum(errs) / len(errs), "sign_agreement": sum(sign) / len(sign)}
