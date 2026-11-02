"""GOAL §5 L3 / §4.7 (Station): float screens may only PRUNE; a result counts only after the exact checker (the LP
adapter's `kernel.lp.check`) confirms it. A float-screen false positive never reaches a certificate."""

from __future__ import annotations

import random
import time
from fractions import Fraction

import pytest

from custom_harness.kernel import ArtifactRef, Claim, ClaimKind, Effect, MetricRef, Scope, canonical_json, lp
from custom_harness.kernel.certificate import Label
from custom_harness.kernel.claim import AnalysisPlan
from custom_harness.kernel.lp import scope_phrase as lp_phrase
from custom_harness.verify import screen as G
from kernel_world import World

# maximise x s.t. 10 x <= 1, x >= 0: optimum 1/10 at x = 1/10, dual y = 1/10
TINY = {"format": "chh-lp/1", "sense": "max", "c": ["1"], "A": [["10"]], "b": ["1"]}
GOOD = G.LPCandidate(certificate={"format": "chh-lp-certificate/1", "x": ["1/10"], "y": ["1/10"]}, value="1/10")
# x exceeds 1/10 by 10^-19: in floats 10 * x == 1.0 and x == 0.1, so the float screen passes it; exactly it is
# infeasible and its objective is not the claimed value
FP = G.LPCandidate(certificate={"format": "chh-lp-certificate/1", "x": ["1000000000000000001/10000000000000000000"],
                                "y": ["1/10"]}, value="1/10")


def test_a_float_screen_false_positive_never_reaches_an_exact_confirmation():
    screen, exact = G.lp_float_screen(TINY), G.lp_exact(TINY)
    assert screen(FP) is True  # the attack: the float screen calls the wrong certificate valid
    assert exact(FP) != []
    res = G.screen_then_certify([("good", GOOD), ("fp", FP)], screen=screen, exact=exact, checker="lp")
    assert [c.candidate_id for c in res.confirmed] == ["good"]
    assert [cid for cid, _ in res.refuted] == ["fp"] and any("infeasible" in r for r in res.refuted[0][1])
    assert res.pruned == () and res.screen_errors == ()
    assert res.confirmed[0].checker == "lp" and res.confirmed[0].value == Fraction(1, 10)


def test_the_screen_can_only_prune_never_promote():
    exact = G.lp_exact(TINY)
    yes, no = (lambda c: True), (lambda c: False)
    assert G.screen_then_certify([("fp", FP)], screen=yes, exact=exact, checker="lp").confirmed == ()
    res = G.screen_then_certify([("good", GOOD)], screen=no, exact=exact, checker="lp")
    assert res.confirmed == () and res.pruned == ("good",)  # a pruned true result is lost, never certified wrongly

    def boom(c):
        raise ZeroDivisionError

    for bad_screen in (boom, lambda c: 0.97, lambda c: None):  # crash / non-bool score: not a prune decision
        res = G.screen_then_certify([("good", GOOD)], screen=bad_screen, exact=exact, checker="lp")
        assert [c.candidate_id for c in res.confirmed] == ["good"] and res.screen_errors == ("good",)
    res = G.screen_then_certify([("good", GOOD)], screen=yes, exact=lambda c: 1 / 0, checker="lp")
    assert res.confirmed == () and res.refuted[0][0] == "good"  # a crashing exact checker refutes
    res = G.screen_then_certify([("good", GOOD)], screen=yes, exact=lambda c: "", checker="lp")
    assert res.confirmed == ()  # anything but an empty LIST of reasons is not a confirmation
    with pytest.raises(ValueError, match="duplicate"):
        G.screen_then_certify([("a", GOOD), ("a", GOOD)], screen=yes, exact=exact, checker="lp")


def test_a_confirmation_cannot_be_forged_outside_the_gate():
    with pytest.raises(TypeError, match="gate"):
        G.Confirmed(candidate_id="fp", checker="lp", value=Fraction(1, 10), candidate_hash="0" * 64)


def test_randomised_near_optimal_candidates_confirmed_iff_exact_checker_accepts():
    problem = {"format": "chh-lp/1", "sense": "max", "c": ["1", "1"], "A": [["2", "1"], ["1", "3"]],
               "b": ["4", "5"]}  # optimum 13/5 at x = (7/5, 6/5), y = (2/5, 1/5)
    rng, cands = random.Random(5), []
    for i in range(300):
        eps = [Fraction(rng.choice([0, 0, 1, -1]), 10 ** rng.choice([3, 12, 17, 19])) for _ in range(4)]
        x = [Fraction(7, 5) + eps[0], Fraction(6, 5) + eps[1]]
        y = [Fraction(2, 5) + eps[2], Fraction(1, 5) + eps[3]]
        cert = {"format": "chh-lp-certificate/1", "x": [str(v) for v in x], "y": [str(v) for v in y]}
        cands.append((f"c{i}", G.LPCandidate(certificate=cert, value="13/5")))
    screen, exact = G.lp_float_screen(problem), G.lp_exact(problem)
    res = G.screen_then_certify(cands, screen=screen, exact=exact, checker="lp")
    truth = {cid for cid, c in cands if lp.check(canonical_json(problem), canonical_json(c.certificate),
                                                 Fraction(13, 5)) == []}
    fooled = {cid for cid, c in cands if screen(c) and cid not in truth}
    assert fooled, "the random set must contain float-screen false positives, or this test attacks nothing"
    confirmed = {c.candidate_id for c in res.confirmed}
    assert confirmed <= truth and confirmed.isdisjoint(fooled)
    assert confirmed == {cid for cid, c in cands if screen(c)} & truth
    assert {cid for cid, _ in res.refuted} == fooled


def _lp_claim(w: World, artifact_id: str) -> Claim:
    data = canonical_json(TINY)
    ref = ArtifactRef(artifact_id=artifact_id, hash=w.vault.register_artifact(artifact_id, data, "problem").hash)
    doc = Claim(kind=ClaimKind.IMPROVEMENT, statement=lp_phrase(data, Fraction(1, 10)), subject=ref,
                comparator=ref, metric=MetricRef(id="lp_optimum", higher_is_better=True),
                effect=Effect(min_effect=0.0, predicted_lo=0.0, predicted_hi=0.0, probability=0.9),
                scope=Scope(datasets=()), development=(), generalization_targets=(),
                analysis_plan=AnalysisPlan(seeds=()), resolve_by=time.time() + 86400).model_dump(mode="json")
    doc |= {"kind": "computed_value", "evidence_class": "computational", "value": "1/10"}
    return Claim.model_validate(doc)


def test_end_to_end_only_gate_confirmed_candidates_are_submitted_and_the_kernel_agrees(tmp_path):
    w = World(tmp_path / "lp", seed=3, n_train=30, n_test=30)
    try:
        w.kernel.approve_adapter("lp", approver="test-world")
        res = G.screen_then_certify([("good", GOOD), ("fp", FP)], screen=G.lp_float_screen(TINY),
                                    exact=G.lp_exact(TINY), checker="lp")
        certs = {}
        for i, (cid, cand) in enumerate([("good", GOOD), ("fp", FP)]):
            claim = _lp_claim(w, f"tiny-{i}")
            w.kernel.preregister(claim)
            aid = w.vault.register_artifact(f"cert-{cid}", canonical_json(cand.certificate), "certificate").artifact_id
            eid = w.kernel.record_certificate(claim, aid)
            certs[cid] = w.kernel.certify(claim, [eid])
        submitted = G.submittable(res)
        assert [c.candidate_id for c in submitted] == ["good"]
        assert certs["good"].label == Label.CERTIFIED_EXACT
        # defence in depth: even if the false positive were submitted, the Kernel's exact K3 rejects it
        assert certs["fp"].label == Label.REJECTED and "K3" in certs["fp"].failed()
    finally:
        w.close()
