"""Multi-generation evolution driver + task-suite loading.

Suite file (YAML):
    tasks:
      - {id: rings-a, dir: C:/.../rings-a, split: evolve}
      - {id: rings-b, dir: C:/.../rings-b, split: gate}
      - {id: rings-c, dir: C:/.../rings-c, split: sealed}   # never used here
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

from ..gateway import Backend
from ..runtime.spec import HarnessSpec
from ..store import EvidenceStore
from .evaluate import SandboxFactory, SuiteTask
from .loop import EvolveConfig, Evaluator, evolve_generation, proposer_calibration


def load_suite(path: Path) -> list[SuiteTask]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    tasks = [SuiteTask(t["id"], Path(t["dir"]), t["split"]) for t in doc["tasks"]]
    ids = [t.task_id for t in tasks]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate task ids in suite")
    bad = [t for t in tasks if t.split not in ("evolve", "gate", "sealed")]
    if bad:
        raise ValueError(f"invalid split for {bad[0].task_id}")
    return tasks


def evolve(seed_spec: HarnessSpec, suite: list[SuiteTask], *, generations: int, backends: dict[str, Backend],
           proposer: Backend, store: EvidenceStore, sandbox_factory: SandboxFactory,
           cfg: EvolveConfig = EvolveConfig(), out_dir: Path | None = None, log=print) -> HarnessSpec:
    """Run `generations` rounds. The sealed split is filtered out before anything runs."""
    working = [t for t in suite if t.split != "sealed"]
    evaluator = Evaluator(backends, store, sandbox_factory, working, cfg.budget_per_run)
    parent = seed_spec
    for g in range(generations):
        t0 = time.time()
        res = evolve_generation(parent, working, evaluator=evaluator, proposer=proposer, store=store, cfg=cfg)
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
