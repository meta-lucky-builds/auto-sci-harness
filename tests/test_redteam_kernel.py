"""Red-team findings for the Kernel (each test asserts the SECURE behaviour).

Found 2026-10-07: K7 (risk) returned "n/a" for negative-result and equivalence claims, and "n/a" counted as a pass,
so a vacuous claim ("does not improve by 0.9 accuracy", "within 0.9 of each other") was CERTIFIED although it
could not have failed. No noise-based rule can tell a vacuous bound from a tight one (at the claimed bound, success
is always <= alpha); that needs a prior over effects, which v1 does not have. Decision: fail closed (K7 fails these
kinds; certification counts only "pass").
"""

import pytest

from custom_harness.kernel import ClaimKind, Effect
from kernel_world import World


@pytest.mark.parametrize(("kind", "subject", "comparator", "delta"), [
    # "noisy_majority does not beat the baseline by 0.9 accuracy": true, but says nothing (no method can)
    (ClaimKind.NEGATIVE_RESULT, "noisy_majority", "baseline", 0.9),
    # "good_k17 and good are within 0.9 accuracy of each other": true for ANY two classifiers
    (ClaimKind.EQUIVALENCE, "good_k17", "good", 0.9),
])
def test_k7_vacuous_negative_and_equivalence_claims_are_not_certified(tmp_path, monkeypatch, kind, subject,
                                                                      comparator, delta):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    w = World(tmp_path / "w", seed=11)
    try:
        claim = w.claim(w.artifact(subject, "baseline" if subject == "baseline" else "method"),
                        w.artifact(comparator, "baseline" if comparator == "baseline" else "method"),
                        kind=kind, delta=delta, probability=0.6)
        # a well-formed claim (the helper's predicted interval is [delta, 0.8], invalid for delta > 0.8)
        claim = claim.model_copy(update={"effect": Effect(min_effect=delta, predicted_lo=-0.1, predicted_hi=0.1,
                                                          probability=0.6)})
        w.kernel.preregister(claim)
        cert = w.kernel.certify(claim, w.evaluate_all(claim))
    finally:
        w.close()
    assert cert.label.value == "REJECTED" and cert.failed() == ["K7"], (cert.label.value, cert.failed())
    assert "no risk model" in " ".join(cert.checks["K7"].reasons)
