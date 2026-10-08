"""M5 engineering adapter `circuit`: exact linear DC circuit analysis (computational class, T1; GOAL §4.7, §5
"Domain adapters", §15; invariants 13 and 14).

A claim states the exact value of one queried quantity of a registered netlist in the IDEAL LINEAR DC MODEL (or that
its MNA system is singular / inconsistent). The certificate is re-checked by a checker that never solves; K8 re-checks
the solution element by element (KCL at every node, constitutive laws, Tellegen, SI dimensions).

Approval convention (stated, as in the lp/sim/lean tests): Kernel-path tests approve the adapter in a throwaway
test ledger with approver "test-world" (the test stand-in for the human). Nothing here approves it in any real
store; `test_registered_but_unapproved_*` shows it is unusable until a human runs `chh approve-adapter circuit`.
"""

from __future__ import annotations

import hashlib
import json
import time
from fractions import Fraction

import pytest

from custom_harness.adapters.circuit import check as C
from custom_harness.adapters.circuit import screen as CS
from custom_harness.adapters.circuit import solver as S
from custom_harness.adapters.circuit import validation as V
from custom_harness.kernel import (
    CHECK_IDS,
    AnalysisPlan,
    ArtifactRef,
    Certificate,
    Claim,
    ClaimKind,
    Effect,
    EvidenceClass,
    Label,
    MetricRef,
    Scope,
)
from custom_harness.kernel import adapters as A
from custom_harness.kernel import checks as K
from custom_harness.tiers import Tier
from custom_harness.verify import screen as G
from kernel_world import World

DIV = V.problem(V.DIVIDER, V.node("n2"))


def reasons(res) -> str:
    return " | ".join(res.reasons)


# ------------------------------------------------------------------ the checker alone
def test_validation_evidence_passes_and_is_registered():
    assert A.validate(V.CIRCUIT_CHECKS) == []
    assert len(V.CIRCUIT_CHECKS) >= 30
    spec, checks = A.REGISTRY["circuit"]
    assert checks is V.CIRCUIT_CHECKS
    assert spec.classes == frozenset({EvidenceClass.COMPUTATIONAL}) and spec.metrics == frozenset({C.METRIC})
    assert spec.exact_module == "custom_harness.adapters.circuit.check"
    covered = A.covered_modules(spec)  # the checker, its validation evidence and the solver are its identity
    for mod in ("custom_harness.adapters.circuit.check", "custom_harness.adapters.circuit.validation",
                "custom_harness.adapters.circuit.solver", "custom_harness.adapters.circuit",
                "custom_harness.adapters"):
        assert mod in covered, mod
    assert len(A.adapter_hash(spec)) == 64


def test_a_failing_validation_check_makes_the_adapter_unapprovable(tmp_path, monkeypatch):
    spec, _ = A.REGISTRY["circuit"]
    monkeypatch.setitem(A.REGISTRY, "circuit", (spec, (*V.CIRCUIT_CHECKS, ("planted failure", lambda: False))))
    w = World(tmp_path / "w", approve_adapters=False)
    try:
        with pytest.raises(A.AdapterError, match=r"failed its validation: \['planted failure'\]"):
            w.kernel.approve_adapter("circuit", approver="test-world")
        assert w.kernel.ledger.entries("adapter_approval") == []
    finally:
        w.close()


def test_every_validation_check_fails_when_the_checker_accepts_everything(monkeypatch):
    """Tests of the tests (GOAL §16.6): a checker that returns no reasons must fail every fault-injection check."""
    monkeypatch.setattr(C, "check", lambda *a: [])
    monkeypatch.setattr(C, "bounds", lambda *a: [])
    failed = A.validate(V.CIRCUIT_CHECKS)
    faults = [n for n, _ in V.CIRCUIT_CHECKS if " is rejected" in n or " is refused" in n or "violates" in n
              or "catch" in n]
    assert len(faults) >= 20
    assert set(faults) - {"KCL at ground and Tellegen catch a wrong solution"} <= set(failed)


def test_golden_values_by_hand():
    assert S.certify(DIV)[0] == 6
    for n, v in V.CONTROLLED_GOLDEN.items():
        assert S.certify(V.problem(V.CONTROLLED, V.node(n), V.CONTROLLED_NODES))[0] == v
    assert S.certify(V.problem(V.CUTSET, V.SINGULAR))[0] == 1
    with pytest.raises(S.SolverError, match="singular"):
        S.certify(V.problem(V.PARALLEL_EQUAL, V.node("n1"), ("0", "n1")))


def test_mna_stamps_of_the_divider():
    a, b = C.mna(C.parse_problem(DIV))
    F = Fraction
    assert a == [[F(1, 2), F(-1, 2), F(1)], [F(-1, 2), F(5, 6), F(0)], [F(1), F(0), F(0)]]
    assert b == [F(0), F(0), F(10)]


def test_scope_phrase_names_the_ideal_model_and_disclaims_physical_circuits():
    phrase = C.scope_phrase(DIV, Fraction(6))
    sha = hashlib.sha256(DIV).hexdigest()[:12]
    model = "ideal linear DC circuit model chh-circuit/1"
    assert phrase.startswith(f"V(n2) = 6 V exactly, in the {model} of netlist {sha}")
    assert phrase.endswith("this says nothing about any physical circuit")


@pytest.mark.parametrize(("doc", "why"), [
    (b"{", "not JSON"),
    (b"[]", "not a chh-circuit/1"),
    (b" " * (C.MAX_BYTES + 1), "at most"),
])
def test_unreadable_documents(doc, why):
    assert any(why in r for r in C.check(doc, b"{}", Fraction(1)))
    assert any(why in r for r in C.bounds(doc, b"{}", Fraction(1)))


def _p(**changes) -> bytes:
    doc = json.loads(DIV) | changes
    return json.dumps(doc).encode()


@pytest.mark.parametrize(("problem", "why"), [
    (_p(comment=1), "'comment' must be a string"),
    (_p(nodes="0"), "'nodes' must be a list"),
    (_p(nodes=["0", "n1", "n1"]), "duplicate node names"),
    (_p(ground="g"), "ground 'g' is not in 'nodes'"),
    (_p(elements=[]), "'elements' must be a list"),
    (_p(elements=[*V.DIVIDER, V.r("R1", "n1", "0", "1")]), "duplicate element ids"),
    (_p(elements=[{"type": "diode"}]), "type must be one of"),
    (_p(elements=[V.r("R 1", "n1", "0", "1")]), "is not a name"),
    (_p(elements=[V.r("R1", "n1", "n9", "1")]), "not in 'nodes'"),
    (_p(elements=[{**V.r("R1", "n1", "0", "1"), "tol": "1/100"}]), "unexpected keys"),
    (_p(elements=[{"id": "R1", "type": "resistor", "pos": "n1", "neg": "0", "value": "1"}]), "missing keys"),
    (_p(elements=[V.r("R1", "n1", "0", "-1")]), "R > 0"),
    (_p(elements=[*V.DIVIDER, {"id": "F1", "type": "cccs", "pos": "n1", "neg": "0", "control": "R1", "value": "1",
                                "unit": "A/A"}]), "must name a vsource"),
    (_p(query=[]), "query must be an object"),
    (_p(query={"quantity": "node_voltage", "node": "n7", "unit": "V"}), "not in the netlist"),
    (_p(query={"quantity": "singular", "unit": "1", "node": "n1"}), "unexpected keys"),
    (_p(query={"quantity": "temperature", "unit": "1"}), "is not registered"),
    (_p(nodes=["0", *(f"n{i}" for i in range(C.MAX_NODES))]), "'nodes' must be a list of 2..40"),
    (_p(nodes=["0", *(f"n{i}" for i in range(39))],
        elements=[V.vs(f"V{i}", f"n{i % 39}", "0", "1") for i in range(C.MAX_UNKNOWNS)]),
     "exceed the registered bound"),
])
def test_problem_reader_refuses(problem, why):
    got = C.check(problem, b"{}", Fraction(6))
    assert any(why in r for r in got), got


def test_certificate_reader_refuses():
    _, cert = V.solved(DIV)
    cases = {
        "problem_sha256 must be a lowercase hex": {**cert, "problem_sha256": "ABC"},
        "dimension mismatch: x": {**cert, "x": ["1"]},
        "dimension mismatch: inverse must have": {**cert, "inverse": [["1"]]},
        "inverse[0]": {**cert, "inverse": [["1"], ["1"], ["1"]]},
    }
    for why, doc in cases.items():
        got = C.check(DIV, json.dumps(doc).encode(), Fraction(6))
        assert any(why in r for r in got), (why, got)


def test_reasons_are_capped():
    nodes = ("0", *(f"n{i}" for i in range(8)))
    els = [V.r(f"R{i}", f"n{i}", "0", "1") for i in range(8)]
    p = V.problem(els, V.node("n0"), nodes)
    value, cert = V.solved(p)
    assert value == 0
    bad = {**cert, "x": ["1"] * 8}
    got = C.check(p, json.dumps(bad).encode(), Fraction(0))
    assert any("... and 3 more A x != b rows" in r for r in got)
    assert any("... and 3 more law violations" in r for r in C.kirchhoff(C.parse_problem(p), [Fraction(1)] * 8))


def test_a_huge_claimed_value_is_flagged():
    huge = Fraction(10 ** (C.MAX_DIGITS + 1))
    _, cert = V.solved(DIV)
    data = json.dumps(cert).encode()
    assert any("exceeds 100 digits" in r for r in C.check(DIV, data, huge))
    assert any("exceeds 100 digits" in r for r in C.bounds(DIV, data, huge))


def test_k8_without_a_solution_checks_units_and_range_only():
    p = V.problem(V.PARALLEL_EQUAL, V.SINGULAR, ("0", "n1"))
    value, cert = V.solved(p)
    assert value == 1 and "x" not in cert
    assert C.bounds(p, json.dumps(cert).encode(), Fraction(1)) == []
    assert any("outside the range" in r for r in C.bounds(p, json.dumps(cert).encode(), Fraction(3)))
    assert any("unreadable" in r for r in C.bounds(p, b"{}", Fraction(1)))


def test_constitutive_law_violations_are_named():
    c = C.parse_problem(V.problem(V.CONTROLLED, V.node("n4"), V.CONTROLLED_NODES))
    value, cert = V.solved(V.problem(V.CONTROLLED, V.node("n4"), V.CONTROLLED_NODES))
    x = [Fraction(s) for s in cert["x"]]
    assert value == 10 and C.kirchhoff(c, x) == []
    for unknown, law in (("v:n4", "H1 (ccvs)"), ("v:n5", "E1 (vcvs)"), ("v:n1", "Vs (vsource)")):
        y = list(x)
        y[c.unknowns.index(unknown)] += 1
        assert any(f"constitutive law of {law}" in r for r in C.kirchhoff(c, y)), unknown


def test_solver_reduce_handles_rank_deficient_and_row_swaps():
    F = Fraction
    red = S.reduce([[F(0), F(1)], [F(1), F(0)]], [F(2), F(3)])
    assert red.pivots == [0, 1] and red.rhs == [F(3), F(2)]
    red = S.reduce([[F(0), F(0)], [F(0), F(1)]], [F(0), F(1)])
    assert red.pivots == [1]


# ------------------------------------------------------------------ float screens may only prune
def test_a_float_screen_false_positive_never_reaches_an_exact_confirmation():
    value, cert = V.solved(DIV)
    good = CS.CircuitCandidate(certificate=cert, value=str(value))
    fp = CS.CircuitCandidate(certificate={**cert, "x": [cert["x"][0], "6000000000000000000001/1000000000000000000000",
                                                        cert["x"][2]]}, value="6")
    screen, exact = CS.float_screen(DIV), CS.exact(DIV)
    assert screen(fp) is True and exact(fp) != []  # the attack: floats call the wrong certificate valid
    res = G.screen_then_certify([("good", good), ("fp", fp)], screen=screen, exact=exact, checker="circuit")
    assert [c.candidate_id for c in res.confirmed] == ["good"]
    assert [cid for cid, _ in res.refuted] == ["fp"]
    pruned = G.screen_then_certify([("good", good)], screen=lambda c: False, exact=exact, checker="circuit")
    assert pruned.confirmed == () and pruned.pruned == ("good",)  # a prune loses a result, never grants one
    assert CS.exact(DIV)(CS.CircuitCandidate(certificate=cert, value="6.0")) != []


# ------------------------------------------------------------------ through the Kernel
@pytest.fixture()
def cw(tmp_path):
    w = World(tmp_path / "circuit", seed=21, n_train=30, n_test=30)
    w.kernel.approve_adapter("circuit", approver="test-world")  # the test world's stand-in for the human approval
    yield w
    w.close()


def circuit_claim(w: World, problem: bytes = DIV, value: str | None = "6", statement: str | None = None,
                  evidence_class: str = "computational", **changes) -> Claim:
    aid = f"netlist-{hashlib.sha256(problem).hexdigest()[:10]}"
    ref = ArtifactRef(artifact_id=aid, hash=w.vault.register_artifact(aid, problem, "netlist").hash)
    try:
        phrase = C.scope_phrase(problem, Fraction(value or "0"))
    except ValueError:
        phrase = "unreadable netlist"
    doc = Claim(kind=ClaimKind.IMPROVEMENT, statement=statement if statement is not None else phrase, subject=ref,
                comparator=ref, metric=MetricRef(id=C.METRIC, higher_is_better=True),
                effect=Effect(min_effect=0.0, predicted_lo=0.0, predicted_hi=0.0, probability=0.9),
                scope=Scope(datasets=()), development=(), generalization_targets=(),
                analysis_plan=AnalysisPlan(seeds=()), resolve_by=time.time() + 86400).model_dump(mode="json")
    doc |= {"kind": "computed_value", "evidence_class": evidence_class, "value": value}
    for path, v in changes.items():
        node = doc
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = v
    return Claim.model_validate(doc)


def circuit_certify(w: World, claim: Claim, cert: bytes | None = None) -> Certificate:
    w.kernel.preregister(claim)
    data = cert if cert is not None else S.certify(w.store.blobs.get(claim.subject.hash))[1]
    aid = f"ccert-{claim.claim_hash()[:10]}-{hashlib.sha256(data).hexdigest()[:10]}"
    w.vault.register_artifact(aid, data, "circuit certificate")
    return w.kernel.certify(claim, [w.kernel.record_certificate(claim, aid)])


def test_a_true_circuit_claim_certifies_exact_at_t1(cw):
    claim = circuit_claim(cw)
    cert = circuit_certify(cw, claim)
    assert cert.label == Label.CERTIFIED_EXACT, {k: v.reasons for k, v in cert.checks.items() if v.status != "pass"}
    assert cert.tier == int(Tier.T1_EXECUTABLE)
    assert [k for k in CHECK_IDS if cert.checks[k].status == "pass"] == ["K0", "K1", "K2", "K3", "K8", "K9"]
    table = K.CHECK_APPLICABILITY[EvidenceClass.COMPUTATIONAL]
    for kid in ("K4", "K5", "K6", "K7"):  # K7 n/a for exact claims, from the human-owned table (as for lp)
        assert cert.checks[kid].status == "n/a" and cert.checks[kid].reasons == (table[kid],)
    (used,) = cert.adapters
    assert used["name"] == "circuit" and used["hash"] == A.adapter_hash(A.REGISTRY["circuit"][0])
    assert f"exact:{C.METRIC}" in cert.verifier_ids
    assert "says nothing about any physical circuit" in cert.claim["statement"]
    assert cw.kernel.verify(cert.cert_hash()) == []


@pytest.mark.parametrize(("problem", "value"), [
    (V.problem(V.PARALLEL_EQUAL, V.SINGULAR, ("0", "n1")), "1"),
    (V.problem(V.DIVIDER, V.SINGULAR), "0"),
    (V.problem(V.PARALLEL_CLASH, V.INCONSISTENT, ("0", "n1")), "1"),
    (V.problem(V.CONTROLLED, V.node("n6"), V.CONTROLLED_NODES), "-2"),
])
def test_singularity_inconsistency_and_controlled_claims_certify(cw, problem, value):
    cert = circuit_certify(cw, circuit_claim(cw, problem, value))
    assert cert.label == Label.CERTIFIED_EXACT, cert.failed()


def test_false_claims_are_rejected_by_the_right_check(cw):
    off = circuit_certify(cw, circuit_claim(cw, value="7"))
    assert off.failed() == ["K3"] and "differs from the claimed value 7" in reasons(off.checks["K3"])
    _, good = V.solved(DIV)
    forged = circuit_certify(cw, circuit_claim(cw, statement="forged x"),
                             json.dumps({**good, "x": ["10", "61/10", "-2"]}).encode())
    assert set(forged.failed()) == {"K3", "K8"} and "A x != b" in reasons(forged.checks["K3"])
    assert "KCL violated" in reasons(forged.checks["K8"]) and "Tellegen" in reasons(forged.checks["K8"])
    volts = V.problem([V.vs("V1", "n1", "0", "10"), V.r("R1", "n1", "n2", "2", "V"), V.r("R2", "n2", "0", "3")],
                      V.node("n2"))
    unit = circuit_certify(cw, circuit_claim(cw, volts))
    assert unit.failed() == ["K8"] and "element equation needs" in reasons(unit.checks["K8"])
    singular = V.problem(V.PARALLEL_EQUAL, V.SINGULAR, ("0", "n1"))
    wrong = circuit_certify(cw, circuit_claim(cw, singular, "0"), json.dumps({
        "format": C.CERT_FORMAT, "problem_sha256": hashlib.sha256(singular).hexdigest(),
        "unknowns": ["v:n1", "i:V1", "i:V2"], "inverse": [["1", "0", "0"], ["0", "1", "0"], ["0", "0", "1"]]}).encode())
    assert wrong.failed() == ["K3"] and "not a left inverse" in reasons(wrong.checks["K3"])


@pytest.mark.parametrize("relabel", ["physical", "simulated", "data", "formal"])
def test_a_circuit_claim_cannot_be_certified_under_another_evidence_class(cw, relabel):
    """Invariant 14: no adapter serves `circuit_value` outside the computational class; a physical relabelling of
    the very claim that certifies as exact is refused at K1."""
    cert = circuit_certify(cw, circuit_claim(cw, evidence_class=relabel))
    assert cert.label == Label.REJECTED and "K1" in cert.failed() and cert.adapters == ()
    assert f"no registered adapter serves evidence class {relabel!r} with metric 'circuit_value'" in reasons(
        cert.checks["K1"])


def test_registered_but_unapproved_adapter_cannot_certify(tmp_path):
    """Invariant 13: registration is not approval. Without a human approval of its current hash the adapter is
    unusable, and the reason names the human step."""
    w = World(tmp_path / "w")  # approves only the ML adapter (test stand-in)
    try:
        adapter, why = A.resolve(w.kernel.ledger, EvidenceClass.COMPUTATIONAL, C.METRIC)
        assert adapter is None and "adapter 'circuit'" in why[0] and "chh approve-adapter circuit" in why[0]
        cert = circuit_certify(w, circuit_claim(w))
        assert cert.label == Label.REJECTED and "K1" in cert.failed() and cert.adapters == ()
        assert "no human approval" in reasons(cert.checks["K1"])
        assert not [e for e in w.kernel.ledger.entries("adapter_approval") if e.body.get("name") == "circuit"]
    finally:
        w.close()


def test_an_approval_of_an_older_circuit_hash_does_not_carry_over(cw, monkeypatch):
    spec, checks = A.REGISTRY["circuit"]
    bumped = A.AdapterSpec(spec.name, "2", spec.classes, spec.modules, spec.metrics, spec.exact_module)
    monkeypatch.setitem(A.REGISTRY, "circuit", (bumped, checks))
    adapter, why = A.resolve(cw.kernel.ledger, EvidenceClass.COMPUTATIONAL, C.METRIC)
    assert adapter is None and "changed since approval" in why[0]
