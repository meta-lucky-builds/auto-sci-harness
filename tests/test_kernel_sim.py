"""M3: the simulator adapter and the simulated evidence class (GOAL §4.7 "Simulated", §5 Domain adapters, invariants
13-14). A simulated claim holds only IN the pinned simulator that produced it; the Kernel re-runs the simulation at
the declared step and at h/2, h/4 (convergence evidence) and reproduces the reported numbers bit-for-bit.

Every planted fault asserts the failing check AND its reason, so nothing passes by failing for the wrong reason."""

from __future__ import annotations

import hashlib
import json
import time
from fractions import Fraction

import pytest

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
    canonical_json,
    sim,
)
from custom_harness.kernel import adapters as A
from custom_harness.kernel import checks as K
from custom_harness.kernel.bundle import build_bundle, reproduce_bundle
from custom_harness.kernel.execution import SandboxReplayer
from custom_harness.sandbox import LocalSandbox
from custom_harness.tiers import Tier
from kernel_world import World

# harmonic oscillator x'' = -x, x(0) = 1, x'(0) = 0: x(1) = cos(1) = 0.5403023058681398 (closed form)
OSC = {"format": "chh-sim/1", "simulator": "chh-ode-rk4", "version": "1", "model": "damped_oscillator",
       "params": {"omega": "1", "zeta": "0"}, "x0": ["1", "0"], "t_end": "1", "steps": 50, "component": 0,
       "tolerance": "1/10000"}
VALUE = "5403/10000"


def reasons(res) -> str:
    return " | ".join(res.reasons)


def config_bytes(config: dict | None = None, **changes) -> bytes:
    return canonical_json({**(config or OSC), **changes})


def result_bytes(config: bytes, values: list[str] | None = None, **changes) -> bytes:
    cfg = sim.parse_config(config)
    vals = values if values is not None else [repr(sim.quantity(cfg, cfg.steps * r)) for r in sim.REFINEMENTS]
    doc = {"format": "chh-sim-result/1", "config_sha256": hashlib.sha256(config).hexdigest(), "values": vals}
    return canonical_json({**doc, **changes})


@pytest.fixture()
def sw(tmp_path):
    w = World(tmp_path / "sim", seed=13, n_train=30, n_test=30)
    w.kernel.approve_adapter("sim", approver="test-world")  # the test world's stand-in for the human approval
    yield w
    w.close()


def sim_claim(w: World, *, config: bytes | None = None, value: str | None = VALUE, artifact_id: str | None = None,
              statement: str | None = None, conditions: dict | None = None, **changes) -> Claim:
    data = config if config is not None else config_bytes()
    aid = artifact_id or f"sim-{hashlib.sha256(data).hexdigest()[:10]}"
    ref = ArtifactRef(artifact_id=aid, hash=w.vault.register_artifact(aid, data, "simulation config").hash)
    try:
        phrase = sim.scope_phrase(data, Fraction(value or "0"))
    except ValueError:
        phrase = "unreadable config"
    cond = conditions if conditions is not None else {"simulator": "chh-ode-rk4", "version": "1", "config": ref.hash}
    doc = Claim(kind=ClaimKind.IMPROVEMENT, statement=statement if statement is not None else phrase, subject=ref,
                comparator=ref, metric=MetricRef(id="sim_value", higher_is_better=True),
                effect=Effect(min_effect=0.0, predicted_lo=0.0, predicted_hi=0.0, probability=0.9),
                scope=Scope(datasets=(), conditions=cond), development=(), generalization_targets=(),
                analysis_plan=AnalysisPlan(seeds=()), resolve_by=time.time() + 86400).model_dump(mode="json")
    doc |= {"kind": "computed_value", "evidence_class": "simulated", "value": value}
    for path, v in changes.items():
        node = doc
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = v
    return Claim.model_validate(doc)


def sim_certify(w: World, claim: Claim, result: bytes | None = None) -> Certificate:
    w.kernel.preregister(claim)
    data = result if result is not None else result_bytes(w.store.blobs.get(claim.subject.hash))
    aid = f"simres-{claim.claim_hash()[:10]}-{hashlib.sha256(data).hexdigest()[:10]}"
    w.vault.register_artifact(aid, data, "simulation result")
    return w.kernel.certify(claim, [w.kernel.record_certificate(claim, aid)])


# ------------------------------------------------------------------ the honest simulated claim
def test_simulated_claim_certifies_only_within_its_simulator(sw):
    claim = sim_claim(sw)
    cert = sim_certify(sw, claim)
    assert cert.label == Label.CERTIFIED_SIMULATED, {k: v.reasons for k, v in cert.checks.items() if v.status != "pass"}
    assert cert.tier == int(Tier.T2_STATISTICAL)  # conservative: T2 within the simulator (decision recorded)
    assert [k for k in CHECK_IDS if cert.checks[k].status == "pass"] == ["K0", "K1", "K2", "K3", "K8", "K9"]
    table = K.CHECK_APPLICABILITY[EvidenceClass.SIMULATED]
    for kid in ("K4", "K5", "K6", "K7"):
        assert cert.checks[kid].status == "n/a" and cert.checks[kid].reasons == (table[kid],)
    scope = f"in simulator chh-ode-rk4 (version 1, config {claim.subject.hash[:12]})"
    assert scope in claim.statement and any(scope in o for o in cert.checks["K3"].observations)
    assert "within the simulator" in cert.vg_ladder and "not that the simulator matches reality" in cert.vg_ladder
    reality = [a for a in cert.assumptions if "simulation matches reality" in a.text]
    assert len(reality) == 1 and reality[0].tested_by is None  # the gap to reality is listed as UNTESTED
    (used,) = cert.adapters
    assert used["name"] == "sim" and used["approver"] == "test-world"
    assert "sim:sim_value" in cert.verifier_ids and not any(v.startswith("exact:") for v in cert.verifier_ids)
    assert sw.kernel.verify(cert.cert_hash()) == []
    k3 = " ".join(cert.checks["K3"].observations)
    assert "Richardson" in k3 and "h/4" in k3


@pytest.mark.parametrize("relabel", ["physical", "data", "formal", "computational"])
def test_a_simulated_claim_cannot_be_certified_as_physical(sw, relabel):
    """GOAL M3 / invariant 14: the very claim and evidence that certify as SIMULATED never certify under any other
    evidence class (no adapter serves the simulator's metric outside the simulated class), and are never relabelled."""
    honest = sim_claim(sw)
    assert sim_certify(sw, honest).label == Label.CERTIFIED_SIMULATED
    sw.kernel.approve_adapter("lp", approver="test-world")  # every other adapter approved: still no path
    doc = honest.model_dump(mode="json") | {"evidence_class": relabel}
    relabelled = Claim.model_validate(doc)
    cert = sim_certify(sw, relabelled)
    assert cert.label == Label.REJECTED and "K1" in cert.failed() and cert.adapters == ()
    assert f"no registered adapter serves evidence class {relabel!r} with metric 'sim_value'" in reasons(
        cert.checks["K1"])


def test_simulated_scope_cannot_claim_physical_reality_or_another_simulator(sw):
    good = sim_claim(sw)
    h = good.subject.hash
    cases = {  # scope conditions -> K1 reason
        "physical scope": ({"simulator": "chh-ode-rk4", "version": "1", "config": h, "holds_in": "physical world"},
                           "scope conditions must be exactly"),
        "no scope": ({}, "scope conditions must be exactly"),
        "wrong simulator": ({"simulator": "ansys", "version": "1", "config": h}, "scope names simulator 'ansys'"),
        "wrong version": ({"simulator": "chh-ode-rk4", "version": "2", "config": h}, "scope names version '2'"),
        "wrong config": ({"simulator": "chh-ode-rk4", "version": "1", "config": "0" * 64},
                         "scope names config 000000000000"),
    }
    for name, (cond, why) in cases.items():
        cert = sim_certify(sw, sim_claim(sw, conditions=cond))
        assert cert.label == Label.REJECTED and cert.failed() == ["K1"], (name, cert.failed())
        assert why in reasons(cert.checks["K1"]), (name, cert.checks["K1"])
    phrase = good.statement
    for prose in (f"x[0](t=1) = {VALUE} in the real oscillator",  # scope dropped
                  phrase + "; the real oscillator behaves the same",  # red team: prose scope creep after the phrase
                  "Measured in the lab: " + phrase):
        cert = sim_certify(sw, sim_claim(sw, statement=prose))
        assert cert.failed() == ["K1"] and "statement must be exactly" in reasons(cert.checks["K1"]), prose
        assert "in simulator chh-ode-rk4 (version 1, config" in reasons(cert.checks["K1"])


# ------------------------------------------------------------------ planted faults through the Kernel
def test_simulated_planted_faults_are_rejected_by_the_right_check(sw):
    wrong = sim_certify(sw, sim_claim(sw, value="27/50"))
    assert wrong.failed() == ["K3"] and "differs from the simulated value" in reasons(wrong.checks["K3"])
    coarse = config_bytes(t_end="10", steps=4, tolerance="1/100")  # h = 2.5: stable but far from converged
    q = Fraction(sim.quantity(sim.parse_config(coarse), 4)).limit_denominator(10**6)
    unconv = sim_certify(sw, sim_claim(sw, config=coarse, value=str(q)))
    assert unconv.failed() == ["K3"] and "unconverged" in reasons(unconv.checks["K3"])
    claim = sim_claim(sw)
    good = result_bytes(config_bytes())
    vals = json.loads(good)["values"]
    forged = sim_certify(sw, claim, result_bytes(config_bytes(), [vals[0], vals[0], vals[0]]))
    assert forged.failed() == ["K3"] and "not reproduced" in reasons(forged.checks["K3"])
    other = sim_certify(sw, sim_claim(sw, effect__probability=0.8),
                        result_bytes(config_bytes(steps=60)))  # a genuine result, but of another config
    assert other.failed() == ["K3"] and "result is for config" in reasons(other.checks["K3"])
    nan = sim_certify(sw, sim_claim(sw, effect__probability=0.7),
                      result_bytes(config_bytes(), ["nan", "nan", "nan"]))
    assert nan.failed() == ["K3"] and "not reproduced" in reasons(nan.checks["K3"])
    blow = config_bytes(model="linear", params={"A": [["100"]]}, x0=["1"], t_end="1000", steps=10, tolerance="1")
    div = sim_certify(sw, sim_claim(sw, config=blow, value="1"), result_bytes(blow, ["inf", "nan", "nan"]))
    assert "K3" in div.failed() and "non-finite" in reasons(div.checks["K3"])
    out = config_bytes(params={"omega": "1000", "zeta": "0"})
    oor = sim_certify(sw, sim_claim(sw, config=out), result_bytes(config_bytes()))
    assert oor.failed() == ["K1", "K3", "K8"] and "outside the registered range" in reasons(oor.checks["K3"])
    assert "outside the registered range" in reasons(oor.checks["K8"])
    loose = config_bytes(tolerance="1")
    lc = sim_certify(sw, sim_claim(sw, config=loose))
    assert lc.failed() == ["K8"] and "tolerance 1 is looser than" in reasons(lc.checks["K8"])
    for bad in ("0.5403", "5403/10000.0", None):
        cert = sim_certify(sw, sim_claim(sw, value=bad, statement=claim.statement))
        assert cert.label == Label.REJECTED and "K1" in cert.failed(), bad
        assert "exact rational" in reasons(cert.checks["K1"]) and "see K1" in reasons(cert.checks["K3"])


def test_simulated_claim_well_formedness(sw):
    other = ArtifactRef(artifact_id="x", hash="0" * 64)
    cases = {
        "comparator must be the subject": {"comparator": other.model_dump()},
        "uses no datasets": {"generalization_targets": ["test"]},
        "analysis plan must be empty": {"analysis_plan__seeds": [0, 1, 2]},
        "effect fields other than the declared probability must be 0": {"effect__min_effect": 0.1},
        "declared probability": {"effect__probability": 1.0},
        "risk prior must be unset": {"risk__prior_probability": 0.5},
        "kind 'computed_value'": {"kind": "improvement"},
        "no registered adapter serves evidence class 'simulated' with metric 'accuracy'": {"metric__id": "accuracy"},
    }
    for why, change in cases.items():
        bad = sim_claim(sw, **change)
        sw.kernel.preregister(bad)
        assert why in reasons(sw.kernel.certify(bad, []).checks["K1"]), (why, bad)
    garbage = sim_claim(sw, config=b"{", statement="x")
    sw.kernel.preregister(garbage)
    assert "config unreadable" in reasons(sw.kernel.certify(garbage, []).checks["K1"])
    lost = sim_claim(sw, config=config_bytes(comment="lost"))
    sw.store.blobs._path(lost.subject.hash).unlink()
    sw.kernel.preregister(lost)
    cert = sw.kernel.certify(lost, [])
    assert "config unreadable" in reasons(cert.checks["K1"]) and "K0" in cert.failed()


def test_simulated_claim_without_an_approved_sim_adapter_fails_k1(tmp_path, monkeypatch):
    """Invariant 13: the adapter is unusable until a human approves its CURRENT content."""
    w = World(tmp_path / "noapp", seed=14, n_train=30, n_test=30)  # approves only the ML adapter
    try:
        cert = sim_certify(w, sim_claim(w))
        assert cert.label == Label.REJECTED and "K1" in cert.failed() and cert.adapters == ()
        assert "adapter 'sim'" in reasons(cert.checks["K1"]) and "no human approval" in reasons(cert.checks["K1"])
        assert "no approved simulator adapter" in reasons(cert.checks["K3"])
        no_mod = A.AdapterSpec("sim", "1", frozenset({EvidenceClass.SIMULATED}), ("custom_harness.kernel.sim",),
                               frozenset({"sim_value"}))
        monkeypatch.setitem(A.REGISTRY, "sim", (no_mod, sim.SIM_CHECKS))
        w.kernel.approve_adapter("sim", approver="test-world")
        cert = sim_certify(w, sim_claim(w, effect__probability=0.8))
        assert "declares no simulator module" in reasons(cert.checks["K1"])
        monkeypatch.setitem(A.REGISTRY, "sim", (A.SIM, (("planted failing validation", lambda: False),)))
        with pytest.raises(A.AdapterError, match="planted failing validation"):
            w.kernel.approve_adapter("sim", approver="test-world")
    finally:
        w.close()


def test_simulated_provenance_and_order(sw):
    claim = sim_claim(sw)
    sw.kernel.preregister(claim)
    assert sw.kernel.certify(claim, []).failed() == ["K0", "K3", "K8"]  # no result at all
    early = sim_claim(sw, effect__probability=0.6)
    data = result_bytes(config_bytes())
    aid = "simres-early"
    sw.vault.register_artifact(aid, data, "simulation result")  # the result exists BEFORE the claim
    sw.kernel.preregister(early)
    cert = sw.kernel.certify(early, [sw.kernel.record_certificate(early, aid)])
    assert cert.failed() == ["K2"] and "registered before the pre-registration" in reasons(cert.checks["K2"])
    canary = sw.vault.canary("test")
    leaky = sim_certify(sw, sim_claim(sw, config=config_bytes(comment=canary)))
    assert leaky.failed() == ["K9"] and "canary token found in problem instance" in reasons(leaky.checks["K9"])


def test_simulated_bundle_reproduces_by_re_running_the_simulation(sw, tmp_path, monkeypatch):
    cert = sim_certify(sw, sim_claim(sw))
    path = tmp_path / "sim.zip"
    build_bundle(sw.kernel, cert.cert_hash(), path)
    fresh = lambda: SandboxReplayer(lambda key: LocalSandbox(tmp_path / "rp" / key, i_understand_no_isolation=True))  # noqa: E731
    rep = reproduce_bundle(path, fresh)
    assert rep.reproduced and rep.reproduced_label == "CERTIFIED-SIMULATED", rep.problems
    monkeypatch.setattr(sim, "quantity", lambda cfg, steps: 0.25)  # reproduction must re-run the simulator
    rep = reproduce_bundle(path, fresh)
    assert not rep.reproduced and any(p.startswith("K3: pass originally, fail now") for p in rep.problems)


# ------------------------------------------------------------------ the adapter itself (fault-injection validation)
def test_sim_adapter_validation_passes_and_covers_every_planted_fault():
    assert A.validate(sim.SIM_CHECKS) == [] and len(sim.SIM_CHECKS) >= 12
    names = " ".join(n for n, _ in sim.SIM_CHECKS)
    for fault in ("wrong value", "unconverged", "version", "config", "NaN", "out-of-range", "not reproduced",
                  "closed form"):
        assert fault in names, fault
    assert A.SIM.classes == frozenset({EvidenceClass.SIMULATED})  # never data / physical / formal / computational
    assert A.REGISTRY["sim"] == (A.SIM, sim.SIM_CHECKS)


def test_sim_rk4_matches_closed_forms_and_is_fourth_order():
    import math
    cfg = sim.parse_config(config_bytes())
    q = [sim.quantity(cfg, 50 * r) for r in (1, 2, 4)]
    assert abs(q[2] - math.cos(1)) < 1e-10
    assert 14 < abs(q[0] - q[1]) / abs(q[1] - q[2]) < 18  # observed order ~4
    decay = sim.parse_config(config_bytes(model="linear", params={"A": [["-1"]]}, x0=["1"]))
    assert abs(sim.quantity(decay, 200) - math.exp(-1)) < 1e-11
    damped = sim.parse_config(config_bytes(params={"omega": "2", "zeta": "1/4"}))
    w, z = 2.0, 0.25
    wd = w * math.sqrt(1 - z * z)
    exact = math.exp(-z * w) * (math.cos(wd) + z * w / wd * math.sin(wd))
    assert abs(sim.quantity(damped, 400) - exact) < 1e-10
    s = config_bytes(model="sir", params={"beta": "3", "gamma": "1"}, x0=["99/100", "1/100", "0"], t_end="5",
                     steps=200, component=1, tolerance="1/1000")
    assert sim.bounds(s, result_bytes(s), Fraction(1, 10)) == []  # conservation and positivity hold
    assert sim.quantity(sim.parse_config(config_bytes()), 50) == q[0]  # deterministic, bit for bit


def test_sim_config_and_result_parsing_fail_closed():
    good = config_bytes()
    res = result_bytes(good)
    v = Fraction(VALUE)
    assert sim.check(good, res, v) == [] and sim.bounds(good, res, v) == []
    no_tol = {k: x for k, x in OSC.items() if k != "tolerance"}
    bad_configs = {
        "not JSON": b"{",
        "not a chh-sim/1": canonical_json([1]),
        "larger than": b" " * (sim.MAX_BYTES + 1),
        "unexpected keys": config_bytes(extra="1"),
        "missing keys": canonical_json(no_tol),
        "is not the pinned simulator chh-ode-rk4": config_bytes(simulator="other"),
        "is not the pinned version 1": config_bytes(version="2"),
        "unknown model": config_bytes(model="navier-stokes"),
        "params must be an object with exactly": config_bytes(params={"omega": "1"}),
        "exact rational string": config_bytes(params={"omega": 1.0, "zeta": "0"}),
        "steps must be an integer": config_bytes(steps=True),
        "steps 0 outside": config_bytes(steps=0),
        f"steps {sim.MAX_STEPS + 1} outside": config_bytes(steps=sim.MAX_STEPS + 1),
        "component 2 outside": config_bytes(component=2),
        "t_end = 0 outside": config_bytes(t_end="0"),
        "tolerance 0 must be > 0": config_bytes(tolerance="0"),
        "x0 must be a list of 2": config_bytes(x0=["1"]),
        "omega = 0 outside the registered range": config_bytes(params={"omega": "0", "zeta": "0"}),
        "zeta = -1 outside the registered range": config_bytes(params={"omega": "1", "zeta": "-1"}),
        "x0[0] = 101 outside the registered range": config_bytes(x0=["101", "0"]),
        "'A' must be a square matrix": config_bytes(model="linear", params={"A": [["1", "2"]]}, x0=["1"]),
        "'A' must be a square matrix of size 1..6": config_bytes(model="linear", params={"A": []}, x0=[]),
        "A[0][0] = 101 outside": config_bytes(model="linear", params={"A": [["101"]]}, x0=["1"]),
        "sir state must be non-negative and sum to 1": config_bytes(
            model="sir", params={"beta": "1", "gamma": "1"}, x0=["1/2", "1/4", "0"]),
        "sir state must be non-negative and sum to 1 ": config_bytes(
            model="sir", params={"beta": "1", "gamma": "1"}, x0=["2", "-1", "0"]),
    }
    for why, cfg in bad_configs.items():
        assert any(why.strip() in r for r in sim.check(cfg, res, v)), (why, sim.check(cfg, res, v))
        assert any(why.strip() in r for r in sim.bounds(cfg, res, v)), why
        with pytest.raises(sim.SimError):
            sim.scope_phrase(cfg, v)
    # red team: a duplicated key shows a reader one tolerance and the parser another (json keeps the last one)
    dup = config_bytes().replace(b'"tolerance":"1/10000"', b'"tolerance":"1/10000","tolerance":"1"')
    assert dup.count(b'"tolerance"') == 2
    assert any("duplicate key 'tolerance'" in r for r in sim.check(dup, res, v)), sim.check(dup, res, v)
    doc = json.loads(res)
    bad_results = {
        "not a chh-sim-result/1": canonical_json({**doc, "format": "x"}),
        "values must be 3 strings": canonical_json({**doc, "values": [1.0, 2.0, 3.0]}),
        "values must be 3 strings ": canonical_json({**doc, "values": ["1"]}),
        "result is for config": canonical_json({**doc, "config_sha256": "0" * 64}),
    }
    for why, r in bad_results.items():
        assert any(why.strip() in x for x in sim.check(good, r, v)), (why, sim.check(good, r, v))
    assert any("exceeds 100 digits" in r for r in sim.bounds(good, res, Fraction(10**120)))
    laws = {  # K8 background laws of the model family, violated by unstable steps
        "energy grew": config_bytes(t_end="3", steps=1),  # h * omega = 3 > 2.83: RK4 amplifies
        "sir conservation/positivity violated": config_bytes(
            model="sir", params={"beta": "100", "gamma": "100"}, x0=["1/2", "1/2", "0"], t_end="10", steps=1,
            tolerance="1/100"),
        "non-finite state": config_bytes(model="linear", params={"A": [["100"]]}, x0=["1"], t_end="1000", steps=40,
                                         tolerance="1"),
    }
    for why, cfg in laws.items():
        assert any(why in r for r in sim.bounds(cfg, res, Fraction(1))), (why, sim.bounds(cfg, res, Fraction(1)))


def test_sim_convergence_rules_are_exact_and_conservative(monkeypatch):
    """The decision logic on planted refinement sequences (q(h), q(h/2), q(h/4))."""
    good = config_bytes()

    def planted(seq: tuple[float, float, float]) -> list[str]:
        table = dict(zip((50, 100, 200), seq, strict=True))
        monkeypatch.setattr(sim, "quantity", lambda cfg, steps: table[steps])
        return sim.check(good, result_bytes(good), Fraction(1, 2))

    assert planted((0.5, 0.5, 0.5)) == []
    assert planted((0.5 + 1.6e-5, 0.5 + 1e-6, 0.5 + 6.25e-8)) == []  # textbook 4th order, error 1.6e-5 < 1e-4
    order = planted((0.5, 0.5, 0.5 + 5e-5))  # agrees at h and h/2, then jumps: not in the asymptotic range
    assert any("asymptotic range" in r for r in order), order
    slow = planted((0.5 + 8e-5, 0.5 + 4e-5, 0.5 + 2e-5))  # first order, not RK4's fourth
    assert any("asymptotic range" in r for r in slow), slow
    big = planted((0.5 + 2e-4, 0.5 + 1.25e-5, 0.5 + 7.8125e-7))  # 4th order but the error estimate exceeds tol
    assert any("unconverged: Richardson error estimate" in r for r in big), big
    edge = planted((0.5 + 9e-5, 0.5 + 9e-5 / 16, 0.5 + 9e-5 / 256))  # |v - q| + E > tol, though each is < tol
    assert any("differs from the simulated value" in r for r in edge), edge
    inf = planted((float("inf"), 0.5, 0.5))
    assert any("non-finite" in r for r in inf), inf
    with pytest.raises(sim.SimError, match="not JSON"):
        sim.scope_phrase(b"{", Fraction(1))
