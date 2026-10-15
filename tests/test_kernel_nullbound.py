"""Bounded-null certificates in exclusion-limit form (GOAL §16.10, answers §13.5; docs/bounded_null.md).

A negative-result claim that declares its power (AnalysisPlan.power, frozen in the pre-registration) is certified
only when a positive plasmode control proves that power on the target's real stored predictions; the certificate
states the exclusion with the limit clipped at the proven sensitivity. Without the declaration K7 still fails closed.
Equivalence claims (TOST, s4-kernel): tests/test_kernel_s4.py. A declared power on an equivalence claim is no longer
misplaced (the parametrised K1 case moved from (EQUIVALENCE, 0.8) to the out-of-range (EQUIVALENCE, 0.99)).
"""

from __future__ import annotations

import random

import pytest

from custom_harness.kernel import AnalysisPlan, ClaimKind, DatasetRole, Effect, KernelConfig, Label, ml
from custom_harness.kernel import checks as K
from custom_harness.kernel import kernel as kernel_module
from custom_harness.kernel.reasons import Code
from kernel_world import World


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    w = World(tmp_path_factory.mktemp("nullbound"), seed=11)
    w.register_target("tiny", DatasetRole.TEST, n=15)
    yield w
    w.close()


def negative(w: World, delta: float, *, targets=("test",), kind=ClaimKind.NEGATIVE_RESULT, **plan):
    """'noisy_majority does not beat the baseline by delta' (true: it is the baseline with 5% random labels)."""
    c = w.claim(w.artifact("noisy_majority"), w.artifact("baseline", "baseline"), kind=kind, delta=delta,
                targets=targets, probability=0.6, n_boot=1000, **plan)
    return c.model_copy(update={"effect": Effect(min_effect=delta, predicted_lo=-0.1, predicted_hi=0.1,
                                                 probability=0.6)})


def certify(w: World, claim):
    w.kernel.preregister(claim)
    return w.kernel.certify(claim, w.evaluate_all(claim))


def test_honest_well_powered_negative_is_certified_with_the_clipped_limit(world):
    cert = certify(world, negative(world, 0.1, power=0.8))
    assert cert.label == Label.CERTIFIED_INTERNAL, cert.failed()
    ex = cert.resolution["exclusion"]
    n = 11  # ceil(ln 0.1 / ln 0.8): zero-failure demonstration of power 0.8 at confidence 0.9
    assert ex["excluded_effect"] == 0.1 and ex["sided"] == "one-sided (upper limit)" and ex["plantings"] == n
    assert ex["declared_power"] == 0.8 and ex["proven_power"] == pytest.approx(0.1 ** (1 / n))
    assert ex["proven_power"] >= 0.8 and ex["power_confidence"] == pytest.approx(0.9)
    assert ex["level"] == cert.resolution["targets"]["test"]["alpha_eff"]
    # a lucky limit (the observed upper limit lies far below delta, here even below 0) is clipped to the sensitivity
    assert ex["observed_limit"] < 0.05 and ex["reported_limit"] == 0.1
    assert any(f"{n}/{n} planted effects" in o for o in cert.checks["K7"].observations)
    assert world.kernel.verify(cert.cert_hash()) == []  # verify() re-derives the plasmode and the statement
    forged = cert.model_copy(update={"resolution": {**cert.resolution, "exclusion": {**ex, "reported_limit": 0.01}}})
    better, _ = kernel_module._differences(forged, cert)
    assert better == ["bounded-null exclusion statement differs from its re-derivation"]


def test_vacuous_delta_is_refused_by_the_plasmode(world):
    cert = certify(world, negative(world, 0.9, power=0.8))  # "does not improve by 0.9 accuracy"
    assert cert.label == Label.REJECTED and cert.failed() == ["K7"], cert.failed()
    assert cert.checks["K7"].codes == (Code.K7_NOT_PLANTABLE,) and "exclusion" not in cert.resolution


def test_insensitive_target_is_refused_by_the_plasmode(world):
    cert = certify(world, negative(world, 0.15, targets=("tiny",), power=0.8))  # 15 examples cannot see 0.15
    assert "K7" in cert.failed() and cert.checks["K7"].codes == (Code.K7_INSENSITIVE,)
    assert "not detected in planting" in cert.checks["K7"].reasons[0] and "exclusion" not in cert.resolution


def test_without_the_declaration_k7_still_fails_closed(world):
    for kind in (ClaimKind.NEGATIVE_RESULT, ClaimKind.EQUIVALENCE):
        cert = certify(world, negative(world, 0.1, kind=kind))
        assert cert.failed() == ["K7"] and cert.checks["K7"].codes == (Code.K7_NO_RISK_MODEL,)


@pytest.mark.parametrize(("kind", "power"), [(ClaimKind.NEGATIVE_RESULT, 0.4), (ClaimKind.NEGATIVE_RESULT, 0.99),
                                             (ClaimKind.IMPROVEMENT, 0.8), (ClaimKind.EQUIVALENCE, 0.99)])
def test_k1_refuses_an_out_of_range_or_misplaced_declaration(world, kind, power):
    c = negative(world, 0.1, kind=kind, power=power)
    cx = K.Context(claim=c, claim_hash=c.claim_hash(), store=world.store, vault=world.vault,
                   ledger=world.kernel.ledger, config=KernelConfig(), replayer=None, evidence_ids=())
    assert Code.K1_POWER in K.k1_well_formed(cx).codes
    cx.claim = negative(world, 0.1, power=0.8)
    assert Code.K1_POWER not in K.k1_well_formed(cx).codes


def test_an_unset_declaration_keeps_old_claim_hashes():
    assert "power" not in AnalysisPlan(seeds=(0, 1, 2)).model_dump()
    assert AnalysisPlan(seeds=(0, 1, 2), power=0.8).model_dump()["power"] == 0.8


def test_plant_lower_is_better_and_impossible_effects():
    labels, preds = ["1.0", "2.0", "3.0", "4.0"], ["2.0", "2.0", "5.0", "4.0"]  # mae 0.75
    planted = ml.plant("mae", labels, preds, 0.5, -1.0, random.Random(0))
    assert planted is not None and planted[2] == "3.0"  # only fixing example 2 (gain 0.5) reaches the gain
    assert ml.plant("mae", labels, preds, 0.8, -1.0, random.Random(0)) is None  # mae cannot drop below 0
