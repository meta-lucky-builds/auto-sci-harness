"""Multi-generation evolution driver + task-suite loading.

Suite file (YAML):
    tasks:
      - {id: rings-a, dir: C:/.../rings-a, split: evolve, family: rings, domain: ml, source: rings-a}
      - {id: spirals-a, dir: C:/.../spirals-a, split: gate, family: spirals, domain: ml, source: spirals-a}
      - {id: rings-c, dir: C:/.../rings-c, split: sealed, family: rings, domain: ml, source: rings-c}  # never used
`source` = the base dataset; loading refuses a suite where a source, or any example row, spans two splits.
`family` is required and `domain` defaults to ml. Loading refuses a suite without a held-out family (gate tasks, no
evolve task) and, once it spans >= 2 domains, without a held-out domain (leave-one-family/domain-out gate).

Agent-visible output (orchestrator decision, owner-delegated, 2026-10-14): `out_dir` and the `log` lines are treated
as agent-visible. A generation's outcome appears there ONLY as its released verdict bit (`accepted`) and a closed
reason code (`decision_code`, `DECISION_CODES`), never as the loop's free-text decision (which can quote a gate
mean or CI, e.g. selection.gate's 'no improvement on gate split (mean=-0.5)'). The free text stays in the host-only
`harness_edit` record.

Ablation (custom_harness/ablation.py): `evolve(..., ablation=...)` runs a multi-generation ABLATION ARM. The
AblationSet can only be made by the scorecard runner's entry point (`scorecard.runner.ablation_set`); there is no CLI
flag, environment variable or config default for it. The set reaches the Evaluator (every run marked ABLATED), every
`evolve_generation` (never accepts; `:ABLATED` record kinds), the campaign ledger (`:ABLATED` kinds; resume only under
the same set). An arm keeps no durable proposal log (the loop refuses one under an ablation), so a resumed arm
re-asks its proposer for an interrupted generation. An arm makes no branches and builds no router (`branching` is
refused).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import yaml

from ..ablation import NO_ABLATION, AblationSet
from ..budget import Budget, BudgetExceeded
from ..gateway import Backend
from ..runtime.spec import HarnessSpec
from ..store import EvidenceStore
from .branches import BranchConfig
from .durable import ProposalLog, durable_key
from .embargo import EmbargoPlan, EmbargoRefused, plan_campaign, resolve_plan
from .evaluate import SandboxFactory, SuiteTask, check_held_out
from .generations import AnnealSchedule, LedgerError, first_parent, last_parent, open_campaign
from .loop import GATE_STAGES, Evaluator, EvolveConfig, GenerationResult, evolve_generation, proposer_calibration
from .multiplicity import check_campaign, runs_per_task_bound
from .reveal import TemporalReveal
from .router import Router, RouteRefused, build_router


def load_suite(path: Path) -> list[SuiteTask]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    missing = [t.get("id") for t in doc["tasks"] if not isinstance(t.get("family"), str) or not t["family"]]
    if missing:  # fail closed: the leave-one-family-out gate needs every task's family
        raise ValueError(f"task(s) with no `family`: {', '.join(map(str, missing[:5]))}")
    tasks = [SuiteTask(t["id"], Path(t["dir"]), t["split"], family=t["family"], domain=str(t.get("domain", "ml")))
             for t in doc["tasks"]]
    ids = [t.task_id for t in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate task ids in suite")
    bad = [t for t in tasks if t.split not in ("evolve", "gate", "sealed")]
    if bad:
        raise ValueError(f"invalid split for {bad[0].task_id}")
    # fail closed: the gate/sealed splits mean nothing if their examples are public in another split
    from ..research.suite import check_split_isolation
    problems = check_split_isolation(doc["tasks"])
    if problems:
        raise ValueError("suite splits are not isolated: " + "; ".join(problems))
    check_held_out(tasks)  # GOAL invariant 16: no campaign without a held-out family (/domain once >= 2)
    return tasks


def evolve(seed_spec: HarnessSpec, suite: list[SuiteTask], *, generations: int, backends: dict[str, Backend],
           proposer: Backend, store: EvidenceStore, sandbox_factory: SandboxFactory,
           cfg: EvolveConfig | None = None, out_dir: Path | None = None, log=print,
           campaign_budget: Budget | None = None, branching: BranchConfig | None = None,
           campaign: str | None = None, anneal: AnnealSchedule | None = None,
           reveals: Sequence[tuple[TemporalReveal, Path]] = (), ablation: AblationSet = NO_ABLATION) -> HarnessSpec:
    """Run `generations` rounds. The sealed split is filtered out before anything runs. Refuses a campaign whose
    claim nodes do not pre-declare n_comparisons >= its multiplicity bound (R2-2, multiplicity.py).

    With a `campaign` the run is durable (durable.py): a re-invocation with the same campaign RESUMES it. It
    continues from the last completed generation's new parent (the seed spec must be the campaign's first parent),
    runs `generations` more generations starting with the interrupted one, re-runs and re-charges no finished unit
    (the Evaluator's durable unit outcomes, keyed by the campaign) and never asks the proposer again for a generation
    whose proposal was persisted. File names in `out_dir` and logged generation numbers are campaign-global."""
    cfg = cfg or EvolveConfig()
    if not isinstance(ablation, AblationSet):
        raise TypeError("ablation must be an AblationSet (custom_harness.ablation)")
    if ablation and branching is not None:
        raise ValueError("an ablation arm makes no branches (it never accepts): branching is refused")
    abl = bool(ablation)
    # `campaign`/`anneal`: generation ledger (generations.py): declared or resumed here, before any run (fail closed);
    # each generation gets the annealed edit budget and a `generation_ledger` record (stall, budget, refs)
    gen_ledger = open_campaign(store, campaign, anneal, cfg, ablation=ablation)
    parent = _resume_parent(store, campaign, seed_spec, ablated=abl) if campaign is not None else seed_spec
    # no durable proposal log in an arm (the loop refuses one: its record kind has no ABLATED form), so a resumed arm
    # asks its proposer again for an interrupted generation (stated limit); its runs stay durable (Evaluator key)
    proposal_campaign = None if abl else campaign
    working = [t for t in suite if t.split != "sealed"]
    check_reveals(reveals, suite, out_dir)  # L6.11: every seal outside every mount this campaign uses (fail closed)
    n_fresh = len(cfg.remeasure_seeds) if cfg.remeasure_seeds is not None else len(cfg.seeds)
    plan: EmbargoPlan | None = None
    unit_key = durable_key(campaign) if campaign is not None else None
    if cfg.evidence == "embargo":  # evolve/embargo.py: only gate runs claim, so the bound is known now (fail closed)
        if abl or branching is not None or reveals:
            raise EmbargoRefused("the evidence embargo takes no ablation arm, no branching and no temporal reveal: "
                                 "each releases information its bound does not count")
        gate_tasks = [t for t in working if t.split == "gate"]
        plan = resolve_plan(store, campaign, gen_ledger.generation if gen_ledger is not None else 0, generations,
                            lambda: plan_campaign(seed_spec, store, gate_tasks, generations=generations,
                                                  n_seeds=len(cfg.seeds), infra_retries=cfg.infra_retries,
                                                  gate_alpha=cfg.gate_alpha))
        bound = plan.n_comparisons
        unit_key = f"{unit_key}|embargo" if unit_key is not None else None  # never mixed with legacy unit outcomes
    else:
        bound = check_campaign(seed_spec, store, [t.task_dir for t in working], runs_per_task=runs_per_task_bound(
            generations=generations, n_edits=cfg.n_edits, n_seeds=len(cfg.seeds), n_fresh_seeds=n_fresh,
            infra_retries=cfg.infra_retries))
    log(json.dumps({"multiplicity_bound": bound}))
    evaluator = Evaluator(backends, store, sandbox_factory, working, cfg.budget_per_run,
                          infra_retries=cfg.infra_retries, campaign_budget=campaign_budget,
                          durable_key=unit_key, ablation=ablation, plan=plan)
    for g in range(generations):
        # the campaign-global generation index (continued across resume); the local loop index without a campaign
        gi = gen_ledger.generation if gen_ledger is not None else g
        cfg = gen_ledger.begin(cfg) if gen_ledger is not None else cfg  # annealed n_edits (<= the bound's)
        reveal_step(reveals, log)  # L6.11: labels whose date the watermark passed become visible (ledgered)
        t0 = time.time()
        try:
            res = evolve_generation(parent, working, evaluator=evaluator, proposer=proposer, store=store, cfg=cfg,
                                    branching=branching, ablation=ablation,
                                    proposals=ProposalLog(store, proposal_campaign, gi)
                                    if proposal_campaign is not None else None)
        except BudgetExceeded as e:
            if gen_ledger is not None:  # ledgered with its stop reason (it does not consume the index)
                gen_ledger.record_stopped(parent.spec_hash(), "budget_exceeded", str(e))
            log(json.dumps({"generation": gi, "stopped": f"campaign budget exhausted: {e}"}))
            break
        if gen_ledger is not None:
            led = gen_ledger.record(res)
            log(json.dumps({"campaign": campaign, "ledger_generation": led["generation"], "budget": led["budget"],
                            "stalled": led["stall"]["stalled"]}))
        # agent-visible (module docstring): the released accept bit and a closed reason code, never res.decision; the
        # loop label (invariant 10, loop_labels.py) is a closed 2-value code too (its embargo status: owner decision)
        summary = {"generation": gi, "loop_label": res.loop_label, "accepted": res.accepted,
                   "decision_code": decision_code(res), **ablation.marker(),
                   "parent": res.parent_hash[:12], "new_parent": res.new_parent.spec_hash()[:12],
                   "harness_runs": res.evaluations, "fixed_k_equivalent": res.evaluations_fixed_k,
                   "candidates": [{k: c.get(k) for k in ("status", "reason", "survived")} | (
                       {"type": c["edit"]["type"], "rationale": c["edit"]["rationale"][:160]} if "edit" in c else {})
                       for c in res.candidates],
                   "calibration": proposer_calibration(store), "wall_s": round(time.time() - t0, 1)}
        log(json.dumps(summary, indent=1))
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"gen{gi:03d}.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
            (out_dir / f"spec_gen{gi:03d}.yaml").write_text(
                yaml.safe_dump(res.new_parent.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
        parent = res.new_parent
    if branching is not None:  # L6.9: route table over the FINAL trunk's branches (fail closed: none on refusal)
        route_after(store, parent, branching, out_dir, log)
    return parent


# The closed reason codes of a generation's outcome in the agent-visible driver output (module docstring): accepted;
# the released rejecting stage (loop.GATE_STAGES, itself a ledgered `count` release); inconclusive (too many units
# lost to infrastructure: no verdict); not_gated (no candidate reached the gate); ablated (an ablation arm's
# measurement: never accepted).
DECISION_CODES: tuple[str, ...] = ("accepted", *(st for st in GATE_STAGES if st is not None), "inconclusive",
                                   "not_gated", "ablated")
_GATED = ("accepted", "rejected_gate", "infra_aborted", "ablated_not_accepted")


def decision_code(res: GenerationResult) -> str:
    """The closed reason code of a generation (DECISION_CODES), from its gated lineage record's status and released
    stage. Fail closed (ValueError) on more than one gated record or a stage outside GATE_STAGES."""
    gated = [c for c in res.candidates if c.get("status") in _GATED]
    if len(gated) > 1:
        raise ValueError("more than one gated candidate in one generation (fail closed)")
    if not gated:
        if res.accepted:
            raise ValueError("an accepted generation without a gated candidate (fail closed)")
        return "not_gated"
    status = gated[0]["status"]
    if (status == "accepted") != bool(res.accepted):
        raise ValueError("accept bit and gated status disagree (fail closed)")
    if status == "accepted":
        return "accepted"
    if status == "infra_aborted":
        return "inconclusive"
    if status == "ablated_not_accepted":
        return "ablated"
    stage = gated[0].get("rejected_on")
    if stage not in GATE_STAGES or stage is None:
        raise ValueError(f"rejected candidate with stage {stage!r} outside the released stages (fail closed)")
    return str(stage)


def _resume_parent(store: EvidenceStore, campaign: str, seed_spec: HarnessSpec, *,
                   ablated: bool = False) -> HarnessSpec:
    """The parent a (resumed) campaign continues from: the seed spec for a new campaign, else the last completed
    generation's new parent, loaded from the store's harness versions and checked against its hash (fail closed).
    `ablated`: an ablation arm's campaign (its `:ABLATED` ledger)."""
    first, last = first_parent(store, campaign, ablated=ablated), last_parent(store, campaign, ablated=ablated)
    if first is None or last is None:
        return seed_spec
    if first != seed_spec.spec_hash():
        raise LedgerError(f"campaign {campaign!r} was started from spec {first[:12]}, not {seed_spec.spec_hash()[:12]}"
                          ": a campaign resumes only from its own seed spec")
    if last == seed_spec.spec_hash():
        return seed_spec
    try:
        spec = HarnessSpec.model_validate(store.harness_spec(last))
    except (KeyError, OSError, ValueError) as e:
        raise LedgerError(f"campaign {campaign!r}: its current parent {last[:12]} cannot be loaded ({e})") from None
    if spec.spec_hash() != last:
        raise LedgerError(f"campaign {campaign!r}: stored parent {last[:12]} does not hash to itself")
    return spec


ROUTE_KIND = "route_table"


def route_after(store: EvidenceStore, trunk: HarnessSpec, branching: BranchConfig, out_dir: Path | None,
                log: Callable[[str], Any] = print) -> Router | None:
    """Build the router over `trunk`'s branches (router.build_router: released verdict bits and a passing LOFO/LODO
    bit only) and record its table (spec hashes and cluster names; no gate value) as a `route_table` store record and
    `router.json`. A refused router is recorded as refused and None is returned: the trunk alone serves (fail
    closed). Branches made against an earlier trunk are stale for the final one and are not routed."""
    try:
        router = build_router(store, trunk, cluster_key=branching.cluster_key)
    except RouteRefused as e:
        rec: dict[str, Any] = {"trunk": trunk.spec_hash(), "status": "refused", "reason": str(e)[:500]}
        store.append_record(ROUTE_KIND, rec)
        log(json.dumps({"router": rec}))
        return None
    rec = {"trunk": router.trunk, "status": "built", "table": router.table(), "table_hash": router.table_hash()}
    store.append_record(ROUTE_KIND, rec)
    log(json.dumps({"router": rec}))
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "router.json").write_text(json.dumps(rec, indent=1, sort_keys=True), encoding="utf-8")
    return router


def check_reveals(reveals: Sequence[tuple[TemporalReveal, Path]], suite: list[SuiteTask],
                  out_dir: Path | None) -> None:
    """Each stream's seal must lie outside every task directory of the suite and the output directory (in addition
    to the mounts the stream declares), and its destination must be one of its declared mounts' files."""
    paths = [t.task_dir for t in suite] + ([out_dir] if out_dir is not None else [])
    for rv, dest in reveals:
        rv.check_mounts(paths)
        rv.check_destination(dest)


def reveal_step(reveals: Sequence[tuple[TemporalReveal, Path]], log: Callable[[str], Any] = print) -> None:
    for rv, dest in reveals:
        rv.write_revealed(dest)
        log(json.dumps({"reveal": rv.stream_id, "visible": rv.n_revealed()}))
