"""GOAL §5 L3, wired into the claim path (s3-verifiers): a K3 tolerance comes from a measured noise floor (or stays
0.0), a planted 0.01 drift is rejected by the Kernel's K3 while honest re-runs pass, the research loop freezes the
calibrated value into the pre-registered claim, and a float-screen pass never reaches a certificate."""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pytest
import yaml

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.gateway import MockBackend
from custom_harness.kernel import ArtifactRef, Claim, ClaimKind, Effect, MetricRef, Scope, canonical_json
from custom_harness.kernel.certificate import Label
from custom_harness.kernel.claim import AnalysisPlan, EvidenceClass
from custom_harness.records import RunStatus
from custom_harness.research.noise_floor import _shuffled_rows
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from custom_harness.verify import l3_config as C
from custom_harness.verify import screen as G
from custom_harness.verify import tolerance as T
from kernel_world import World
from test_research import rings_task, scripted_llm  # noqa: F401  (fixture re-export)

ROOT = Path(__file__).resolve().parents[1]


def honest_floor(seed: int = 3, sd: float = 0.0005, ref: float = 0.8) -> T.NoiseFloor:
    rng, n = random.Random(seed), T.min_runs(C.TOL_CLAIM_MISS_RATE)
    return T.NoiseFloor(reference=ref, nominal=tuple(ref + rng.gauss(0, sd) for _ in range(n)),
                        perturbed=tuple(ref + rng.gauss(0, 2 * sd) for _ in range(n)), rebuild=(), seed=())


def test_claim_tolerance_is_exact_without_a_floor_and_never_takes_a_hand_set_value():
    assert T.claim_tolerance(EvidenceClass.DATA, None).value == 0.0
    r = T.claim_tolerance(EvidenceClass.DATA, honest_floor())
    assert r.source == "calibrated" and 0.0 < r.value < 0.01, r
    for exact in (EvidenceClass.FORMAL, EvidenceClass.COMPUTATIONAL):  # exact classes: always 0, floor ignored
        assert T.claim_tolerance(exact, honest_floor()).value == 0.0
    assert T.claim_tolerance(EvidenceClass.PHYSICAL, honest_floor()).value is None
    # a floor calibration refuses (too few runs; noise above the Kernel ceiling) keeps the strict 0.0, not a clip
    few = T.NoiseFloor(reference=0.8, nominal=(0.8001,) * 3, perturbed=(0.8,) * 3, rebuild=(), seed=())
    loud = T.NoiseFloor(reference=0.8, nominal=(0.85,) * 9, perturbed=(0.8,) * 9, rebuild=(), seed=())
    for f in (few, loud):
        r = T.claim_tolerance(EvidenceClass.DATA, f)
        assert r.value == 0.0 and any("calibration refused" in x for x in r.reasons), r
    # review H1: identical nominal re-runs keep the exact K3 (identical predictions), even when the shuffled-rows
    # source is noisy (a tolerance > 0 would switch K3 from comparing predictions to comparing scores)
    det = T.NoiseFloor(reference=0.8, nominal=(0.8,) * 9, perturbed=tuple(0.8 + 0.001 * i for i in range(9)),
                       rebuild=(), seed=())
    assert T.calibrate(det, miss_rate=0.1, purpose=C.TOL_CLAIM_PURPOSE).tolerance > 0  # the trap is real
    r = T.claim_tolerance(EvidenceClass.DATA, det)
    assert r.value == 0.0 and any("identical" in x for x in r.reasons), r
    # the claim purpose never needs rebuild runs, and is never looser than the full reproduce purpose
    f = honest_floor()
    full = T.NoiseFloor(f.reference, f.nominal, f.perturbed, tuple(f.reference + 0.001 for _ in f.nominal), ())
    assert T.calibrate(full, miss_rate=0.1, purpose=C.TOL_CLAIM_PURPOSE).tolerance <= \
        T.calibrate(full, miss_rate=0.1, purpose="reproduce").tolerance


def test_measure_refuses_a_failed_rerun():
    with pytest.raises(T.ToleranceError, match="no finite value"):
        T.measure(lambda s, i: None if (s, i) == ("perturbed", 4) else 0.8, 0.8, purpose=C.TOL_CLAIM_PURPOSE, n=9)
    with pytest.raises(T.ToleranceError, match="reference"):
        T.measure(lambda s, i: 0.8, None, purpose=C.TOL_CLAIM_PURPOSE, n=9)
    f = T.measure(lambda s, i: 0.8 + (0.001 if s == "perturbed" else 0.0), 0.8, purpose=C.TOL_CLAIM_PURPOSE, n=9)
    assert f.rebuild == () and len(f.nominal) == len(f.perturbed) == 9


def test_shuffled_rows_keep_header_rows_and_terminators():
    for data in (b"a,b\n1,2\n3,4\n5,6\n", b"a,b\r\n1,2\r\n3,4\r\n5,6", b"a,b\n1,2\n"):
        out = _shuffled_rows(data, 7)
        assert out.splitlines()[0] == data.splitlines()[0]
        assert sorted(out.splitlines()[1:]) == sorted(data.splitlines()[1:])
        assert out.endswith((b"\n",)) == data.endswith(b"\n") and len(out) == len(data)
    assert _shuffled_rows(b"a,b\n1,2\n3,4\n5,6\n7,8\n9,0\n", 1) != b"a,b\n1,2\n3,4\n5,6\n7,8\n9,0\n"


def test_planted_drift_is_rejected_by_k3_and_honest_reruns_pass(tmp_path):
    tol = T.claim_tolerance(EvidenceClass.DATA, honest_floor()).value
    assert tol is not None and 0.0 < tol < 0.01
    w = World(tmp_path / "w", seed=2)
    try:
        good, base = w.artifact("good"), w.artifact("baseline", "baseline")

        def k3(delta: float, tolerance: float, offset: float) -> str:
            c = w.claim(good, base, delta=delta, reproduce_tolerance=tolerance)
            w.kernel.preregister(c)
            cert = w.kernel.certify(c, w.evaluate_all(c, score_offset=offset))
            return cert.checks["K3"].status

        # each call is a DIFFERENT claim (delta): re-certifying one claim hash is subject to the Kernel's own rules
        # for earlier certificates of that claim, which this test is not about
        assert k3(0.2, tol, 0.0) == "pass"  # honest: the replay reproduces the recorded score
        assert k3(0.19, tol, tol / 2) == "pass"  # an honest re-run inside the measured noise passes
        # the attack: a 0.01 drift between the recorded score and what the program reproduces ...
        assert k3(0.21, C.TOL_CEILING, 0.01) == "pass"  # ... passes the Kernel's hand-set ceiling (0.02)
        assert k3(0.22, tol, 0.01) == "fail"  # ... and is rejected by the calibrated tolerance
    finally:
        w.close()


# ------------------------------------------------------------------ the research loop freezes the calibrated value
def test_research_claim_freezes_a_calibrated_tolerance(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")  # trusted test code only
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    doc = yaml.safe_load((ROOT / "examples/research_v1.yaml").read_text(encoding="utf-8"))
    doc["nodes"]["claim"]["params"]["calibrate_tolerance"] = True
    spec = HarnessSpec.model_validate(doc)
    from custom_harness.research import noise_floor as NF
    real, measured = NF.measure_dev_noise_floor, []

    def jittered(*a, **k):  # the REAL measurement runs; its nominal spread is then widened a little, so the value
        f = real(*a, **k)  # frozen into the claim is > 0 and the test can tell it from the default 0.0
        measured.append(f)
        return T.NoiseFloor(f.reference, tuple(v + 1e-4 * (i + 1) for i, v in enumerate(f.nominal)), f.perturbed,
                            (), ())
    monkeypatch.setattr(NF, "measure_dev_noise_floor", jittered)
    llm = MockBackend(scripted_llm)
    res = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    rec = s["tolerance"]
    n = T.min_runs(C.TOL_CLAIM_MISS_RATE)
    assert len(measured) == 1 and len(measured[0].nominal) == len(measured[0].perturbed) == n  # real dev re-runs
    assert rec["source"] == "calibrated" and rec["runs_per_source"] == n and rec["value"] > 0, rec
    assert len(rec["floor"]["nominal"]) == len(rec["floor"]["perturbed"]) == n
    claim = Claim.model_validate(s["claim"])
    assert claim.analysis_plan.reproduce_tolerance == rec["value"] > 0  # frozen into the pre-registration
    from custom_harness.kernel import Kernel
    regs = [e.body for e in Kernel(store).ledger.entries("prereg") if e.body.get("claim_hash") == s["claim_hash"]]
    assert regs and regs[0]["claim"]["analysis_plan"]["reproduce_tolerance"] == rec["value"]
    assert "K3" not in s["certificate"]["failed"], s["certificate"]  # an honest (deterministic) program reproduces
    # the replica claim (no calibrate param) keeps the strict 0.0
    if s.get("replica_claim"):
        assert Claim.model_validate(s["replica_claim"]).analysis_plan.reproduce_tolerance == 0.0
        assert "replica_tolerance" not in s
    json.dumps(rec)  # plain JSON in state


# ------------------------------------------------------------------ float screen -> exact gate -> Kernel
TINY = {"format": "chh-lp/1", "sense": "max", "c": ["1"], "A": [["10"]], "b": ["1"]}
GOOD = G.LPCandidate(certificate={"format": "chh-lp-certificate/1", "x": ["1/10"], "y": ["1/10"]}, value="1/10")
FP = G.LPCandidate(certificate={"format": "chh-lp-certificate/1", "x": ["1000000000000000001/10000000000000000000"],
                                "y": ["1/10"]}, value="1/10")


def _claim_for(w: World):
    def build(cid: str, cand: G.LPCandidate) -> Claim:
        ref = ArtifactRef(artifact_id=f"tiny-{cid}",
                          hash=w.vault.register_artifact(f"tiny-{cid}", canonical_json(TINY), "problem").hash)
        doc = Claim(kind=ClaimKind.IMPROVEMENT, statement=f"the optimum of tiny-{cid} is {cand.value}", subject=ref,
                    comparator=ref, metric=MetricRef(id="lp_optimum", higher_is_better=True),
                    effect=Effect(min_effect=0.0, predicted_lo=0.0, predicted_hi=0.0, probability=0.9),
                    scope=Scope(datasets=()), development=(), generalization_targets=(),
                    analysis_plan=AnalysisPlan(seeds=()), resolve_by=time.time() + 86400).model_dump(mode="json")
        doc |= {"kind": "computed_value", "evidence_class": "computational", "value": cand.value}
        return Claim.model_validate(doc)
    return build


def test_a_screen_pass_never_grants_only_exact_confirmations_are_certified(tmp_path):
    w = World(tmp_path / "lp", seed=3, n_train=30, n_test=30)
    try:
        w.kernel.approve_adapter("lp", approver="test-world")
        screen = G.lp_float_screen(TINY)
        assert screen(FP) is True  # the attack: the float screen passes a wrong certificate
        cands = {"good": GOOD, "fp": FP}
        res = G.screen_then_certify(list(cands.items()), screen=screen, exact=G.lp_exact(TINY), checker="lp")
        n_ledger = len(w.kernel.ledger.entries("prereg"))
        # a screen survivor, a bare candidate or an id is never submitted; nothing is registered on refusal
        for bad in (["fp"], [FP], [("fp", FP)], [res]):
            with pytest.raises(TypeError, match="never grants"):
                G.certify_confirmed_lp(w.kernel, bad, cands, _claim_for(w))
        # review L2: dataclasses.replace copies the gate token; such a copy is still refused
        import dataclasses
        forged = dataclasses.replace(G.submittable(res)[0], candidate_id="fp", candidate_hash=G._hash(FP))
        with pytest.raises(TypeError, match="not issued"):
            G.certify_confirmed_lp(w.kernel, [forged], cands, _claim_for(w))
        # a confirmation whose candidate was swapped afterwards is refused
        with pytest.raises(ValueError, match="not the one"):
            G.certify_confirmed_lp(w.kernel, G.submittable(res), {"good": FP}, _claim_for(w))
        assert len(w.kernel.ledger.entries("prereg")) == n_ledger
        certs = G.certify_confirmed_lp(w.kernel, G.submittable(res), cands, _claim_for(w))
        assert list(certs) == ["good"] and certs["good"].label == Label.CERTIFIED_EXACT
    finally:
        w.close()
