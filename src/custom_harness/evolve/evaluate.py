"""Evaluating a harness on a task suite (PLAN §6.2, §10 splits).

Objective per research run (M2 version, documented so it can be audited and changed by humans):
  None (EXCLUDED)           if the environment failed (status INFRA_FAILED): it says nothing about the harness
  0                         if the run did not complete or the integrity audit failed (hard constraint)
  0.5 * certified + 0.5 * replicated + 0.5 * clip(relative held-out improvement, -1, 1)   otherwise
"certified" = the run's claim holds a Kernel certificate (CERTIFIED-INTERNAL/VALIDATED); "replicated" = an
independent re-implementation written from the claim text (not the code) was ALSO certified at the same
minimum effect (GOAL L4.9 headline rule). The relative improvement comes from the held-out evaluation records.
The VDS-centred objective (GOAL §6.3)
replaces this once the prediction ledger has enough resolved predictions per campaign (M3).
Splits are over TASKS: evolve (proposer sees traces), gate (accept/reject only), sealed (humans only).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..budget import Budget
from ..errors import InfraError
from ..gateway import Backend
from ..records import Cost, RunStatus
from ..runtime import Engine, HarnessSpec
from ..runtime.engine import RetryPolicy
from ..sandbox import Sandbox
from ..store import EvidenceStore

Split = Literal["evolve", "gate", "sealed"]


@dataclass(frozen=True)
class SuiteTask:
    task_id: str
    task_dir: Path
    split: Split


@dataclass
class RunOutcome:
    task_id: str
    run_id: str
    seed: int
    score: float | None  # None = excluded (infrastructure failure), never treated as 0
    verified: bool
    status: str
    cost: Cost
    failure_signatures: list[str] = field(default_factory=list)
    cert_label: str | None = None  # the run's Kernel certificate label (drives the loop label, GOAL §6.1)

    @property
    def infra_failed(self) -> bool:
        return self.status == RunStatus.INFRA_FAILED.value


SandboxFactory = Callable[[str], Sandbox]


def _signature(text: str) -> str:
    """Normalise an error into a clusterable signature (drop numbers, paths, quotes)."""
    text = re.sub(r"[A-Za-z]:\\[^\s'\"]+|/[^\s'\"]+", "<path>", text)
    text = re.sub(r"\d+(\.\d+)?", "<n>", text)
    text = re.sub(r"'[^']*'|\"[^\"]*\"", "<s>", text)
    return text.strip()[:160]


def failure_signatures(store: EvidenceStore, run_id: str) -> list[str]:
    sigs = []
    for step in store.trace(run_id):
        if step.error:
            sigs.append(f"{step.node_id}: {_signature(step.error.splitlines()[0])}")
        run_err = step.output.get("run_error") if isinstance(step.output, dict) else None
        if run_err:
            last = [ln for ln in str(run_err).splitlines() if ln.strip()][-1:] or ["?"]
            sigs.append(f"{step.node_id}/solution: {_signature(last[0])}")
        if isinstance(step.output, dict) and step.output.get("route") == "give_up":
            sigs.append(f"{step.node_id}: gave up without beating baseline on dev")
    status, detail = store.run_status(run_id)
    if status != RunStatus.COMPLETED:
        sigs.append(f"run: {status.value}: {_signature(detail.splitlines()[0] if detail else '')}")
    return sigs


def score_state(state: dict, status: RunStatus) -> tuple[float | None, bool]:
    if status == RunStatus.INFRA_FAILED:
        return None, False
    if status != RunStatus.COMPLETED:  # incl. PAUSED: a run waiting for a human decision earns no credit
        return 0.0, False
    try:
        return _score_completed(state)
    except (AttributeError, TypeError, ValueError, KeyError):  # malformed state: no credit (fail closed)
        return 0.0, False


def _score_completed(state: dict) -> tuple[float, bool]:
    audit = state.get("audit")
    if not isinstance(audit, dict) or audit.get("passed") is not True:  # no audit = no credit (hard constraint)
        return 0.0, False
    cert = state.get("certificate") or {}
    # Credit only for Kernel certificates; the audit (I1) has already checked this state against the ledger.
    verified = cert.get("label") in ("CERTIFIED-INTERNAL", "CERTIFIED-VALIDATED")
    # replication earns credit only when it was independent by policy too: a replicator of the author's model
    # family (invariant 5 not met) shares its biases (GOAL §10.5)
    independent = (state.get("model_policy") or {}).get("invariant_5") == "met"
    replicated = verified and independent and (state.get("replica_certificate") or {}).get("label") in (
        "CERTIFIED-INTERNAL", "CERTIFIED-VALIDATED")
    sol, base = cert.get("subject_mean"), cert.get("comparator_mean")
    rel = 0.0
    if (isinstance(sol, (int, float)) and isinstance(base, (int, float)) and not isinstance(sol, bool)
            and not isinstance(base, bool) and math.isfinite(sol) and math.isfinite(base) and base != 0):
        hib = (state.get("task") or {}).get("metric") not in ("rmse", "mae")
        rel = ((sol - base) if hib else (base - sol)) / abs(base)
    return 0.5 * verified + 0.5 * replicated + 0.5 * max(-1.0, min(1.0, rel)), verified


def run_harness(spec: HarnessSpec, task: SuiteTask, seed: int, *, backends: dict[str, Backend],
                store: EvidenceStore, sandbox_factory: SandboxFactory,
                budget_limits: dict[str, float | None] | None = None, retry: RetryPolicy | None = None) -> RunOutcome:
    try:
        sandbox = sandbox_factory(f"{spec.spec_hash()[:10]}-{task.task_id}-{seed}")
    except InfraError as e:  # e.g. Docker daemon down before the run even started
        return RunOutcome(task.task_id, "", seed, None, False, RunStatus.INFRA_FAILED.value, Cost(),
                          [f"run: infra_failed: {_signature(str(e).splitlines()[0] if str(e) else '')}"])
    eng = Engine(spec, backends=backends, store=store, sandbox=sandbox,
                 budget=Budget(limits=budget_limits) if budget_limits else None, retry=retry or RetryPolicy())
    res = eng.run(task.task_id, {"task_dir": str(task.task_dir)}, seed=seed)
    score, verified = score_state(res.state, res.status)
    cert = res.state.get("certificate")
    label = cert.get("label") if isinstance(cert, dict) else None
    return RunOutcome(task.task_id, res.run_id, seed, score, verified, res.status.value, res.cost,
                      failure_signatures(store, res.run_id), cert_label=label)
