"""Automated (non-human) supervision of studio runs, ONLY through the downgrade-only gate (GOAL §5 L1 "LLM controllers
may only downgrade", §1.1; control/downgrade.py).

`studio_gate` is the one constructor of a DowngradeGate over the studio's runs: its kill effect is
`studio.kill.controller_kill_effect` (marks the run FAILED; the gate ledgers a `controller_action` record, never a
`run_kill` entry, which stays human-only), and every engine check honours that record (`studio.kill.kill_entry`).
`supervise` runs one controller turn: it shows the controller a code-built observation of the killable runs and
applies whatever the gate accepts. `chh supervise --controller BACKEND` is the CLI for it.

What a controller may do here, per action (the gate's semantics, unchanged):
- `kill` a still-killable run, with evidence code recomputes (`no_progress`, `stalled`; `budget_exceeded` only when
  the caller injects the run's Budget, which the CLI does not: no per-run Budget object is persisted);
- `deny` a PENDING request of an injected AdmissionRegister and `shrink` an injected budget cap. The studio has no
  persisted admission queue or live budget object to inject today, so from the CLI both are always rejected
  (unknown request / budget) and ledgered.
Grant, admit, enlarge, revive, approve and every other action type are rejected and ledgered by the gate.
Approval nodes are never routed here: only a human decides them (invariant 15).

The observation carries no free text from runs: ids, statuses, step counts, the sha256 of recent step errors (what
`no_progress` evidence names) and recorded costs. Nothing private or target-derived is read.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..budget import Budget
from ..control.downgrade import (
    DEFAULT_NO_PROGRESS_MIN_STEPS,
    DEFAULT_STALL_S,
    AdmissionRegister,
    DowngradeGate,
    ProposalResult,
)
from ..gateway.base import Backend
from ..store import EvidenceStore
from .kill import _killable, controller_kill_effect, kill_entry

OBSERVED_ERRORS = 5  # recent step errors whose sha256 the observation shows per run


def studio_gate(store: EvidenceStore, *, budgets: Mapping[str, Budget] | None = None,
                admissions: AdmissionRegister | None = None, run_budgets: Mapping[str, Budget] | None = None,
                no_progress_min_steps: int = DEFAULT_NO_PROGRESS_MIN_STEPS, stall_s: float = DEFAULT_STALL_S,
                clock: Callable[[], float] = time.time) -> DowngradeGate:
    return DowngradeGate(store, budgets={} if budgets is None else budgets,
                         admissions=AdmissionRegister() if admissions is None else admissions,
                         kill=controller_kill_effect(store), run_budgets=run_budgets,
                         no_progress_min_steps=no_progress_min_steps, stall_s=stall_s, clock=clock)


def observe(store: EvidenceStore, *, limit: int = 50, budgets: Mapping[str, Budget] | None = None,
            admissions: AdmissionRegister | None = None, clock: Callable[[], float] = time.time) -> str:
    """The code-built observation a controller sees (JSON text)."""
    runs = []
    for run in store.list_runs(limit):
        status, detail = store.run_status(run.id)
        if not _killable(status, detail) or kill_entry(store, run.id) is not None:
            continue
        trace = store.trace(run.id)
        cost = store.run_cost(run.id)
        runs.append({"run_id": run.id, "status": status.value, "steps": len(trace),
                     "last_step_index": trace[-1].index if trace else -1,
                     "seconds_since_last_step": round(clock() - (trace[-1].ended_at if trace else run.started_at), 1),
                     "recent_error_sha256": [None if s.error is None else hashlib.sha256(
                         s.error.encode("utf-8", "surrogatepass")).hexdigest() for s in trace[-OBSERVED_ERRORS:]],
                     "cost": {"tokens": cost.prompt_tokens + cost.completion_tokens, "usd": cost.usd}})
    caps = {name: dict(b.limits) for name, b in (budgets or {}).items()}
    pending = sorted(r for r, s in (admissions.snapshot() if admissions else {}).items() if s.value == "pending")
    return json.dumps({"runs": runs, "budgets": caps, "pending_admissions": pending}, sort_keys=True)


def supervise(store: EvidenceStore, backend: Backend, *, controller_id: str, limit: int = 50,
              budgets: Mapping[str, Budget] | None = None, admissions: AdmissionRegister | None = None,
              run_budgets: Mapping[str, Budget] | None = None, **gate_kw: Any) -> ProposalResult:
    """One controller turn over the studio's killable runs; every effect goes through the downgrade gate."""
    gate = studio_gate(store, budgets=budgets, admissions=admissions, run_budgets=run_budgets, **gate_kw)
    clock = gate_kw.get("clock", time.time)
    return gate.ask(controller_id, backend, observe(store, limit=limit, budgets=budgets, admissions=admissions,
                                                     clock=clock))


def result_json(res: ProposalResult) -> dict[str, Any]:
    return {"proposal_sha256": res.proposal_sha256, "envelope": res.envelope.value,
            "actions": [{"index": a.index, "type": a.action_type, "target": a.target_id, "outcome": a.outcome.value,
                         "reason": a.reason.value} for a in res.actions]}
