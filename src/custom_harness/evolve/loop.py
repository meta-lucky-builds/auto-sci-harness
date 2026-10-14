"""One harness-evolution generation (PLAN L6 pipeline):

parent evaluated on evolve split -> diagnose -> propose typed edits -> static checks ->
race candidates on evolve split -> RE-MEASURE the race winner and the parent on fresh runs (fresh seeds,
same evolve tasks) -> gate on (fresh evolve evidence, gate split) -> leave-one-family-out (and, with >= 2 domains,
leave-one-domain-out) non-regression on EACH held-out family/domain -> record lineage (incl. rejects),
harness-science ledger (incl. per-group results and OOD drop, key "lofo") and proposer calibration.
A campaign without a held-out family (/domain) is refused (GOAL invariant 16, fail closed).

M4 pieces (GOAL §5 L6.3/6.6/6.8/6.10): the diagnosis carries first-divergence causal signatures from evolve-split
runs; an edit whose citations do not resolve in what the proposer saw, or whose claimed change is not the real diff
of its ops, is rejected unevaluated (`claims_check`, status `rejected_claims`, claims.py); an edit whose normalised
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
import threading
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
from .branches import BranchConfig, propose_branch
from .claims import check_claims, digest_refs
from .critic import FamilySeparatedCritic
from .edits import (
    DEFAULT_PROTECTED_NODES,
    Edit,
    EditRejected,
    ScreenRejected,
    _token_hit,
    apply_edit,
    check_one_mechanism,
    denied_tokens,
    screen_prediction,
    withheld,
)
from .evaluate import (
    DURABLE_KIND,
    Attempt,
    ExecutedRun,
    RunOutcome,
    SandboxFactory,
    SuiteTask,
    check_held_out,
    execute_run,
    finish_run,
)
from .evaluate import outcome_from_record as _outcome_from
from .evaluate import outcome_to_record as _outcome_to
from .export import proposer_view, screen_digest
from .multiplicity import check_run, last_prereg_seq
from .propose import diagnose, propose
from .proposer_agent import ProposerAgent
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
# A smoke failure is included (the candidate broke where the parent did not). Infra-failed smoke runs are not
# rejections at all (_smoke_failure); older lineage records marked `infra` are still skipped below.
REJECTED_STATUSES = ("rejected_static", "rejected_gate", "rejected_smoke")
# smoke run (GOAL §5 L6.7): run statuses meaning the harness itself broke on the smoke task
SMOKE_BROKEN = ("failed", "step_limit", "budget_exceeded")
# screened edit fields whose dropped items are unchecked claims (claims.py): a drop rejects the edit
UNVERIFIABLE_IF_DROPPED = ("evidence", "failure_clusters_fixed", "claimed_change")


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
    # prediction screen (edits.screen_prediction, GOAL §5 L6.2): bounds on proposer free text and on the predicted
    # delta (the objective per run lies in [-0.5, 1.5], so a paired delta lies in [-2, 2]). A bandwidth limit on the
    # ledger channel, not claim policy.
    max_rationale_chars: int = 2000
    max_expected_delta: float = 2.0
    delta_decimals: int = 3
    # race allocation (GOAL §5 L1; control/allocator.py, evalrace.py, voi.py). "batch" = selection.race in a loop
    # (default). "per_completion" = the completion-driven allocator over the single-writer Evaluator; with
    # race_slots=1 it evaluates in exactly the batch order. race_slots > 1 runs harness runs concurrently: refused
    # unless race_concurrency_ack (an owner decision: unit scores depend on Kernel ledger history, so concurrent
    # orders are not shown to give the batch race's decisions; docs/control_allocation.md).
    race_allocator: str = "batch"
    race_slots: int = 1
    race_concurrency_ack: bool = False
    race_timeout_s: float = 3600.0


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
    # L6.9: the `branch` record created this generation (a gate-rejected candidate that won a cluster), or None
    branch: dict[str, Any] | None = None


class Evaluator:
    """Runs (spec, task, seed) and caches outcomes so parent results are reused across stages."""

    def __init__(self, backends: dict[str, Backend], store: EvidenceStore, sandbox_factory: SandboxFactory,
                 tasks: list[SuiteTask], budget_per_run: dict[str, float | None] | None = None,
                 infra_retries: int = 1, retry: RetryPolicy | None = None,
                 campaign_budget: Budget | None = None, releases: Releases | None = None,
                 durable_key: str | None = None):
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
        # SINGLE WRITER (control/evalrace.py): every mutation of the state above (cache, counters, budget charges,
        # durable unit records, releases) happens in begin()/finish() on ONE thread, the first that calls them.
        # Only attempt() (the harness run itself) may run on other threads. A second writer thread is refused.
        self._writer: int | None = None
        self._in_flight: set[tuple[str, str, int]] = set()
        # durable unit outcomes (crash/resume): with a key, every finished unit is appended to the store AFTER its
        # charge, and a new Evaluator with the same key starts from them (never re-run, never charged again)
        self.durable_key = durable_key
        if durable_key is not None:
            for rec in store.records_where(DURABLE_KIND, "durable_key", durable_key):
                self.cache[(rec["spec_hash"], rec["task_id"], int(rec["seed"]))] = _outcome_from(rec["outcome"])

    def outcome(self, spec: HarnessSpec, unit: Unit) -> RunOutcome:
        """A unit whose run failed for infrastructure reasons is re-run (fresh run, same seed) up to
        `infra_retries` times; if it still fails it stays excluded (score None)."""
        key = (spec.spec_hash(), unit[0], unit[1])
        n = 0
        while key not in self.cache:
            att = self.begin(spec, unit, n)
            try:
                ex = self.attempt(att)
            except BaseException:
                self.abandon(att)
                raise
            self.finish(att, ex)
            n += 1
        return self.cache[key]

    # ---------------------------------------------------------------- single-writer protocol (evalrace.py)
    def _own(self) -> None:
        me = threading.get_ident()
        if self._writer is None:
            self._writer = me
        elif self._writer != me:
            raise RuntimeError("Evaluator state has a single writer thread; run only attempt() on other threads "
                               "(control/evalrace.py)")

    def begin(self, spec: HarnessSpec, unit: Unit, n: int = 0) -> Attempt:
        """Writer thread: start attempt `n` of a unit (campaign budget check first, as before every run)."""
        self._own()
        key = (spec.spec_hash(), unit[0], unit[1])
        if key in self.cache:
            raise RuntimeError(f"unit {key} is already evaluated")
        if n == 0 and key in self._in_flight:
            raise RuntimeError(f"unit {key} is already being evaluated (a duplicate run would be charged twice)")
        if self.campaign_budget is not None:
            try:
                self.campaign_budget.check()
            except BaseException:
                self._in_flight.discard(key)
                raise
        self._in_flight.add(key)
        self.calls += 1
        return Attempt(key, spec, unit, n, last_prereg_seq(self.store))

    def attempt(self, att: Attempt) -> ExecutedRun:
        """Any thread: the harness run itself (no Evaluator state is touched)."""
        return execute_run(att.spec, self.tasks[att.unit[0]], att.unit[1], backends=self.backends, store=self.store,
                           sandbox_factory=self.sandbox_factory, budget_limits=self.budget_per_run, retry=self.retry)

    def finish(self, att: Attempt, ex: ExecutedRun) -> RunOutcome | None:
        """Writer thread: score, check, charge and record attempt `att`. Returns the unit's final outcome, or None
        when it failed for infrastructure reasons and has a retry left (the caller begins attempt att.n + 1)."""
        self._own()
        out = finish_run(ex, store=self.store, releases=self.releases)
        # R2-2: every claim this run certified must have used m = its declared n_comparisons (constant
        # strictness, independent of evaluation order); otherwise MultiplicityDrift stops the campaign
        try:
            check_run(self.store, att.since)
        except BaseException:
            self._in_flight.discard(att.key)
            raise
        if self.campaign_budget is not None:
            try:
                self.campaign_budget.charge(out.cost)
            except BudgetExceeded:
                pass  # recorded; the next check() stops the campaign before any further run
        if out.infra_failed:
            self.infra_failures += 1
            if att.n < self.infra_retries:
                return None
        self._in_flight.discard(att.key)
        if self.durable_key is not None:  # after the charge: a crash in between re-runs the unit (a real, second
            # run, charged once itself); a recorded unit is never re-run nor charged again
            self.store.append_record(DURABLE_KIND, {"durable_key": self.durable_key, "spec_hash": att.key[0],
                                                    "task_id": att.key[1], "seed": att.key[2],
                                                    "outcome": _outcome_to(out)})
        self.cache[att.key] = out
        return out

    def abandon(self, att: Attempt) -> None:
        """Writer thread: an attempt whose run raised (no outcome): it is no longer in flight."""
        self._own()
        self._in_flight.discard(att.key)

    def score(self, spec: HarnessSpec, unit: Unit) -> float | None:
        return self.outcome(spec, unit).score


def evolve_generation(parent: HarnessSpec, tasks: list[SuiteTask], *, evaluator: Evaluator,
                      proposer: Backend | ProposerAgent, store: EvidenceStore, cfg: EvolveConfig | None = None,
                      releases: Releases | None = None,
                      critic: FamilySeparatedCritic | None = None,
                      branching: BranchConfig | None = None) -> GenerationResult:
    """`proposer`: a Backend (one-shot proposer, propose.py) or a ProposerAgent (agentic: reads the evidence export
    in a sandbox). `critic`: optional family-separated critic (critic.py), refused before any model call when it
    shares the proposer's family; its review is advisory (recorded, never gating). `branching`: when given, a
    candidate rejected on its evolve re-measurement or the held-out gate is tested per gate cluster and may become
    a branch (branches.py; its cluster bits are ledgered releases); None = no branching (unchanged behaviour)."""
    cfg = cfg or EvolveConfig()
    check_race_config(cfg)  # fail closed before any run
    if critic is not None:
        critic.check_proposer(proposer.backend if isinstance(proposer, ProposerAgent) else proposer)
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
    digest = diagnose(parent_outcomes, store, references=references, evolve_ids=evolve_ids)
    # the prompt carries the digest: the export's leak audit applies to it too (fail closed, export.screen_digest)
    digest = screen_digest(digest, store, runs=[*parent_outcomes, *references], tasks=tasks,
                           canaries=getattr(proposer, "canaries", ()))
    ledger = [proposer_view(r, evolve_ids) for r in store.records("harness_edit")]

    if isinstance(proposer, ProposerAgent):
        proposal: Any = proposer.propose_from_export(
            parent, digest, ledger, store=store, tasks=tasks, runs=[*parent_outcomes, *references],
            n_edits=cfg.n_edits, protected=cfg.protected_nodes)
        store.append_record("proposer_session", {"parent": parent_hash, "transcript": proposal.transcript,
                                                 "problems": proposal.problems})
    else:
        proposal = propose(proposer, parent, digest, ledger, n_edits=cfg.n_edits, protected=cfg.protected_nodes)
    edits, problems = proposal.edits, proposal.problems
    # what the proposer was shown and may cite (claims.py): the agentic session's export + re-linked diagnosis, or the
    # one-shot proposer's diagnosis
    shown = getattr(proposal, "evidence_refs", None)
    known = set(shown) if shown is not None else digest_refs(digest)
    claims_by: dict[str, dict[str, Any]] = {}
    rejected_before = rejected_signatures(store)
    deny = denied_tokens(tasks)
    task_ids = {t.task_id for t in tasks}
    candidates: dict[str, tuple[Edit, HarnessSpec]] = {}
    extras: dict[str, dict[str, Any]] = {}  # per candidate: screen notes and the (advisory) critic review
    records: list[dict[str, Any]] = [{"status": "malformed", "problem": p} for p in problems]
    if proposal.decline is not None:
        # proposer free text written to the lineage (exported): screened like an edit's rationale
        reason = proposal.decline if not _token_hit(proposal.decline, deny) else "<withheld: names held-out data>"
        records.append({"status": "declined", "reason": reason})
    for edit in edits:
        sig = edit_signature(edit)
        try:  # covert-channel / leak screen on the edit's text and predicted effect, before anything is stored
            edit, notes = screen_prediction(edit, task_ids=task_ids, deny=deny, max_text_chars=cfg.max_rationale_chars,
                                            max_delta=cfg.max_expected_delta, delta_decimals=cfg.delta_decimals)
        except ScreenRejected as e:  # the refused text is never stored (withheld); a text-only refusal does not
            # feed the novelty gate (the same change re-worded is a different proposal), an ops refusal does
            if sig in rejected_before:
                records.append({"status": "rejected_novelty", "edit": withheld(edit), "signature": sig,
                                "reason": "identical (normalised) to a previously rejected edit"})
            else:
                records.append({"status": "rejected_static", "edit": withheld(edit), "reason": str(e),
                                **({"signature": sig} if e.about_ops else {})})
            continue
        base = {"edit": _public(edit, evolve_ids), "signature": sig, "claims_check": check_claims(edit, refs=known),
                **({"screen": notes} if notes else {})}
        if sig in rejected_before:
            records.append({"status": "rejected_novelty", **base,
                            "reason": "identical (normalised) to a previously rejected edit"})
            continue
        # a citation or claimed path the screen DROPPED (too long, too many, not an id) can never be checked: the edit
        # is refused rather than judged on what is left (fail closed; the notes quote no proposer text)
        unverifiable = [n for n in notes if n.split(":")[0] in UNVERIFIABLE_IF_DROPPED and "dropped" in n]
        if unverifiable:
            records.append({"status": "rejected_claims", **base,
                            "reason": "unverifiable claims: " + "; ".join(unverifiable)})
            continue
        try:
            spec = apply_edit(parent, edit, cfg.protected_nodes)
            check_one_mechanism(edit, parent)  # GOAL §5 L6.6: one mechanism per candidate
        except EditRejected as e:
            records.append({"status": "rejected_static", **base, "reason": str(e)})
            continue
        h = spec.spec_hash()
        if h == parent_hash or h in candidates:
            records.append({"status": "no_op_or_duplicate", **base})
            continue
        # GOAL §5 L6.3, enforced: citations resolve in what the proposer saw AND the claimed change is the real diff.
        # Not a novelty rejection (the same ops honestly claimed are a new proposal): no REJECTED_STATUSES entry.
        base["claims_check"] = claims_by[h] = check_claims(
            edit, refs=known, before=parent.model_dump(mode="json"), after=spec.model_dump(mode="json"))
        if not claims_by[h]["passed"]:
            records.append({"status": "rejected_claims", **base, "reason": claims_by[h]["reason"]})
            continue
        if critic is not None:
            base["critic"] = critic.review(edit).as_record()
        store.add_harness_version(spec.id, spec.model_dump(mode="json"), parent_hash=parent_hash)
        # smoke run (GOAL §5 L6.7): the candidate runs the first evolve unit through the real loop before racing.
        # That unit is in the race's first chunk, so a passing smoke run costs nothing extra (the Evaluator caches it).
        smoke = evaluator.outcome(spec, evolve_units[0])
        smoke_fail = _smoke_failure(smoke, parent_outcomes[0])
        if smoke_fail is not None:
            records.append({"status": "rejected_smoke", **base, "candidate": h, "reason": smoke_fail,
                            "infra": smoke.infra_failed, "smoke": {"task": smoke.task_id, "seed": smoke.seed,
                                                                   "status": smoke.status, "run_id": smoke.run_id}})
            continue
        candidates[h] = (edit, spec)
        extras[h] = {k: base[k] for k in ("screen", "critic") if k in base}
        if smoke.infra_failed:  # excluded, not scored (invariant 21: a proxy denies only on evidence): ledgered here
            extras[h]["smoke"] = {"excluded": "infra", "task": smoke.task_id, "seed": smoke.seed,
                                  "status": smoke.status, "run_id": smoke.run_id}

    accepted_spec, accepted = parent, False
    decision = f"proposer declined: {proposal.decline}" if proposal.decline is not None else "no valid candidates"
    survivors: list[Any] = []
    loop_label = "MECHANICAL"
    fixed_k = 0
    branch: dict[str, Any] | None = None
    if candidates:
        rr = _race(candidates, evolve_units, evaluator, parent_scores, cfg)
        fixed_k = rr.evaluations_fixed_k
        survivors = list(rr.survivors)
        for h, (edit, _) in candidates.items():
            st = rr.stats.get(h, {})
            records.append({"status": "raced", "candidate": h, "edit": _public(edit, evolve_ids), "race": st,
                            "survived": h in rr.survivors, "signature": edit_signature(edit), **extras[h]})
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
            # infra_aborted: too many units were lost to infrastructure failures, so there is no verdict at all
            # (INCONCLUSIVE, owner decision 2026-10-08; GOAL L1 "excluded, not scored"). Never accepted, but not a
            # policy rejection either: no rejected_on stage, no novelty block, no branch.
            infra_aborted = False
            if not excl["passed"]:  # the comparison itself is untrustworthy: checked first (fail closed)
                decision, rejected_on = "rejected: " + excl["reason"], "exclusion_asymmetry"
            elif not d.accepted and d.infra_aborted:
                decision, rejected_on, infra_aborted = "inconclusive: " + d.reason, None, True
            elif not d.accepted:
                decision = d.reason
                rejected_on = "evolve" if d.reason.startswith("evolve") else "held_out_gate"
            elif not lofo["passed"] and lofo.get("infra_aborted"):
                decision, rejected_on, infra_aborted = "inconclusive: " + lofo["reason"], None, True
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
            at_risk = set(edit.predicted.tasks_at_risk)  # screened: exact campaign task ids only
            claims = claims_by[best]
            # every unit the acceptance decision rested on, BOTH arms: the fresh evolve re-measurement and the gate
            # (R2-4: the gate alone made a decision on internal evolve evidence look VALIDATED)
            used_labels = [evaluator.outcome(x, u).cert_label for x in (spec, parent) for u in fresh_units + gate_units]
            loop_label = "VALIDATED" if all(lb == "CERTIFIED-VALIDATED" for lb in used_labels) else "MECHANICAL"
            records.append({"status": "accepted" if ok else ("infra_aborted" if infra_aborted else "rejected_gate"),
                            "candidate": best,
                            "loop_label": loop_label,
                            "edit": _public(edit, evolve_ids), "race": rr.stats.get(best, {}),
                            "evolve_fresh": d.evolve,
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
                "cost": cost, "exclusions": excl, "rejected_on": rejected_on, "infra_aborted": infra_aborted,
                "signature": edit_signature(edit), "per_task": per_task, "flipped": flipped, "regressed": regressed,
                "tasks_at_risk": sorted(at_risk), "regressed_unpredicted": [t for t in regressed if t not in at_risk]})
            if ok:
                accepted_spec, accepted = spec, True
            elif branching is not None and rejected_on in ("evolve", "held_out_gate"):
                branch = propose_branch(parent, spec, tasks, seeds=cfg.seeds, outcome=evaluator.outcome,
                                        store=store, releases=rel, globally_accepted=False, cfg=branching)
    for r in records:
        store.append_record("lineage", {"parent": parent_hash, **r})
    counters = _counters(records, n_proposed=len(edits) + len(problems), n_raced=len(candidates),
                         n_survivors=len(survivors))
    store.append_record("generation_stats", {"parent": parent_hash, **counters})
    return GenerationResult(parent_hash, accepted_spec, accepted, decision, records,
                            evaluator.calls - calls_before, fixed_k, loop_label, counters, branch)


RACE_ALLOCATORS = ("batch", "per_completion", "voi")


def check_race_config(cfg: EvolveConfig) -> None:
    """Fail closed on a race configuration that is unknown or not shown to keep the batch race's decisions."""
    if cfg.race_allocator not in RACE_ALLOCATORS:
        raise ValueError(f"unknown race_allocator {cfg.race_allocator!r} (known: {RACE_ALLOCATORS})")
    if isinstance(cfg.race_slots, bool) or not isinstance(cfg.race_slots, int) or cfg.race_slots < 1:
        raise ValueError("race_slots must be an integer >= 1")
    if cfg.race_slots > 1 and cfg.race_allocator != "per_completion":
        raise ValueError("race_slots > 1 needs race_allocator='per_completion'")
    if cfg.race_slots > 1 and not cfg.race_concurrency_ack:
        raise ValueError("race_slots > 1 runs harness runs concurrently, whose scores depend on Kernel ledger order "
                         "(K4 transcript bound): decisions equal to the batch race are not shown. Refused unless "
                         "race_concurrency_ack=True (owner decision; docs/control_allocation.md)")


def _race(candidates: dict[str, tuple[Edit, HarnessSpec]], units: list[Unit], evaluator: Evaluator,
          parent_scores: dict[Unit, float | None], cfg: EvolveConfig) -> Any:
    if cfg.race_allocator == "per_completion":
        from ..control.evalrace import race_evaluator  # local: control/ imports evolve.selection
        rr, _alloc, _ex = race_evaluator({h: spec for h, (_, spec) in candidates.items()}, units, evaluator,
                                         parent_scores, slots=cfg.race_slots, rounds=cfg.race_rounds, keep=1,
                                         timeout_s=cfg.race_timeout_s)
        return rr
    if cfg.race_allocator == "voi":  # selection order/allocation only; the acceptance rule below is unchanged
        from ..control.voi import voi_race
        return voi_race(list(candidates), units, lambda h, u: evaluator.score(candidates[str(h)][1], u),
                        parent_scores, rounds=cfg.race_rounds, keep=1)
    return race(list(candidates), units, lambda h, u: evaluator.score(candidates[str(h)][1], u),
                parent_scores, rounds=cfg.race_rounds, keep=1)


def _public(edit: Edit, evolve_ids: set[str]) -> dict[str, Any]:
    """An edit as the lineage (exported to the proposer) stores it: tasks_at_risk restricted to evolve tasks. The
    harness_edit ledger keeps the full list (it computes `regressed_unpredicted`; never exported raw)."""
    doc = edit.model_dump()
    doc["predicted"]["tasks_at_risk"] = [t for t in doc["predicted"]["tasks_at_risk"] if t in evolve_ids]
    return doc


def _smoke_failure(cand: RunOutcome, parent: RunOutcome) -> str | None:
    """Why a candidate fails its smoke run, or None. It fails when its run ended broken (SMOKE_BROKEN) on a unit where
    the parent's run did not. A smoke run that failed for infrastructure reasons is EXCLUDED, not a rejection (GOAL
    L1: "an infrastructure failure means excluded, not scored"; an outage must change no accept/reject decision):
    the candidate goes on to the race, which excludes that unit, and the gate fails closed when too many units are
    excluded. (Until 2026-10-08 it was rejected, which let an outage change decisions.)"""
    if cand.infra_failed:
        return None
    if cand.status in SMOKE_BROKEN and parent.status not in SMOKE_BROKEN:
        sig = "; ".join(cand.failure_signatures[:2])
        return f"smoke run ended {cand.status!r} where the parent's did not ({sig[:300]})"
    return None


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
        if r.get("status") not in REJECTED_STATUSES or (r.get("status") == "rejected_smoke" and r.get("infra")):
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
        "malformed", "declined", "rejected_novelty", "rejected_static", "no_op_or_duplicate", "rejected_claims",
        "rejected_smoke", "accepted", "rejected_gate", "infra_aborted")}
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
    groups = [*out["families"].values(), *out["domains"].values()]
    # inconclusive only when EVERY failing group failed on infrastructure exclusions (a policy failure wins)
    out["infra_aborted"] = bool(failed) and all(r.get("infra_aborted") for r in groups if not r["passed"])
    return out


def proposer_calibration(store: EvidenceStore) -> dict[str, float]:
    """Mean absolute error and sign agreement between predicted and measured edit effects."""
    rows = [r for r in store.records("harness_edit") if r.get("actual_evolve_delta") is not None]
    if not rows:
        return {"n": 0}
    errs = [abs(r["predicted_delta"] - r["actual_evolve_delta"]) for r in rows]
    sign = [(r["predicted_delta"] > 0) == (r["actual_evolve_delta"] > 0) for r in rows]
    return {"n": len(rows), "mae": sum(errs) / len(errs), "sign_agreement": sum(sign) / len(sign)}
