"""`chh memory`: inspection, retraction sync, Kernel-derived negative memory and human revocation (GOAL §5 L5)."""

from __future__ import annotations

import json
import os
import time
from fractions import Fraction

import pytest

from custom_harness.cli import main
from custom_harness.config import data_dir
from custom_harness.kernel import (
    AnalysisPlan,
    ArtifactRef,
    Claim,
    ClaimKind,
    Effect,
    Label,
    MetricRef,
    Scope,
    canonical_json,
)
from custom_harness.kernel.lp import scope_phrase as lp_phrase
from custom_harness.memory import Kind, MemoryStore, Proposal, Status
from kernel_world import World
from test_memory import LP_CERT, LP_PROBLEM

os.environ.setdefault("CHH_ALLOW_UNISOLATED", "1")


def _lp_cert(w: World, tag: str, value: str = "13/5") -> str:
    problem = {**LP_PROBLEM, "comment": tag}
    ref = ArtifactRef(artifact_id=tag, hash=w.vault.register_artifact(tag, canonical_json(problem), "problem").hash)
    doc = Claim(kind=ClaimKind.IMPROVEMENT, statement=lp_phrase(canonical_json(problem), Fraction(value)), subject=ref,
                comparator=ref,
                metric=MetricRef(id="lp_optimum", higher_is_better=True),
                effect=Effect(min_effect=0.0, predicted_lo=0.0, predicted_hi=0.0, probability=0.9),
                scope=Scope(datasets=()), development=(), generalization_targets=(),
                analysis_plan=AnalysisPlan(seeds=()), resolve_by=time.time() + 86400).model_dump(mode="json")
    claim = Claim.model_validate(doc | {"kind": "computed_value", "evidence_class": "computational", "value": value})
    w.kernel.preregister(claim)
    art = w.vault.register_artifact(f"cert-{tag}", canonical_json(LP_CERT), "certificate").artifact_id
    return w.kernel.certify(claim, [w.kernel.record_certificate(claim, art)]).cert_hash()


@pytest.fixture
def world():
    w = World(data_dir(), seed=2, n_train=30, n_test=30)  # the CLI's store: <data dir>/store
    w.kernel.approve_adapter("lp", approver="test-world")
    yield w
    w.close()


def test_cli_memory_lists_syncs_and_revokes(world, capsys):
    cert = _lp_cert(world, "cli-a")
    assert world.kernel.certificate(cert).label == Label.CERTIFIED_EXACT
    mem = MemoryStore(world.kernel)
    a = mem.submit(Proposal(kind=Kind.POSITIVE, title="cli entry", text="t", evidence=(cert,)), writer="w").entry_id
    b = mem.submit(Proposal(kind=Kind.POSITIVE, title="child", text="t", evidence=(cert,), parents=(a,)),
                   writer="w").entry_id
    assert main(["memory", "list"]) == 0
    out = capsys.readouterr().out
    assert a[:20] in out and "active" in out and "cli entry" in out
    assert main(["memory", "unused"]) == 0
    assert set(json.loads(capsys.readouterr().out)) == {a, b}
    assert main(["memory", "verify-log"]) == 0
    assert json.loads(capsys.readouterr().out)["intact"] is True
    with pytest.raises(SystemExit, match="--reason"):
        main(["memory", "revoke", a])
    assert main(["memory", "revoke", a, "--reason", "bad", "--by", "alice"]) == 0
    assert set(json.loads(capsys.readouterr().out)["revoked"]) == {a, b}
    assert mem.status(b) == Status.REVOKED


def test_cli_memory_sync_processes_kernel_retractions(world, capsys):
    cert = _lp_cert(world, "cli-b")
    mem = MemoryStore(world.kernel)
    a = mem.submit(Proposal(kind=Kind.POSITIVE, title="to retract", text="t", evidence=(cert,)), writer="w").entry_id
    world.kernel.retract(cert, "wrong")
    assert main(["memory", "sync"]) == 0
    (cascade,) = json.loads(capsys.readouterr().out)["cascades"]
    assert cascade["revoked"] == [a]
    assert main(["memory", "harvest-negatives"]) == 0
    assert json.loads(capsys.readouterr().out) == []  # no verified failure in this ledger
