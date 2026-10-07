"""Evaluating a harness on a task suite (PLAN §6.2, §10 splits).

Objective per research run (documented so it can be audited and changed by humans):
  None (EXCLUDED)           if the environment failed (status INFRA_FAILED): it says nothing about the harness
  0                         if the run did not complete or the integrity audit failed (hard constraint)
  0.5 * certified + 0.5 * replicated + 0.5 * clip(relative DEV improvement, -1, 1)   otherwise
"certified" = the run's claim holds a Kernel certificate (CERTIFIED-INTERNAL/VALIDATED); "replicated" = an
independent re-implementation written from the claim text (not the code) was ALSO certified at the same
minimum effect (GOAL L4.9 headline rule). The relative improvement is the best candidate's DEV score against the
baseline's dev score (cross-validation on public training data only).

Verdict embargo (GOAL §16.5.3, invariant 18: "continuous held-out values never reach the evolver"): until
2026-10-07 the third term was the relative HELD-OUT improvement (certificate subject_mean vs comparator_mean), a
continuous function of the test labels fed back to the proposer every generation (F1: 55.5% false certification
at nominal 5% in the toy). The held-out side of the score is now only verdict bits (audit passed, certified,
replicated, the certificate label), and those bits are a typed, ledgered release (`Releases.emit`, channel
`evolution_score`, alphabet `verdict_bit`) on every target whose private files the run read. In a replayed
campaign (kerneld/campaign.py) the bits are PINNED to their recorded values.
The VDS-centred objective (GOAL §6.3) replaces this once the prediction ledger has enough resolved predictions.
Splits are over TASKS: evolve (proposer sees traces), gate (accept/reject only), sealed (humans only).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..budget import Budget
from ..errors import InfraError
from ..gateway import Backend
from ..kerneld.releases import VERDICT_LABELS
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
    # task family (e.g. "rings", "repair") and domain (e.g. "ml"); "" = unknown, which evolution refuses
    # (fail closed: the leave-one-family-out gate needs every task's family). Defaulted only so positional
    # construction keeps working; load_suite requires `family` in the suite file.
    family: str = ""
    domain: str = "ml"


def held_out(pairs: Iterable[tuple[str, str]]) -> list[str]:
    """Groups (families or domains) HELD OUT in a campaign, from (split, group) pairs: they have >= 1 gate task
    and no evolve task. Sealed tasks neither count as held out nor as exposed."""
    gate, evolve = set(), set()
    for split, group in pairs:
        if split == "gate":
            gate.add(group)
        elif split == "evolve":
            evolve.add(group)
    return sorted(gate - evolve)


def check_held_out(tasks: list[SuiteTask]) -> tuple[list[str], list[str]]:
    """(held-out families, held-out domains) of a campaign; raises ValueError unless it has a held-out family and,
    once the tasks span >= 2 domains, a held-out domain (GOAL §10.16: no acceptance without a LOFO/LODO gate;
    refusing to run is the fail-closed form). Held-out domains are [] while there is a single domain."""
    missing = [t.task_id for t in tasks if not t.family]
    if missing:
        raise ValueError(f"task(s) with no family: {', '.join(missing[:5])} (needed for the leave-one-family-out "
                         "gate)")
    if any(not t.domain for t in tasks):
        raise ValueError("task(s) with an empty domain")
    families = held_out((t.split, t.family) for t in tasks)
    if not families:
        raise ValueError("no held-out family: some family must have gate tasks and no evolve task "
                         "(leave-one-family-out gate, GOAL invariant 16)")
    domains: list[str] = []
    if len({t.domain for t in tasks}) >= 2:
        domains = held_out((t.split, t.domain) for t in tasks)
        if not domains:
            raise ValueError("no held-out domain although the suite spans >= 2 domains (leave-one-domain-out "
                             "gate, GOAL invariant 16)")
    return families, domains


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


def verdict_bits(state: dict, status: RunStatus) -> dict[str, Any]:
    """The TARGET-DERIVED part of the run score: verdict bits only (never a continuous held-out value)."""
    none: dict[str, Any] = {"audit": False, "certified": False, "replicated": False, "label": None}
    if status != RunStatus.COMPLETED:
        return dict(none)
    try:
        audit = state.get("audit")
        cert = state.get("certificate")
        label = cert.get("label") if isinstance(cert, dict) else None
        label = label if isinstance(label, str) and label in VERDICT_LABELS else None
        # credit only for Kernel certificates; the audit (I1) has already checked this state against the ledger
        certified = label in ("CERTIFIED-INTERNAL", "CERTIFIED-VALIDATED")
        # replication earns credit only when it was independent by policy too: a replicator of the author's model
        # family (invariant 5 not met) shares its biases (GOAL §10.5)
        independent = (state.get("model_policy") or {}).get("invariant_5") == "met"
        rcert = state.get("replica_certificate") or {}
        replicated = bool(certified and independent and rcert.get("label") in (
            "CERTIFIED-INTERNAL", "CERTIFIED-VALIDATED"))
    except (AttributeError, TypeError, ValueError, KeyError):  # malformed state: no credit (fail closed)
        return dict(none)
    return {"audit": isinstance(audit, dict) and audit.get("passed") is True, "certified": certified,
            "replicated": replicated, "label": label}


def dev_term(state: dict) -> float:
    """clip(relative dev improvement of the best candidate over the baseline, -1, 1); 0 if unavailable. Dev scores
    come from cross-validation on PUBLIC training data: not target-derived, never a release."""
    try:
        best, base = (state.get("best") or {}).get("dev_score"), state.get("baseline_dev")
        hib = (state.get("task") or {}).get("metric") not in ("rmse", "mae")
    except (AttributeError, TypeError):
        return 0.0
    if (isinstance(best, (int, float)) and isinstance(base, (int, float)) and not isinstance(best, bool)
            and not isinstance(base, bool) and math.isfinite(best) and math.isfinite(base) and base != 0):
        rel = ((best - base) if hib else (base - best)) / abs(base)
        return max(-1.0, min(1.0, rel))
    return 0.0


def combine(status: RunStatus, bits: dict[str, Any], dev: float) -> tuple[float | None, bool]:
    """The run score from the (possibly pinned) verdict bits and the dev term."""
    if status == RunStatus.INFRA_FAILED:
        return None, False
    if status != RunStatus.COMPLETED or bits.get("audit") is not True:  # incl. PAUSED; no audit = no credit
        return 0.0, False
    verified = bits.get("certified") is True
    replicated = verified and bits.get("replicated") is True
    return 0.5 * verified + 0.5 * replicated + 0.5 * dev, verified


def score_state(state: dict, status: RunStatus) -> tuple[float | None, bool]:
    if status == RunStatus.INFRA_FAILED:
        return None, False
    if not isinstance(state, dict):
        return 0.0, False
    return combine(status, verdict_bits(state, status), dev_term(state))


# Capability for the one-shot sealed-split evaluation (finalize.py): run_harness refuses a sealed task without it.
_SEALED_ACCESS = object()


def run_harness(spec: HarnessSpec, task: SuiteTask, seed: int, *, backends: dict[str, Backend],
                store: EvidenceStore, sandbox_factory: SandboxFactory,
                budget_limits: dict[str, float | None] | None = None, retry: RetryPolicy | None = None,
                sealed_access: object | None = None) -> RunOutcome:
    if task.split == "sealed" and sealed_access is not _SEALED_ACCESS:  # invariant 6: sealed only via finalize
        raise ValueError(f"task {task.task_id} is in the sealed split: only `chh finalize` (a human action) runs it")
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
