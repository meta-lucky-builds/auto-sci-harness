"""Kill switch (GOAL §3 L1 "admit/timebox/kill", §5 L9 "kill switch").

`chh kill RUN_ID --by NAME` is a HUMAN action. It appends a `run_kill` entry to the Kernel ledger, then marks the run
FAILED at once. From then on:

- the engine stops the run before its next step and ends it FAILED, never RUNNING or COMPLETED (runtime/engine.py
  reads `kill_entry` before every step and again before it writes the final status);
- a killed run is never resumable (`Engine.resume` refuses it, even if a later crash wrote "engine crash");
- the Kernel a module gets from `ModuleContext.kernel()` is a `RunGuardedKernel`. It refuses to pre-register, record
  evidence or certify for a killed run. A certificate whose issuing began before the kill but was recorded after it
  is retracted at once, with the reason recorded. So no certificate recorded after the kill stays active.

Automated (non-human) stops: an LLM controller acts only through the downgrade-only gate (control/downgrade.py,
wired by studio/controller.py). A kill it applies is ledgered as a `controller_action` record, never as `run_kill`
(owner decision: `run_kill` stays human-only). `kill_entry` returns either, so the engine, resume, fork and the
run-guarded Kernel stop a controller-killed run exactly like a human-killed one.

Honest limits:
- A step already in flight when the kill lands runs to its end. Its model calls are spent and recorded, but its
  output does not drive any further step.
- Trusted module code can reach the real store (`kernel.store`), as runtime/modules.py says. A module written to
  ignore the kill is not stopped by this code; modules are human-written, registry-only code.
- The Kernel does not check that `by` names a human. The CLI is the trust boundary, as it is for `chh approve`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..kernel import KernelError
from ..kernel.claim import Claim
from ..kernel.ledger import Entry, Ledger
from ..kerneld.scope import ScopedKernel
from ..records import RunStatus
from ..store import EvidenceStore

KILL_KIND = "run_kill"
# closed set: the reason is a code, never free text (it reaches the run status other code reads)
KILL_REASONS = ("human_stop", "runaway_cost", "unsafe_behaviour", "wrong_config", "other")


class KillRefused(ValueError):
    pass


def human_kill_entry(store: EvidenceStore, run_id: str) -> Entry | None:
    """The first intact HUMAN kill (`run_kill` ledger entry) of `run_id`, or None."""
    for e in Ledger(store).entries(KILL_KIND):
        if e.intact() and e.body.get("run_id") == run_id and str(e.body.get("by", "")).strip():
            return e
    return None


@dataclass(frozen=True)
class ControllerStop:
    """An automated controller's kill of a run, applied by the downgrade-only gate (control/downgrade.py) and
    ledgered as a `controller_action` record (NEVER as `run_kill`, which stays human-only). `n` = its position in
    `store.records("controller_action")`."""

    n: int
    body: dict[str, Any]


def controller_stop(store: EvidenceStore, run_id: str) -> ControllerStop | None:
    """The first kill of `run_id` that the downgrade gate APPLIED (code-checked evidence), or None."""
    from ..control.downgrade import ACTION_KIND, EVIDENCE_KINDS
    for i, r in enumerate(store.records(ACTION_KIND)):
        if (r.get("action_type") == "kill" and r.get("outcome") == "applied" and r.get("reason") == "ok"
                and r.get("target_id") == run_id and r.get("evidence_kind") in EVIDENCE_KINDS):
            return ControllerStop(i, r)
    return None


def kill_entry(store: EvidenceStore, run_id: str) -> Entry | ControllerStop | None:
    """What stops `run_id`: the first intact human kill, else the first kill applied by the downgrade gate for an
    automated controller, else None. Every engine check (before each step, at the end, resume, fork) and the
    run-guarded Kernel read this, so both stop a run the same way; only the human one is a `run_kill` entry."""
    return human_kill_entry(store, run_id) or controller_stop(store, run_id)


def killed_detail(entry: Entry | ControllerStop) -> str:
    if isinstance(entry, ControllerStop):
        return (f"stopped by an automated controller ({entry.body.get('controller_id')}, downgrade gate, evidence "
                f"{entry.body.get('evidence_kind')}, controller_action record #{entry.n})")
    return (f"killed by a human ({entry.body.get('by')}, reason {entry.body.get('reason_code')}, "
            f"run_kill ledger seq {entry.seq})")


def controller_kill_effect(store: EvidenceStore) -> Callable[[str, str], None]:
    """The `kill(run_id, code)` callback the downgrade gate applies for an automated controller (studio/controller.py
    builds the gate with it). It only marks the run FAILED (the gate then appends the `controller_action` record that
    every engine check honours); it writes no `run_kill` entry. Refused (raises, so the gate ledgers effect_error) for
    a code outside the gate's closed set or a run that is no longer killable."""
    from ..control.downgrade import EVIDENCE_KINDS

    def kill(run_id: str, code: str) -> None:
        kind = code.removeprefix("controller_")
        if not code.startswith("controller_") or kind not in EVIDENCE_KINDS:
            raise KillRefused(f"not a downgrade-gate kill code: {code!r}")
        status, detail = store.run_status(run_id)
        if not _killable(status, detail) or kill_entry(store, run_id) is not None:
            raise KillRefused(f"run {run_id} is already {status.value}: nothing to stop")
        store.set_run_status(run_id, RunStatus.FAILED, f"stopped by an automated controller (downgrade gate, "
                                                       f"evidence {kind}); see the controller_action record")
    return kill


def _killable(status: RunStatus, detail: str) -> bool:
    """A run that can still do something: running, paused, or resumable after an infra failure or engine crash."""
    return (status in (RunStatus.RUNNING, RunStatus.PAUSED, RunStatus.INFRA_FAILED)
            or (status == RunStatus.FAILED and detail.startswith("engine crash")))


def kill_run(store: EvidenceStore, run_id: str, *, by: str, reason_code: str = "human_stop") -> Entry:
    """HUMAN action: stop a run safely. Idempotent: a second kill returns the first entry."""
    if not by.strip():
        raise KillRefused("--by must name the human taking this action")
    if reason_code not in KILL_REASONS:
        raise KillRefused(f"reason code {reason_code!r} is not one of {KILL_REASONS}")
    try:
        status, detail = store.run_status(run_id)
    except KeyError:
        raise KillRefused(f"no run {run_id!r} in this store") from None
    existing = human_kill_entry(store, run_id)
    if existing is not None:
        return existing
    if not _killable(status, detail):
        raise KillRefused(f"run {run_id} is already {status.value} ({detail[:80]}): nothing to stop")
    entry = Ledger(store).append(KILL_KIND, {"run_id": run_id, "by": by.strip(), "reason_code": reason_code,
                                             "status_before": status.value})
    store.set_run_status(run_id, RunStatus.FAILED, killed_detail(entry))
    return entry


class _ModuleLedger(Ledger):
    """The ledger a module's Kernel writes through: it cannot write the L9 human-only kinds."""

    HUMAN_ONLY = frozenset({KILL_KIND, "tier_policy"})

    def append(self, kind: str, body: dict[str, Any]) -> Entry:
        if kind in self.HUMAN_ONLY:
            raise KernelError(f"ledger kind {kind!r} is a human action (L9 CLI); module code cannot write it")
        return super().append(kind, body)


class RunGuardedKernel(ScopedKernel):
    """The Kernel bound to one run (what `ModuleContext.kernel()` returns): refuses every claim-making call once the
    run is killed, and retracts a certificate that was recorded after the kill. A ScopedKernel: its private reads
    happen only inside kerneld's per-claim grants (kerneld/scope.py, GOAL §16.5.2)."""

    run_id: str  # the binding (narrower than Kernel.run_id); recorded on prereg/certificate entries (s4-kernel)

    def __init__(self, store: EvidenceStore, *, run_id: str, **kw: Any):
        super().__init__(store, **kw)
        from ..kernel.vault import Vault
        self.run_id = run_id
        self.ledger = _ModuleLedger(self.store)  # self.store: the HostStore view (ScopedKernel), never the raw store
        self.vault = Vault(self.store, self.ledger)

    def _refuse_if_killed(self, what: str) -> None:
        kill = kill_entry(self.store, self.run_id)
        if kill is not None:
            raise KernelError(f"{what} refused: run {self.run_id} was {killed_detail(kill)}")
        # an ABLATED run (custom_harness/ablation.py) pre-registers, records and certifies nothing (fail closed)
        off = self.store.run_ablated(self.run_id)
        if off:
            raise KernelError(f"{what} refused: run {self.run_id} is ABLATED (switched off: {', '.join(off)})")

    def preregister(self, claim: Claim) -> Entry:
        self._refuse_if_killed("pre-registration")
        return super().preregister(claim)

    def record_evaluation(self, record: Any) -> str:
        self._refuse_if_killed("evidence")
        return super().record_evaluation(record)

    def record_certificate(self, claim: Claim, artifact_id: str) -> str:
        self._refuse_if_killed("evidence")
        return super().record_certificate(claim, artifact_id)

    def bundle(self, cert_hash: str, out_path: Any) -> str:
        """A module may bundle only into the data dir's `bundles/` (the research loop's place), never to a path of its
        choosing, and not on a killed run (s4-kerneld-review finding 2; every bundling is also recorded)."""
        from pathlib import Path

        from ..config import data_dir
        from ..pathsafe import is_within
        self._refuse_if_killed("bundling")
        if not is_within(Path(out_path), data_dir() / "bundles"):
            raise KernelError(f"bundle refused: a module bundles only into {data_dir() / 'bundles'}")
        return super().bundle(cert_hash, out_path)

    def certify(self, claim: Claim, evidence_ids: Sequence[str], *, full_chain: bool = True,
                record: bool = True, as_of: int | None = None) -> Any:
        self._refuse_if_killed("certification")
        cert = super().certify(claim, list(evidence_ids), full_chain=full_chain, record=record, as_of=as_of)
        if record and as_of is None:
            kill = kill_entry(self.store, self.run_id)
            if kill is not None:  # the kill landed while the checks ran: the certificate must not stand
                self.retract(cert.cert_hash(), f"run_killed: run {self.run_id} was {killed_detail(kill)} while "
                                               f"this certificate was being issued")
        return cert
