"""L7 search over search, wired (GOAL §5 L7): islands with their own lineages, run through the real generation loop
under the deterministic orchestrator of control/islands.py.

`evolve_islands` runs `rounds` orchestrator rounds. Per round:
  1. the previous round is closed (a policy-trial round records its outcome; a completed trial records its
     decision), an accepted policy change is applied when the cadence allows, and a declared challenger's trial is
     declared as soon as `Orchestrator.trial_refusal` allows it (control/policy_gate.py);
  2. `Orchestrator.step` decides every island's lever, budget, seeds, edit count and edit-type plan; with a
     `controller` (an LLM Backend) each island's budget is first exposed as Budget(limits={"quota_units": budget})
     to a DowngradeGate (control/downgrade.py): the controller may only SHRINK it (anything else is rejected and
     ledgered by the gate), and the orchestrator applies min(its budget, the shrunk cap). No model sets a budget in
     any other way;
  3. islands run SEQUENTIALLY in id order (the generation ledger attributes records by snapshot: never interleave).
     An island with 0 edits is skipped (logged); a `weights` decision calls the `weights_export` hook (the caller
     binds it to weights.export.export_trajectories with its declared inputs) and runs no harness generation: the
     lever ROUTES to the L8 data export only, NOTHING IS TRAINED (no GPU; a weight update is a human decision). Any
     other island runs `evolve_generation` on its own parent with n_edits = min(decision n_edits, cfg.n_edits),
     its decided number of seeds, a proposer picked deterministically from its proposer mix (smooth weighted round
     robin) and the edit-type quota enforced in the loop (`edit_admission` = islands.admit under the round's
     policy). Each island has its own generation-ledger campaign f"{campaign}/{island}";
  4. after each generation the orchestrator observes a GenerationSignal built ONLY by `signal_from_generation`
     from the generation's records through the proposer-visible views (verdict embargo, invariant 18).

Before anything runs: the sealed split is filtered out; every island's proposer mix must name only given proposers;
`check_campaign` is called once with a runs-per-task bound covering the worst case (rounds x islands generations,
cfg.n_edits edits, max_seeds race seeds and the matching fresh seeds, cfg.infra_retries); refusal stops the run.

Honest limits: the `generation_ledger` record's `budget` field is the per-campaign declared cap (a constant
schedule at cfg.n_edits); the n_edits an island's proposer actually got is in that round's `orchestrator_decision`
record (and `generation_stats.proposed` counts what it proposed). The decision, trial and controller records are
EvidenceStore records (`append_record`), not Kernel ledger kinds.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .. import config
from ..budget import Budget, BudgetExceeded
from ..control.downgrade import AdmissionRegister, DowngradeGate
from ..control.islands import Island, IslandError, Orchestrator, OrchestratorPolicy, admit, signal_from_generation
from ..gateway import Backend
from ..runtime.spec import HarnessSpec
from ..store import EvidenceStore
from .edits import Edit
from .evaluate import SandboxFactory, SuiteTask
from .generations import open_campaign
from .loop import Evaluator, EvolveConfig, evolve_generation
from .multiplicity import check_campaign, runs_per_task_bound

WEIGHTS_ROUTE_KIND = "weights_route"
ISLAND_SKIP_KIND = "island_skip"


@dataclass(frozen=True)
class TrialParams:
    """A declared policy trial (control/policy_gate.py); defaults are the UNCALIBRATED config values."""
    order_seed: str
    n_pairs: int = config.POLICY_TRIAL_PAIRS
    alpha: Any = config.POLICY_TRIAL_ALPHA
    min_nonzero: int = config.POLICY_TRIAL_MIN_NONZERO
    trial_id: str | None = None


@dataclass
class SearchResult:
    parents: dict[str, HarnessSpec]
    decisions: list[dict[str, Any]] = field(default_factory=list)
    trial_decisions: list[dict[str, Any]] = field(default_factory=list)
    policy_version: str = ""
    stopped: str | None = None


class ProposerPicker:
    """Smooth weighted round robin over one island's proposer mix: deterministic, and over any window of
    sum-of-weights picks each proposer appears in proportion to its weight (ties broken by name)."""

    def __init__(self, mix: Mapping[str, float]):
        self.mix = {k: float(v) for k, v in sorted(mix.items()) if v > 0}
        self.current = {k: 0.0 for k in self.mix}

    def pick(self) -> str:
        total = sum(self.mix.values())
        for k, w in self.mix.items():
            self.current[k] += w
        best = max(self.current, key=self.current.__getitem__)  # first maximum in name order
        self.current[best] -= total
        return best


def island_seeds(cfg: EvolveConfig, n: int) -> tuple[int, ...]:
    """The first n race seeds of an island: cfg.seeds, then the next integers that are neither in cfg.seeds nor in
    cfg.remeasure_seeds (the more-evaluation lever only ever ADDS seeds)."""
    if n < len(cfg.seeds):
        raise ValueError("an island never runs on fewer seeds than cfg.seeds")
    out = list(cfg.seeds)
    avoid = set(cfg.seeds) | set(cfg.remeasure_seeds or ())
    nxt = max(cfg.seeds) + 1
    while len(out) < n:
        if nxt not in avoid:
            out.append(nxt)
        nxt += 1
    return tuple(out)


def _refuse_kill(run_id: str, code: str) -> None:
    raise RuntimeError("the L7 controller cannot kill runs")  # the gate records an effect_error rejection


def _controller_cap(store: EvidenceStore, controller: Backend, controller_id: str, round_no: int
                    ) -> Callable[[dict[str, int]], dict[str, float | None]]:
    def cap(budgets: dict[str, int]) -> dict[str, float | None]:
        live = {iid: Budget(limits={"quota_units": float(b)}) for iid, b in sorted(budgets.items())}
        gate = DowngradeGate(store, budgets=live, admissions=AdmissionRegister(), kill=_refuse_kill)
        # code-built observation: the round number and each island's cap (counts only; no gate bit or score)
        obs = json.dumps({"round": round_no, "budgets": {iid: {"unit": "quota_units", "cap": float(b)}
                                                         for iid, b in sorted(budgets.items())},
                          "allowed": "shrink only"}, sort_keys=True)
        gate.ask(controller_id, controller, obs)
        return {iid: bud.limits.get("quota_units") for iid, bud in live.items()}
    return cap


def _summary(res: Any) -> dict[str, Any]:
    """What the store keeps of a weights-export result: its manifest counts and destination, if it has them."""
    man = getattr(res, "manifest", None)
    if isinstance(man, dict):
        return {"format": man.get("format"), "kept": man.get("kept"), "excluded": man.get("excluded"),
                "path": str(getattr(res, "path", ""))}
    return {"result_type": type(res).__name__}


def evolve_islands(seed_spec: HarnessSpec, suite: list[SuiteTask], *, islands: list[Island],
                   policy: OrchestratorPolicy, rounds: int, total_budget: int, proposers: Mapping[str, Backend],
                   backends: dict[str, Backend], store: EvidenceStore, sandbox_factory: SandboxFactory,
                   cfg: EvolveConfig, campaign: str, controller: Backend | None = None,
                   weights_export: Callable[[str, dict[str, Any]], Any] | None = None,
                   challenger: OrchestratorPolicy | None = None, trial: TrialParams | None = None,
                   campaign_budget: Budget | None = None, log: Callable[[str], None] = print) -> SearchResult:
    """Run the L7 islands search (module docstring). Refuses (ValueError/IslandError/MultiplicityDrift) before any
    run on: rounds < 1, a mix naming an unknown proposer, max_seeds below len(cfg.seeds), a trial without a
    challenger, or a campaign whose claim nodes do not declare the worst-case multiplicity bound."""
    if type(rounds) is not int or rounds < 1:
        raise ValueError("rounds must be an integer >= 1")
    if not isinstance(campaign, str) or not campaign:
        raise ValueError("a campaign name is required (each island gets its own generation ledger)")
    if trial is not None and challenger is None:
        raise ValueError("a trial needs a challenger policy")
    for isl in islands:
        unknown = sorted(set(isl.proposer_mix) - set(proposers))
        if unknown:
            raise IslandError(f"island {isl.island_id}: proposer mix names unknown proposer(s) {unknown}")
    pols = [policy] + ([challenger] if challenger is not None else [])
    max_seeds = max(p.max_seeds for p in pols)
    if min(p.max_seeds for p in pols) < len(cfg.seeds):
        raise ValueError("policy max_seeds is below len(cfg.seeds)")
    working = [t for t in suite if t.split != "sealed"]
    n_fresh = len(cfg.remeasure_seeds) if cfg.remeasure_seeds is not None else max_seeds
    bound = check_campaign(seed_spec, store, [t.task_dir for t in working], runs_per_task=runs_per_task_bound(
        generations=rounds * len(islands), n_edits=cfg.n_edits, n_seeds=max_seeds, n_fresh_seeds=n_fresh,
        infra_retries=cfg.infra_retries))
    log(json.dumps({"multiplicity_bound": bound}))
    orch = Orchestrator(islands, policy, total_budget=total_budget, store=store, initial_seeds=len(cfg.seeds),
                        weights_lever=weights_export is not None)
    ledgers = {isl.island_id: open_campaign(store, f"{campaign}/{isl.island_id}", None, cfg)
               for isl in sorted(islands, key=lambda i: i.island_id)}
    pickers = {isl.island_id: ProposerPicker(isl.proposer_mix) for isl in islands}
    evaluator = Evaluator(backends, store, sandbox_factory, working, cfg.budget_per_run,
                          infra_retries=cfg.infra_retries, campaign_budget=campaign_budget)
    parents = {isl.island_id: seed_spec for isl in islands}
    result = SearchResult(parents)
    declared = False
    pending: dict[str, Any] | None = None

    def settle() -> None:
        nonlocal pending
        for dec in orch.trial_decisions.values():
            if dec not in result.trial_decisions:
                result.trial_decisions.append(dec)
                log(json.dumps({"policy_trial_decision": {k: dec[k] for k in (
                    "trial_id", "n_plus", "n_minus", "n_zero", "p", "accepted")}}))
                if dec["accepted"] and challenger is not None:
                    pending = dec
        if pending is not None and challenger is not None and \
                orch.round - orch.policy_changed_at >= orch.policy.cadence:
            orch.set_policy(challenger, trial_decision=pending)
            log(json.dumps({"policy_changed": challenger.version, "round": orch.round}))
            pending = None

    for _ in range(rounds):
        orch.close_round()
        settle()
        if challenger is not None and not declared and orch.trial_refusal(challenger) is None:
            tp = trial or TrialParams(order_seed=campaign)
            orch.declare_trial(challenger, order_seed=tp.order_seed, trial_id=tp.trial_id, n_pairs=tp.n_pairs,
                               alpha=tp.alpha, min_nonzero=tp.min_nonzero)
            declared = True
        cap = _controller_cap(store, controller, f"l7-controller:{campaign}", orch.round + 1) \
            if controller is not None else None
        decisions = orch.step(cap=cap)
        result.decisions += decisions
        round_policy = orch.round_policy

        def admission(edits: list[Edit], _p: OrchestratorPolicy = round_policy) -> tuple[list[int], list[int]]:
            return admit([e.type.value for e in edits], _p)

        for d in decisions:  # sorted by island id
            iid = d["island"]
            if d["lever"] == "weights":
                out = weights_export(iid, d) if weights_export is not None else None
                store.append_record(WEIGHTS_ROUTE_KIND, {"island": iid, "round": d["round"],
                                                         "policy_version": d["policy_version"],
                                                         "trained": False, "export": _summary(out)})
                log(json.dumps({"round": d["round"], "island": iid, "lever": "weights", "trained": False}))
                orch.skip(iid)
                continue
            n_edits = min(d["n_edits"], cfg.n_edits)
            if n_edits < 1:
                store.append_record(ISLAND_SKIP_KIND, {"island": iid, "round": d["round"], "reason": "0 edits"})
                log(json.dumps({"round": d["round"], "island": iid, "skipped": "0 edits this round"}))
                orch.skip(iid)
                continue
            ledger = ledgers[iid]
            assert ledger is not None  # open_campaign with a campaign name always returns a ledger
            gcfg = dataclasses.replace(ledger.begin(cfg), n_edits=n_edits, seeds=island_seeds(cfg, d["n_seeds"]))
            name = pickers[iid].pick()
            try:
                res = evolve_generation(parents[iid], working, evaluator=evaluator, proposer=proposers[name],
                                        store=store, cfg=gcfg, edit_admission=admission)
            except BudgetExceeded as e:
                result.stopped = f"campaign budget exhausted: {e}"
                log(json.dumps({"round": d["round"], "island": iid, "stopped": result.stopped}))
                break
            led = ledger.record(res)
            lo, hi = led["refs"]["lineage"]
            sig = signal_from_generation(iid, led["generation"],
                                         store.records("generation_stats")[led["refs"]["generation_stats"]],
                                         store.records("lineage")[lo:hi])
            orch.observe(sig)
            parents[iid] = res.new_parent
            log(json.dumps({"round": d["round"], "island": iid, "proposer": name, "n_edits": n_edits,
                            "accepted": res.accepted, "lever": d["lever"]}))
        if result.stopped is not None:
            break
    orch.close_round()
    settle()
    abandoned = orch.abandon_trial()
    if abandoned is not None:
        result.trial_decisions.append(abandoned)
        log(json.dumps({"policy_trial_abandoned": abandoned["trial_id"], "reason": abandoned["reason"]}))
    result.parents = parents
    result.policy_version = orch.policy.version
    return result
