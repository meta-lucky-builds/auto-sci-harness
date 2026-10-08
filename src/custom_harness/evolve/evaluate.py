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
from ..kerneld.releases import VERDICT_LABELS, Releases, attributed_reads
from ..records import Cost, RunStatus
from ..runtime import Engine, HarnessSpec
from ..runtime.engine import RetryPolicy, run_family
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

# store record kind of a durable unit outcome (Evaluator(durable_key=...)): host-only, never exported
DURABLE_KIND = "evolve_unit"


def outcome_to_record(o: RunOutcome) -> dict[str, Any]:
    return {"task_id": o.task_id, "run_id": o.run_id, "seed": o.seed, "score": o.score, "verified": o.verified,
            "status": o.status, "cost": o.cost.model_dump(mode="json"),
            "failure_signatures": list(o.failure_signatures), "cert_label": o.cert_label}


def outcome_from_record(r: dict[str, Any]) -> RunOutcome:
    score = r["score"]
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))):
        raise ValueError("malformed durable unit outcome (score)")  # fail closed: never guess a score
    return RunOutcome(str(r["task_id"]), str(r["run_id"]), int(r["seed"]), None if score is None else float(score),
                      bool(r["verified"]), str(r["status"]), Cost.model_validate(r["cost"]),
                      [str(s) for s in r["failure_signatures"]], r.get("cert_label"))


@dataclass
class Attempt:
    """One attempt (number `n`, 0 = first) at evaluating `unit` with `spec` (Evaluator.begin/finish). `since` = the
    last pre-registration ledger seq before it started (R2-2 check_run)."""

    key: tuple[str, str, int]
    spec: HarnessSpec
    unit: tuple[str, int]
    n: int
    since: int


def _signature(text: str) -> str:
    """Normalise an error into a clusterable signature (drop numbers, paths, quotes)."""
    text = re.sub(r"[A-Za-z]:\\[^\s'\"]+|/[^\s'\"]+", "<path>", text)
    text = re.sub(r"\d+(\.\d+)?", "<n>", text)
    text = re.sub(r"'[^']*'|\"[^\"]*\"", "<s>", text)
    return text.strip()[:160]


EMBARGOED_SIGNATURE = "<embargoed>"


def failure_signatures(store: EvidenceStore, run_id: str) -> list[str]:
    """Clusterable failure signatures of a run (the proposer sees them). A step whose output carries target labels
    (runtime/taint.py) contributes only `<node>: <embargoed>` (and not even the node when it ran because of a
    target-dependent routing decision); the run's end detail is withheld once any step is labelled."""
    from ..runtime.taint import step_labels
    sigs = []
    labels = step_labels(store, run_id)
    tainted = False
    for step in store.trace(run_id):
        lab = labels.get(step.index)
        if lab is not None and lab.tainted:
            tainted = True
            out = step.output if isinstance(step.output, dict) else {}
            if step.error or out.get("run_error") or out.get("route") == "give_up":
                node = EMBARGOED_SIGNATURE if lab.control else step.node_id
                sigs.append(f"{node}: {EMBARGOED_SIGNATURE}")
            continue
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
        why = EMBARGOED_SIGNATURE if tainted else _signature(detail.splitlines()[0] if detail else "")
        sigs.append(f"run: {status.value}: {why}")
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
                sealed_access: object | None = None, releases: Releases | None = None) -> RunOutcome:
    """`releases`: where the score's verdict bits are released (default: the store's ledger, live mode). Evolve-
    split scores reach the proposer, so their bits are emitted on every target the run read privately.

    = `finish_run(execute_run(...))`. The split exists for the per-completion race (control/evalrace.py): only
    `execute_run` runs on a worker thread; `finish_run` (the release emission, a write to the shared Releases
    object and the ledger) runs on the single writer thread."""
    return finish_run(execute_run(spec, task, seed, backends=backends, store=store, sandbox_factory=sandbox_factory,
                                  budget_limits=budget_limits, retry=retry, sealed_access=sealed_access),
                      store=store, releases=releases)


@dataclass
class ExecutedRun:
    """A finished harness run whose score is not computed yet (`finish_run`). `infra` is set instead of `result`
    when the sandbox could not even be created. `targets` = datasets whose private files were read while it ran
    (per run: reads naming it, no run or an unknown run; a concurrent run's own reads do not count)."""

    spec_hash: str
    task: SuiteTask
    seed: int
    result: Any = None  # runtime.engine.RunResult
    infra: RunOutcome | None = None
    targets: list[str] = field(default_factory=list)


def execute_run(spec: HarnessSpec, task: SuiteTask, seed: int, *, backends: dict[str, Backend],
                store: EvidenceStore, sandbox_factory: SandboxFactory,
                budget_limits: dict[str, float | None] | None = None, retry: RetryPolicy | None = None,
                sealed_access: object | None = None) -> ExecutedRun:
    """Run the harness; touch no shared in-memory state (the store is internally locked)."""
    if task.split == "sealed" and sealed_access is not _SEALED_ACCESS:  # invariant 6: sealed only via finalize
        raise ValueError(f"task {task.task_id} is in the sealed split: only `chh finalize` (a human action) runs it")
    h = spec.spec_hash()
    try:
        sandbox = sandbox_factory(f"{h[:10]}-{task.task_id}-{seed}")
    except InfraError as e:  # e.g. Docker daemon down before the run even started
        return ExecutedRun(h, task, seed, infra=RunOutcome(
            task.task_id, "", seed, None, False, RunStatus.INFRA_FAILED.value, Cost(),
            [f"run: infra_failed: {_signature(str(e).splitlines()[0] if str(e) else '')}"]))
    eng = Engine(spec, backends=backends, store=store, sandbox=sandbox,
                 budget=Budget(limits=budget_limits) if budget_limits else None, retry=retry or RetryPolicy())
    rows = store.ledger_rows()
    before = rows[-1][0] if rows else 0
    res = eng.run(task.task_id, {"task_dir": str(task.task_dir)}, seed=seed)
    # per run, fail closed (kerneld/releases.attributed_reads): a concurrent run's own reads do not count, reads
    # naming no run or an unknown run do; so does every target the run's persisted labels carry (e.g. a read a step
    # made itself naming another run, runtime/taint.persisted_run_label)
    from ..runtime.taint import persisted_run_label
    targets = (sorted(set(attributed_reads(store, after_seq=before, run_ids=run_family(res.run_id))[0])
                      | persisted_run_label(store, res.run_id))
               if task.split == "evolve" else [])
    return ExecutedRun(h, task, seed, result=res, targets=targets)


def finish_run(ex: ExecutedRun, *, store: EvidenceStore, releases: Releases | None = None) -> RunOutcome:
    """The run's score: verdict bits (released on `ex.targets` for an evolve-split run) plus the dev term."""
    if ex.infra is not None:
        return ex.infra
    res, task, seed = ex.result, ex.task, ex.seed
    bits = verdict_bits(res.state, res.status) if isinstance(res.state, dict) else verdict_bits({}, res.status)
    if task.split == "evolve":
        rel = releases if releases is not None else Releases.live(store)
        bits = rel.emit(bits, targets=ex.targets, channel="evolution_score", alphabet="verdict_bit",
                        note="evolve.score:verdict", key=f"{ex.spec_hash}|{task.task_id}|{seed}")
    score, verified = combine(res.status, bits, dev_term(res.state) if isinstance(res.state, dict) else 0.0)
    cert = res.state.get("certificate")
    label = cert.get("label") if isinstance(cert, dict) else None
    return RunOutcome(task.task_id, res.run_id, seed, score, verified, res.status.value, res.cost,
                      failure_signatures(store, res.run_id), cert_label=label)
