"""Invariant 14 (no claim above its evidence class) for exact certificates (reported by s3-domain, verified here).

Before 2026-10-09 `k1_computational` checked neither `claim.statement` nor `scope.conditions`, so a computational
claim worded as a physical one ("this circuit will work when built", scope "holds_in: physical world") was
CERTIFIED-EXACT with that prose on the certificate, for the `lp` adapter and for any adapter shaped like the circuit
adapter of origin/s3-domain (stubbed here: tests/_exact_stub.py). Now the statement must be exactly the adapter's
scope rendering and the scope has no conditions; an adapter without a rendering fails K1 (fail closed). The simulated
class already had this rule (tests/test_kernel_sim.py); the formal class: tests/test_kernel_formal.py."""

from __future__ import annotations

import time
from fractions import Fraction

import pytest

import _exact_stub
from custom_harness.kernel import (
    AnalysisPlan,
    ArtifactRef,
    Claim,
    Effect,
    EvidenceClass,
    Label,
    MetricRef,
    Scope,
    adapters,
    canonical_json,
    lp,
)
from kernel_world import World

PROBLEM = canonical_json({"format": "chh-lp/1", "sense": "max", "c": ["1", "1"], "A": [["2", "1"], ["1", "3"]],
                          "b": ["4", "5"]})
CERT = canonical_json({"format": "chh-lp-certificate/1", "x": ["7/5", "6/5"], "y": ["2/5", "1/5"]})
PHYSICAL = "this production plan will be feasible when the factory is built"


def _certify(w: World, metric: str, problem: bytes, cert: bytes, value: str, statement: str,
             conditions: dict[str, str] | None = None):
    ref = ArtifactRef(artifact_id=f"inst-{metric}", hash=w.vault.register_artifact(f"inst-{metric}", problem,
                                                                                   "problem").hash)
    doc = Claim(kind="improvement", statement=statement, subject=ref, comparator=ref,
                metric=MetricRef(id=metric, higher_is_better=True),
                effect=Effect(min_effect=0.0, predicted_lo=0.0, predicted_hi=0.0, probability=0.9),
                scope=Scope(datasets=(), conditions=conditions or {}), development=(), generalization_targets=(),
                analysis_plan=AnalysisPlan(seeds=()), resolve_by=time.time() + 86400).model_dump(mode="json")
    claim = Claim.model_validate(doc | {"kind": "computed_value", "evidence_class": "computational", "value": value})
    w.kernel.preregister(claim)
    aid = f"cert-{claim.claim_hash()[:12]}"
    w.vault.register_artifact(aid, cert, "certificate")
    return w.kernel.certify(claim, [w.kernel.record_certificate(claim, aid)])


@pytest.fixture()
def world(tmp_path, monkeypatch):
    w = World(tmp_path / "w", seed=11, n_train=30, n_test=30)
    stub = adapters.AdapterSpec(name="circuit-stub", version="1", classes=frozenset({EvidenceClass.COMPUTATIONAL}),
                                modules=("_exact_stub",), metrics=frozenset({"circuit_value"}),
                                exact_module="_exact_stub")
    monkeypatch.setitem(adapters.REGISTRY, "circuit-stub", (stub, ()))
    for name in ("lp", "circuit-stub"):
        w.kernel.approve_adapter(name, approver="test-world")
    yield w
    w.close()


def test_lp_claim_worded_as_a_physical_claim_is_not_certified(world):
    cert = _certify(world, "lp_optimum", PROBLEM, CERT, "13/5", PHYSICAL)
    assert cert.label == Label.REJECTED, "an exact certificate carried a physical claim"
    assert cert.failed() == ["K1"] and "K1_STATEMENT" in cert.checks["K1"].codes
    honest = lp.scope_phrase(PROBLEM, Fraction(13, 5))
    assert _certify(world, "lp_optimum", PROBLEM, CERT, "13/5", honest).label == Label.CERTIFIED_EXACT
    scoped = _certify(world, "lp_optimum", PROBLEM, CERT, "13/5", honest, {"holds_in": "the physical world"})
    assert scoped.failed() == ["K1"] and "K1_SCOPE" in scoped.checks["K1"].codes


def test_circuit_shaped_claim_worded_as_a_physical_claim_is_not_certified(world):
    problem, cert = canonical_json({"value": "5/2"}), canonical_json({"value": "5/2"})
    bad = _certify(world, "circuit_value", problem, cert, "5/2", "this circuit will work when built")
    assert bad.label == Label.REJECTED and bad.failed() == ["K1"]
    good = _certify(world, "circuit_value", problem, cert, "5/2", _exact_stub.scope_phrase(problem, Fraction(5, 2)))
    assert good.label == Label.CERTIFIED_EXACT, {k: v.reasons for k, v in good.checks.items() if v.status == "fail"}


def test_an_adapter_without_a_scope_rendering_fails_k1(world, monkeypatch):
    monkeypatch.delattr(_exact_stub, "scope_phrase")
    problem = canonical_json({"value": "1"})
    cert = _certify(world, "circuit_value", problem, problem, "1", "anything")
    assert cert.failed() == ["K1"] and any("renders no scope phrase" in r for r in cert.checks["K1"].reasons)
