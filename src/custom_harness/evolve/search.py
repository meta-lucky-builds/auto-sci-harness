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

Run path (s4-wiring): `chh evolve SPEC --suite SUITE --islands ISLANDS.yaml` (explicit opt-in campaign mode;
`load_islands_config` reads and checks the file, closed keys, fail closed). The `weights` lever is NOT offered there:
the owner decided no model training (GOAL L8), the lever may only route to the trajectory export, and no run produces
trajectories for it, so the CLI binds no `weights_export` hook (the orchestrator then never chooses the lever).

Ablation (custom_harness/ablation.py; the AblationSet only the scorecard runner's entry point makes): `ablation` is
threaded into the Orchestrator (its `orchestrator-reallocation` / `lever-choice` switches and `:ABLATED` decision
records), the Evaluator, every generation and every island's campaign ledger (`:ABLATED` kinds, generations.py).
Refused under an ablation (fail closed, their records have no ablated kind): a `controller` (DowngradeGate records)
and a `weights_export` hook. Stated limit: an ablated generation never accepts, so an arm's islands all stall after
the window and never see a gated rejection; the L7 switches then change only the stall decisions' budget and mix.

Honest limits: the `generation_ledger` record's `budget` field is the per-campaign declared cap (a constant
schedule at cfg.n_edits); the n_edits an island's proposer actually got is in that round's `orchestrator_decision`
record (and `generation_stats.proposed` counts what it proposed). The decision, trial and controller records are
EvidenceStore records (`append_record`), not Kernel ledger kinds.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .. import config
from ..ablation import NO_ABLATION, AblationSet
from ..budget import Budget, BudgetExceeded
from ..control.downgrade import AdmissionRegister, DowngradeGate
from ..control.islands import Island, IslandError, Orchestrator, OrchestratorPolicy, admit, signal_from_generation
from ..gateway import Backend
from ..runtime.spec import HarnessSpec
from ..store import EvidenceStore
from .edits import Edit
from .evaluate import SandboxFactory, SuiteTask
from .generations import kind_of, open_campaign
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
                   campaign_budget: Budget | None = None, log: Callable[[str], None] = print,
                   ablation: AblationSet = NO_ABLATION) -> SearchResult:
    """Run the L7 islands search (module docstring). Refuses (ValueError/IslandError/MultiplicityDrift) before any
    run on: rounds < 1, a mix naming an unknown proposer, max_seeds below len(cfg.seeds), a trial without a
    challenger, or a campaign whose claim nodes do not declare the worst-case multiplicity bound."""
    if type(rounds) is not int or rounds < 1:
        raise ValueError("rounds must be an integer >= 1")
    if not isinstance(campaign, str) or not campaign:
        raise ValueError("a campaign name is required (each island gets its own generation ledger)")
    if trial is not None and challenger is None:
        raise ValueError("a trial needs a challenger policy")
    if not isinstance(ablation, AblationSet):
        raise TypeError("ablation must be an AblationSet (custom_harness.ablation)")
    if ablation and (controller is not None or weights_export is not None):
        raise ValueError("an ablated islands campaign takes no controller and no weights_export hook (their records "
                         "have no ABLATED kind; fail closed)")
    abl = bool(ablation)
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
                        weights_lever=weights_export is not None, ablation=ablation)
    ledgers = {isl.island_id: open_campaign(store, f"{campaign}/{isl.island_id}", None, cfg, ablation=ablation)
               for isl in sorted(islands, key=lambda i: i.island_id)}
    pickers = {isl.island_id: ProposerPicker(isl.proposer_mix) for isl in islands}
    evaluator = Evaluator(backends, store, sandbox_factory, working, cfg.budget_per_run,
                          infra_retries=cfg.infra_retries, campaign_budget=campaign_budget, ablation=ablation)
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
                store.append_record(kind_of(ISLAND_SKIP_KIND, abl), {"island": iid, "round": d["round"],
                                                                     "reason": "0 edits", **ablation.marker()})
                log(json.dumps({"round": d["round"], "island": iid, "skipped": "0 edits this round"}))
                orch.skip(iid)
                continue
            ledger = ledgers[iid]
            assert ledger is not None  # open_campaign with a campaign name always returns a ledger
            gcfg = dataclasses.replace(ledger.begin(cfg), n_edits=n_edits, seeds=island_seeds(cfg, d["n_seeds"]))
            name = pickers[iid].pick()
            try:
                res = evolve_generation(parents[iid], working, evaluator=evaluator, proposer=proposers[name],
                                        store=store, cfg=gcfg, edit_admission=admission, ablation=ablation)
            except BudgetExceeded as e:
                result.stopped = f"campaign budget exhausted: {e}"
                log(json.dumps({"round": d["round"], "island": iid, "stopped": result.stopped}))
                break
            led = ledger.record(res)
            lo, hi = led["refs"]["lineage"]
            sig = signal_from_generation(iid, led["generation"],
                                         store.records(kind_of("generation_stats", abl))[
                                             led["refs"]["generation_stats"]],
                                         store.records(kind_of("lineage", abl))[lo:hi])
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


# ============================================================================== `chh evolve --islands` (run path)
ISLANDS_FORMAT_KEYS = frozenset({"rounds", "total_budget", "policy", "challenger", "trial", "proposers", "controller",
                                 "islands"})
_ISLAND_KEYS = frozenset({"id", "proposers", "mutators"})
_TRIAL_KEYS = frozenset({"order_seed", "n_pairs", "alpha", "min_nonzero", "trial_id"})
_ISLAND_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")


@dataclass(frozen=True)
class IslandsConfig:
    """An islands campaign as `chh evolve --islands FILE` declares it. Backends are named by CLI kind
    (`mock`, `codex[:model]`, `openai:<model>`, `ollama:<model>`); the CLI builds them."""
    rounds: int
    total_budget: int
    policy: OrchestratorPolicy
    islands: tuple[Island, ...]
    proposers: Mapping[str, str]  # proposer name -> backend kind
    challenger: OrchestratorPolicy | None = None
    trial: TrialParams | None = None
    controller: str | None = None  # backend kind of the downgrade-only L7 controller (it may only SHRINK budgets)


def load_islands_config(path: Path) -> IslandsConfig:
    """Read and check an islands file (closed keys at every level; fail closed with ValueError / IslandError).

    rounds: 4
    total_budget: 12
    policy: {version: p1}              # OrchestratorPolicy fields; unspecified = the UNCALIBRATED config defaults
    challenger: {version: p2, ...}     # optional: a policy trialled by the policy gate (control/policy_gate.py)
    trial: {order_seed: s1}            # optional TrialParams (needs a challenger)
    proposers: {a: mock, b: "codex"}   # proposer name -> backend kind
    controller: codex                  # optional: downgrade-only budget controller
    islands:
      - {id: i1, proposers: {a: 1}, mutators: {prompt_text: 1, control_flow: 1}}
    A `weights` / `weights_export` key is refused: no model training (owner decision 2026-10-14)."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("islands file: a mapping is required")
    if "weights_export" in doc or "weights" in doc:
        raise ValueError("islands file: the weights lever is not offered (no model training, GOAL L8 owner decision "
                         "2026-10-14; it may only route to the trajectory export, which no run feeds)")
    extra = sorted(set(doc) - ISLANDS_FORMAT_KEYS)
    missing = sorted({"rounds", "total_budget", "policy", "proposers", "islands"} - set(doc))
    if extra or missing:
        raise ValueError(f"islands file: unknown keys {extra}, missing keys {missing}")
    for k in ("rounds", "total_budget"):
        if type(doc[k]) is not int or doc[k] < 1:
            raise ValueError(f"islands file: {k} must be an integer >= 1")

    def policy(d: Any, what: str) -> OrchestratorPolicy:
        if not isinstance(d, dict):
            raise ValueError(f"islands file: {what} must be a mapping of OrchestratorPolicy fields")
        fields = {f.name for f in dataclasses.fields(OrchestratorPolicy)}
        bad = sorted(set(d) - fields)
        if bad:
            raise ValueError(f"islands file: unknown {what} fields {bad}")
        return OrchestratorPolicy(**d)

    props = doc["proposers"]
    if not isinstance(props, dict) or not props or not all(
            isinstance(k, str) and k and isinstance(v, str) and v for k, v in props.items()):
        raise ValueError("islands file: proposers must map names to backend kinds")
    if not isinstance(doc["islands"], list) or not doc["islands"]:
        raise ValueError("islands file: islands must be a non-empty list")
    islands = []
    for i in doc["islands"]:
        bad_id = not isinstance(i, dict) or not isinstance(i.get("id"), str) or not _ISLAND_ID.fullmatch(i["id"])
        if not isinstance(i, dict) or set(i) != _ISLAND_KEYS or bad_id:
            raise ValueError(f"islands file: each island needs exactly {sorted(_ISLAND_KEYS)}, an id of letters, "
                             "digits, '_', '-' or '.' (it names a campaign and an output file)")
        islands.append(Island(i["id"], dict(i["proposers"]), dict(i["mutators"])))
    if len({i.island_id for i in islands}) != len(islands):
        raise ValueError("islands file: duplicate island ids")
    trial = None
    if doc.get("trial") is not None:
        t = doc["trial"]
        if not isinstance(t, dict) or set(t) - _TRIAL_KEYS or "order_seed" not in t:
            raise ValueError(f"islands file: trial keys are {sorted(_TRIAL_KEYS)} (order_seed required)")
        trial = TrialParams(**t)
    controller = doc.get("controller")
    if controller is not None and (not isinstance(controller, str) or not controller):
        raise ValueError("islands file: controller must be a backend kind")
    return IslandsConfig(rounds=doc["rounds"], total_budget=doc["total_budget"], policy=policy(doc["policy"], "policy"),
                         islands=tuple(islands), proposers=dict(props),
                         challenger=policy(doc["challenger"], "challenger") if doc.get("challenger") else None,
                         trial=trial, controller=controller)


def islands_summary(res: SearchResult) -> dict[str, Any]:
    """What `chh evolve --islands` writes to its output directory: per-island final spec hashes, the final policy
    version, the stop reason and the policy-trial decisions (round outcomes are accept counts: released bits only).
    No gate value, no free-text gate decision."""
    return {"parents": {iid: spec.spec_hash() for iid, spec in sorted(res.parents.items())},
            "policy_version": res.policy_version, "stopped": res.stopped,
            "n_decisions": len(res.decisions),
            "trial_decisions": [{k: d.get(k) for k in ("trial_id", "n_plus", "n_minus", "n_zero", "p", "accepted")}
                                for d in res.trial_decisions]}


__all__ = ["ISLAND_SKIP_KIND", "WEIGHTS_ROUTE_KIND", "IslandsConfig", "ProposerPicker", "SearchResult",
           "TrialParams", "evolve_islands", "island_seeds", "islands_summary", "load_islands_config"]
