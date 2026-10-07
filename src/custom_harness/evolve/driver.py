"""Multi-generation evolution driver + task-suite loading.

Suite file (YAML):
    tasks:
      - {id: rings-a, dir: C:/.../rings-a, split: evolve, family: rings, domain: ml, source: rings-a}
      - {id: spirals-a, dir: C:/.../spirals-a, split: gate, family: spirals, domain: ml, source: spirals-a}
      - {id: rings-c, dir: C:/.../rings-c, split: sealed, family: rings, domain: ml, source: rings-c}  # never used
`source` = the base dataset; loading refuses a suite where a source, or any example row, spans two splits.
`family` is required and `domain` defaults to ml. Loading refuses a suite without a held-out family (gate tasks, no
evolve task) and, once it spans >= 2 domains, without a held-out domain (leave-one-family/domain-out gate).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

from ..budget import Budget, BudgetExceeded
from ..gateway import Backend
from ..runtime.spec import HarnessSpec
from ..store import EvidenceStore
from .evaluate import SandboxFactory, SuiteTask, check_held_out
from .loop import Evaluator, EvolveConfig, evolve_generation, proposer_calibration
from .multiplicity import check_campaign, runs_per_task_bound


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
           campaign_budget: Budget | None = None) -> HarnessSpec:
    """Run `generations` rounds. The sealed split is filtered out before anything runs. Refuses a campaign whose
    claim nodes do not pre-declare n_comparisons >= its multiplicity bound (R2-2, multiplicity.py)."""
    cfg = cfg or EvolveConfig()
    working = [t for t in suite if t.split != "sealed"]
    n_fresh = len(cfg.remeasure_seeds) if cfg.remeasure_seeds is not None else len(cfg.seeds)
    bound = check_campaign(seed_spec, store, [t.task_dir for t in working], runs_per_task=runs_per_task_bound(
        generations=generations, n_edits=cfg.n_edits, n_seeds=len(cfg.seeds), n_fresh_seeds=n_fresh,
        infra_retries=cfg.infra_retries))
    log(json.dumps({"multiplicity_bound": bound}))
    evaluator = Evaluator(backends, store, sandbox_factory, working, cfg.budget_per_run,
                          infra_retries=cfg.infra_retries, campaign_budget=campaign_budget)
    parent = seed_spec
    for g in range(generations):
        t0 = time.time()
        try:
            res = evolve_generation(parent, working, evaluator=evaluator, proposer=proposer, store=store, cfg=cfg)
        except BudgetExceeded as e:
            log(json.dumps({"generation": g, "stopped": f"campaign budget exhausted: {e}"}))
            break
        summary = {"generation": g, "accepted": res.accepted, "decision": res.decision,
                   "parent": res.parent_hash[:12], "new_parent": res.new_parent.spec_hash()[:12],
                   "harness_runs": res.evaluations, "fixed_k_equivalent": res.evaluations_fixed_k,
                   "candidates": [{k: c.get(k) for k in ("status", "reason", "survived")} | (
                       {"type": c["edit"]["type"], "rationale": c["edit"]["rationale"][:160]} if "edit" in c else {})
                       for c in res.candidates],
                   "calibration": proposer_calibration(store), "wall_s": round(time.time() - t0, 1)}
        log(json.dumps(summary, indent=1))
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"gen{g:03d}.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
            (out_dir / f"spec_gen{g:03d}.yaml").write_text(
                yaml.safe_dump(res.new_parent.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
        parent = res.new_parent
    return parent
