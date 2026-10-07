"""Finalize (GOAL §5 L6.12, invariant 6): the ONE-SHOT evaluation of a frozen final harness on the sealed split.

HUMAN action only, through `chh finalize` (pattern of `chh approve-adapter`): Claude and models never run it. It
  1. refuses without a named human approver, without sealed tasks, or if ANY sealed task (by task id or by the
     content-based id of its held-out data, so a renamed copy counts) was finalized before: once per sealed split;
  2. records the decision in the hash-chained Kernel ledger BEFORE any sealed run (kind "approval", node_id
     "finalize", bound to the hash of exactly what is evaluated: spec hash, optional baseline hash, sealed tasks and
     their held-out ids, seeds), so a crash or an abort mid-way still consumes the shot (fail closed: no retry
     until it looks good);
  3. runs the final spec (and, if given, the baseline spec, paired per unit) on every sealed task x seed and appends
     the scores to the `finalize` record.
Nothing else may run a sealed task: `run_harness` refuses a sealed task unless called from here (code-level guard,
`_SEALED_ACCESS`), the evolution Evaluator refuses sealed tasks, the driver filters them out and the evidence export
drops their runs. Trust boundary (stated, as for approve-adapter): trusted host code could call this function; the
ledger entry names the approver and is hash-chained (accountability, not enforcement). The Kernel ledger has no
dedicated kind for it yet, so the human "approval" kind is used with node_id "finalize" (no run is named
`finalize:*`, so the engine can never read it as a run's approval).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from ..gateway import Backend
from ..kernel import Ledger
from ..runtime.engine import approval_item_hash
from ..runtime.spec import HarnessSpec
from ..store import EvidenceStore
from .evaluate import _SEALED_ACCESS, SandboxFactory, SuiteTask, run_harness
from .multiplicity import held_out_target_id
from .selection import paired_ci

FINALIZE_NODE = "finalize"


class FinalizeRefused(RuntimeError):
    pass


def prior_finalizations(store: EvidenceStore) -> list[dict[str, Any]]:
    """Every recorded finalize decision (intact ledger entries only; a broken one is reported by ledger-verify)."""
    out = []
    for e in Ledger(store).entries("approval"):
        if e.body.get("node_id") == FINALIZE_NODE and isinstance(e.body.get("finalize"), dict):
            out.append({"seq": e.seq, "intact": e.intact(), **e.body["finalize"]})
    return out


def finalize(spec: HarnessSpec, suite: list[SuiteTask], *, approver: str, backends: Mapping[str, Backend],
             store: EvidenceStore, sandbox_factory: SandboxFactory, seeds: tuple[int, ...] = (0,),
             baseline: HarnessSpec | None = None) -> dict[str, Any]:
    approver = (approver or "").strip()
    if not approver:
        raise FinalizeRefused("finalize is a human action: --approver must name the human")
    sealed = sorted((t for t in suite if t.split == "sealed"), key=lambda t: t.task_id)
    if not sealed:
        raise FinalizeRefused("the suite has no sealed task")
    if not seeds or len(set(seeds)) != len(seeds):
        raise FinalizeRefused("seeds must be non-empty and distinct")
    tasks = [{"task_id": t.task_id, "target": held_out_target_id(t.task_dir)} for t in sealed]
    used_ids, used_targets = set(), set()
    for p in prior_finalizations(store):
        for t in p.get("sealed") or []:
            used_ids.add(t.get("task_id"))
            used_targets.add(t.get("target"))
    again = [t["task_id"] for t in tasks if t["task_id"] in used_ids or t["target"] in used_targets]
    if again:
        raise FinalizeRefused(f"sealed task(s) {again} were already finalized: the sealed split is touched once "
                              "(a second look would make it a training signal)")
    item = {"spec_hash": spec.spec_hash(), "baseline_hash": baseline.spec_hash() if baseline else None,
            "sealed": tasks, "seeds": list(seeds)}
    item_hash = approval_item_hash(item)
    entry = Ledger(store).append("approval", {
        "run_id": f"{FINALIZE_NODE}:{item_hash[:16]}", "node_id": FINALIZE_NODE, "item_hash": item_hash,
        "decision": "approved", "approver": approver, "note": "one-shot sealed-split evaluation", "finalize": item})
    store.append_record("finalize", {"ledger_seq": entry.seq, "item_hash": item_hash, "approver": approver,
                                     "started_at": time.time(), "status": "started", **item})
    units = [(t, s) for t in sealed for s in seeds]
    rows, diffs = [], []
    for t, s in units:
        out = run_harness(spec, t, s, backends=dict(backends), store=store, sandbox_factory=sandbox_factory,
                          sealed_access=_SEALED_ACCESS)
        row: dict[str, Any] = {"task_id": t.task_id, "seed": s, "run_id": out.run_id, "score": out.score,
                               "status": out.status, "cert_label": out.cert_label}
        if baseline is not None:
            b = run_harness(baseline, t, s, backends=dict(backends), store=store, sandbox_factory=sandbox_factory,
                            sealed_access=_SEALED_ACCESS)
            row |= {"baseline_run_id": b.run_id, "baseline_score": b.score, "baseline_status": b.status}
            if out.score is not None and b.score is not None:
                diffs.append(out.score - b.score)
        rows.append(row)
    valid = [r["score"] for r in rows if r["score"] is not None]
    summary: dict[str, Any] = {"ledger_seq": entry.seq, "item_hash": item_hash, "approver": approver, **item,
                               "units": rows, "n_excluded": len(rows) - len(valid),
                               "mean_score": sum(valid) / len(valid) if valid else None}
    if baseline is not None:
        m, lo, hi = paired_ci(diffs)
        summary["paired"] = {"n": len(diffs), "mean_diff": m, "lo": lo, "hi": hi}
    store.append_record("finalize", {**summary, "status": "done", "ended_at": time.time()})
    return summary
