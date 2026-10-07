"""Red-team findings for the Kernel.

Open finding (2026-10-07), awaiting a statistics-policy decision by the user, pinned by a test of the CURRENT
behaviour so the gate fails the day it changes either way: K7 (risk) returns "n/a" for negative-result and equivalence
claims, and "n/a" counts as a pass, so a vacuous claim ("does not improve by 0.9 accuracy", "within 0.9 of each
other") is CERTIFIED although it could not have failed. The research loop only makes improvement claims today.
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
def test_open_gap_k7_certifies_vacuous_negative_and_equivalence_claims(tmp_path, monkeypatch, kind, subject,
                                                                       comparator, delta):
    """PINS the open gap exactly (certified, K7 n/a). When the policy is decided and implemented, this test
    must be turned into the secure assertion (REJECTED by K7); until then any change in behaviour fails here."""
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
    assert cert.failed() == [] and cert.checks["K7"].status == "n/a", (cert.label.value, cert.failed())
    assert cert.label.value == "CERTIFIED-INTERNAL"  # the gap: a claim that could not have failed is certified
