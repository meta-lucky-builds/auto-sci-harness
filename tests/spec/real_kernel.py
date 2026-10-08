"""Replays an abstract History (history.py) against the REAL Kernel in a fresh store, event by event.

Physical encoding (as history.py documents): every dataset has one public file `public.csv` (columns x1,x2 for
kind "features", x1,x2,label for "labelled"), rows from a global example pool, and optionally a private
`labels.csv` whose bytes are a pure function of its token (equal tokens = byte-identical files). All datasets have
`N` examples, so a token's labels always fit. Strong subjects predict the private labels exactly; every other arm
predicts the constant "0". The replayer returns the stored predictions (reproduction is exact).
"""

from __future__ import annotations

import csv
import io
import random
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from history import ApproveRole, Artifact, Certify, ClaimSpec, Dataset, Event, Prereg, Read, Record, Verdict

from custom_harness.kernel import (
    AnalysisPlan,
    ArtifactRef,
    Claim,
    ClaimKind,
    DatasetRole,
    Effect,
    ExecRecord,
    InputRef,
    Kernel,
    MetricRef,
    Scope,
    VaultError,
)
from custom_harness.records import Run, StepKind, TraceStep
from custom_harness.store import EvidenceStore
from custom_harness.verify import metrics as M

N = 40
RESOLVE_BY = 4_102_444_800.0  # fixed, so claim hashes are reproducible
_FAMILY = re.compile(r"^(.+): (\d+) claim\(s\) tested on this target")


def example_row(i: int) -> tuple[str, str, str]:
    r = random.Random(i)
    return f"{r.uniform(-5, 5):.6f}", f"{r.uniform(-5, 5):.6f}", str(r.randrange(3))


def token_labels(token: str) -> list[str]:
    r = random.Random(f"labels:{token}")
    return [str(r.randrange(3)) for _ in range(N)]


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode()


def public_bytes(d: Dataset) -> bytes:
    rows = [example_row(i) for i in sorted(d.examples)]
    if d.kind == "features":
        return _csv([["x1", "x2"], *[[a, b] for a, b, _ in rows]])
    return _csv([["x1", "x2", "label"], *[list(r) for r in rows]])


def private_bytes(token: str) -> bytes:
    return _csv([["label"], *[[y] for y in token_labels(token)]])


class StoredReplayer:
    def __init__(self) -> None:
        self.calls = 0

    def replay(self, record: ExecRecord, vault: Any) -> list[str] | None:
        self.calls += 1
        preds = vault.store.blobs.get_json(record.preds_ref)
        return list(preds)


def to_claim(spec: ClaimSpec, refs: dict[str, ArtifactRef]) -> Claim:
    missing = ArtifactRef(artifact_id="missing", hash="0" * 64)
    return Claim(kind=ClaimKind(spec.kind), statement=f"claim {spec.key}", subject=refs.get(spec.subject, missing),
                 comparator=refs.get(spec.comparator, missing), metric=MetricRef(id="accuracy", higher_is_better=True),
                 effect=Effect(min_effect=spec.min_effect, predicted_lo=spec.min_effect, predicted_hi=0.95,
                               probability=0.7),
                 scope=Scope(datasets=spec.targets), development=spec.development,
                 generalization_targets=spec.targets,
                 analysis_plan=AnalysisPlan(seeds=spec.seeds, alpha=spec.alpha, n_comparisons=spec.n_comparisons,
                                            n_boot=1000, reproduce="all"),
                 resolve_by=RESOLVE_BY)


def verdict_of(cert: Any) -> Verdict:
    family: dict[str, int] = {}
    for o in cert.checks["K4"].observations:
        m = _FAMILY.match(o)
        if m:
            family[m.group(1)] = int(m.group(2))
    return Verdict(checks={k: v.status for k, v in cert.checks.items()}, label=cert.label.value, family=family)


@dataclass
class RealWorld:
    """One real store. `apply(event)` executes the event; it returns the events actually logged (the event itself,
    plus Kernel-made reads after a Certify), or () when the Kernel refused it."""

    root: Path
    kernel: Kernel = field(init=False)
    refs: dict[str, ArtifactRef] = field(default_factory=dict)
    datasets: dict[str, Dataset] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)  # claim hash -> key

    def __post_init__(self) -> None:
        self.store = EvidenceStore(self.root / "store")
        self.kernel = Kernel(self.store, replayer=StoredReplayer(), clock=lambda: 1.0)
        self.kernel.approve_adapter("ml", approver="spec-harness")
        self._runs = 0

    def close(self) -> None:
        self.store.close()

    def claim(self, spec: ClaimSpec) -> Claim:
        c = to_claim(spec, self.refs)
        self.keys[c.claim_hash()] = spec.key
        return c

    def apply(self, ev: Event) -> tuple[Event, ...]:
        v = self.kernel.vault
        if isinstance(ev, Dataset):
            private = {"labels.csv": private_bytes(ev.private)} if ev.private is not None else {}
            v.register_dataset(ev.id, DatasetRole(ev.role), public={"public.csv": public_bytes(ev)}, private=private)
            self.datasets[ev.id] = ev
            return (ev,)
        if isinstance(ev, Artifact):
            rec = v.register_artifact(ev.id, f"# {ev.kind} program {ev.id}\nprint('hello')\n".encode(), ev.kind)
            self.refs[ev.id] = ArtifactRef(artifact_id=ev.id, hash=rec.hash)
            return (ev,)
        if isinstance(ev, Prereg):
            self.kernel.preregister(self.claim(ev.claim))
            return (ev,)
        if isinstance(ev, ApproveRole):
            try:
                self.kernel.approve_role(ev.dataset, approver="spec-human")
            except VaultError:  # not (yet) registered: no approval is written
                return ()
            return (ev,)
        if isinstance(ev, Read):
            ch = self._hash_of(ev.claim)
            try:
                v.read(ev.dataset, "labels.csv", purpose=ev.purpose, claim_hash=ch, arm=ev.arm, seed=ev.seed)
            except VaultError:
                return ()
            return (ev,)
        if isinstance(ev, Certify):
            before = len(self.kernel.ledger.entries("data_access"))
            self.last = self.certify(ev.claim, ev.records)
            new = self.kernel.ledger.entries("data_access")[before:]
            reads = tuple(Read(dataset=e.body["dataset_id"], purpose=e.body["purpose"],
                               claim=self.keys.get(str(e.body.get("claim_hash"))), arm=e.body.get("arm"),
                               seed=e.body.get("seed")) for e in new)
            return (ev, *reads)
        raise TypeError(ev)

    def _hash_of(self, key: str | None) -> str | None:
        if key is None:
            return None
        for h, k in self.keys.items():
            if k == key:
                return h
        return "f" * 64  # a claim the harness never built: an unregistered hash

    def certify(self, spec: ClaimSpec, records: tuple[Record, ...]) -> Verdict:
        claim = self.claim(spec)
        ids = [self._record(claim, spec, r) for r in records]
        self.last_cert = self.kernel.certify(claim, ids)
        return verdict_of(self.last_cert)

    def _record(self, claim: Claim, spec: ClaimSpec, r: Record) -> str:
        d = self.datasets.get(r.target)
        labels = token_labels(d.private) if d is not None and d.private is not None else ["0"] * N
        preds = labels if (r.arm == "subject" and spec.strong) else ["0"] * N
        self._runs += 1
        run = Run(harness_id="spec", harness_hash="spec", task_id=r.target, seed=r.seed)
        self.store.add_run(run)
        now = time.time()
        self.store.add_step(TraceStep(run_id=run.id, index=0, node_id="evaluate", kind=StepKind.TOOL,
                                      input={"arm": r.arm}, output={"note": "evaluated"}, started_at=now,
                                      ended_at=now))
        blob = (self.store.blobs.put(public_bytes(d)) if d is not None else "0" * 64)
        rec = ExecRecord(claim_hash=claim.claim_hash(), arm=r.arm,
                         artifact_hash=(claim.subject if r.arm == "subject" else claim.comparator).hash,
                         inputs={"test": InputRef(dataset_id=r.target, file="public.csv", blob=blob)},
                         target=r.target, label_file="labels.csv", seed=r.seed, env={"sandbox": "spec"},
                         limits={"timeout_s": 10.0}, preds_ref=self.store.blobs.put_json(preds), metric="accuracy",
                         score=M.score("accuracy", labels, preds), run_id=run.id)
        return self.kernel.record_evaluation(rec)


def fresh() -> tuple[RealWorld, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory(prefix="spec-")
    return RealWorld(Path(tmp.name)), tmp
