"""Clean-room model vs the real Kernel (differential), Kernel-side divergence pins, and metamorphic relations
(GOAL §16.6 "clean-room reference model plus metamorphic relations"; §16.7 "reputation may only raise scrutiny").

Every MR builds real Kernel stores and asserts on real certificates. Each MR also asserts that its perturbation
really happened (the TEST-BLIND lesson): the relation is checked against a baseline that differs in exactly the
perturbed way.
"""

from __future__ import annotations

import os

import gen
import kernel_model as model
import pytest
from history import ApproveRole, Artifact, Certify, ClaimSpec, Dataset, Event, Prereg, Read, Record
from hypothesis import HealthCheck, Phase, given, settings
from real_kernel import fresh

os.environ.setdefault("CHH_ALLOW_UNISOLATED", "1")
EX = frozenset(range(0, 40))
ARTS = list(gen.ARTIFACTS)
N_DIFF = int(os.environ.get("CHH_SPEC_EXAMPLES", "25"))


def claim(key: str = "c0", targets: tuple[str, ...] = ("t",), dev: tuple[str, ...] = (), subject: str = "m1",
          comparator: str = "b1", n_comparisons: int = 1, strong: bool = True) -> ClaimSpec:
    return ClaimSpec(key=key, kind="improvement", subject=subject, comparator=comparator, development=dev,
                     targets=targets, seeds=gen.SEEDS, alpha=0.05, n_comparisons=n_comparisons, min_effect=0.1,
                     strong=strong)


def honest(c: ClaimSpec) -> list[Event]:
    return [Prereg(c), *gen.eval_reads(c), Certify(c, gen.honest_records(c))]


def play(events: list[Event]) -> list[model.Verdict]:
    """Run events on a fresh real Kernel; returns the verdict of every Certify, in order."""
    world, tmp = fresh()
    out = []
    try:
        for ev in events:
            world.apply(ev)
            if isinstance(ev, Certify):
                out.append(world.last)
    finally:
        world.close()
        tmp.cleanup()
    return out


T = Dataset("t", "test", "features", EX, "A")


# ------------------------------------------------------------------ differential
@settings(max_examples=N_DIFF, deadline=None, database=None, phases=(Phase.explicit, Phase.generate),
          derandomize=os.environ.get("CHH_SPEC_RANDOM") != "1",
          suppress_health_check=list(HealthCheck))
@given(gen.scripts())
def test_model_and_kernel_agree_on_generated_histories(script):
    """The triaged clean-room model and the Kernel agree on every check, label, family size and vault decision.
    Known KERNEL-side divergences (pinned below as strict xfails) are excluded by their exact predicate only."""
    _history, dis, _ = gen.run(script)
    dis = [d for d in dis if not _known_kernel_side(d, script)]
    assert dis == [], dis


def _known_kernel_side(d: gen.Disagreement, script: list[Event]) -> bool:
    ev = script[d.step]
    if not isinstance(ev, Certify):
        return False
    ds = {e.id: e for e in script[:d.step] if isinstance(e, Dataset)}
    c = ev.claim
    # KD1: a development dataset, or a dataset read for development, sharing held-out examples passes K6
    used = set(c.development) | {e.dataset for e in script[:d.step] if isinstance(e, Read) and e.purpose == "develop"}
    kd1 = any(dv in ds and t in ds and dv != t and ds[dv].examples & ds[t].examples for dv in used for t in c.targets)
    # KD2: K7 judges risk on the targets that have statistics when another target has none
    kd2 = len(c.targets) > 1 and d.model == "fail" and d.real[0] == "pass"  # model fails it, Kernel passes
    return (d.where == "K6" and kd1) or (d.where == "K7" and kd2) or (d.where == "label" and kd1)


# ------------------------------------------------------------------ Kernel-side divergences found (pinned)
@pytest.mark.xfail(strict=True, reason="KD1 (found by the clean-room differential run 2026-10-07): K6 checks only "
                   "identical content and public LABELLED copies, so a development dataset that holds the same "
                   "held-out examples with private labels does not count as development use of the target")
def test_kd1_a_development_dataset_sharing_held_out_examples_fails_k6():
    dev = Dataset("devset", "dev", "features", EX, "B")
    c = claim(dev=("devset",))
    [v] = play([T, dev, *ARTS, Read("devset", "develop"), *honest(c)])
    assert model.certify_unregistered((T, dev), c, gen.honest_records(c)).checks["K6"] == "fail"  # model side
    assert v.checks["K6"] == "fail" and v.label == "REJECTED", v


def test_kd1_attack_really_certifies_today():
    """Positive control for KD1: the scenario above is CERTIFIED today (so the xfail is about the gap, not noise)."""
    dev = Dataset("devset", "dev", "features", EX, "B")
    [v] = play([T, dev, *ARTS, Read("devset", "develop"), *honest(claim(dev=("devset",)))])
    assert v.label == "CERTIFIED-INTERNAL", v


@pytest.mark.xfail(strict=True, reason="KD2 (differential run): with two targets and statistics on only one, K7 "
                   "passes on the subset; the label is still REJECTED (K4/K6 fail), so no wrong certificate")
def test_kd2_k7_fails_when_any_target_has_no_statistics():
    u = Dataset("u", "test", "features", frozenset(range(200, 240)), "U")
    c = claim(targets=("t", "u"))
    recs = gen.honest_records(c) + (Record("subject", 0, "u"),)
    [v] = play([T, u, *ARTS, Prereg(c), *gen.eval_reads(c), Certify(c, recs)])
    assert v.label == "REJECTED"
    assert v.checks["K7"] == "fail", v


# ------------------------------------------------------------------ metamorphic relations on the real Kernel
def test_mr_irrelevance_unrelated_datasets_and_reads_change_nothing():
    other = Dataset("z", "test", "features", frozenset(range(500, 540)), "Z")
    c = claim()
    base = play([T, *ARTS, *honest(c)])
    noisy = play([T, other, *ARTS, Read("z", "audit"), Read("z", "develop"), *honest(c)])
    assert base[0].label == "CERTIFIED-INTERNAL"
    assert noisy == base


def test_mr_permutation_evidence_order_does_not_matter():
    c = claim()
    fwd = play([T, *ARTS, Prereg(c), *gen.eval_reads(c), Certify(c, gen.honest_records(c))])
    rev = play([T, *ARTS, Prereg(c), *gen.eval_reads(c), Certify(c, tuple(reversed(gen.honest_records(c))))])
    assert fwd == rev and fwd[0].label == "CERTIFIED-INTERNAL"


def test_mr_renaming_ids_does_not_change_the_verdict():
    c = claim()
    base = play([T, *ARTS, *honest(c)])
    t2 = Dataset("renamed_target", "test", "features", EX, "A")
    arts = [Artifact("x_method", "method"), Artifact("x_other", "method"), Artifact("x_base", "baseline")]
    c2 = claim(key="renamed", targets=("renamed_target",), subject="x_method", comparator="x_base")
    renamed = play([t2, *arts, *honest(c2)])
    assert [v.label for v in renamed] == [v.label for v in base]
    assert [v.checks for v in renamed] == [v.checks for v in base]
    assert renamed[0].family == {"renamed_target": 1} and base[0].family == {"t": 1}


@pytest.mark.parametrize("bad", ["audit_before", "develop_on_target", "twin"])
def test_mr_fail_closed_monotonicity_suspicious_events_only_lower_the_label(bad):
    order = {"REJECTED": 0, "CERTIFIED-INTERNAL": 1, "CERTIFIED-VALIDATED": 2}
    c = claim()
    base = play([T, *ARTS, *honest(c)])[0]
    extra: list[Event] = {"audit_before": [Read("t", "audit")], "develop_on_target": [Read("t", "develop")],
                          "twin": [Dataset("t_twin", "test", "features", EX, "A")]}[bad]
    worse = play([T, *ARTS, *extra, *honest(c)])[0]
    assert base.label == "CERTIFIED-INTERNAL"
    assert order[worse.label] < order[base.label], (bad, worse)  # each of these is a real violation: strictly lower


def test_mr_honest_composition_two_claims_in_either_order_both_pass_k2():
    a, b = claim("a", subject="m1"), claim("b", subject="m2")
    ab = play([T, *ARTS, *honest(a), *honest(b)])
    ba = play([T, *ARTS, *honest(b), *honest(a)])
    assert [v.checks["K2"] for v in ab + ba] == ["pass"] * 4
    assert ab[1].family == {"t": 2} and ba[1].family == {"t": 2}  # the reuse really happened (charged by K4)


def test_mr_twin_equivalence_identical_labels_under_a_new_id_count_like_the_original():
    """Byte-identical private labels under another id are the same test set (R1-F4)."""
    twin = Dataset("t_copy", "test", "features", frozenset(range(300, 340)), "A")  # same labels, other rows
    same = Dataset("t_same", "test", "features", frozenset(range(300, 340)), "Q")  # control: other labels
    first = claim("first", targets=("t_copy",))
    first_ctrl = claim("first", targets=("t_same",))
    c = claim("second", subject="m2")
    with_twin = play([T, twin, *ARTS, Prereg(first), *honest(c)])[0]
    with_ctrl = play([T, same, *ARTS, Prereg(first_ctrl), *honest(c)])[0]
    with_orig = play([T, *ARTS, Prereg(claim("first")), *honest(c)])[0]
    assert with_twin.family == with_orig.family == {"t": 2}
    assert with_ctrl.family == {"t": 1}  # the twin, not any earlier claim, is what enlarged the family


def test_mr_arm_swap_never_certifies_both_directions():
    c = claim(comparator="m2")
    swapped = claim("swapped", subject="m2", comparator="m1")
    for comp_kind in ("baseline", "method"):
        arts = [Artifact("m1", "method"), Artifact("m2", comp_kind), Artifact("b1", "baseline")]
        v1 = play([T, *arts, *honest(c)])[0]
        v2 = play([T, *arts, *honest(swapped)])[0]
        assert not (v1.label.startswith("CERTIFIED") and v2.label.startswith("CERTIFIED")), (v1, v2)


@pytest.mark.xfail(strict=True, reason="as-of stability (invariant 17, §16.4, owned by kstats): verify() re-derives "
                   "against the CURRENT ledger, so a later develop read turns an honest certificate into a "
                   "'forged-looking' difference instead of a separate STALE status")
def test_mr_as_of_stability_later_events_do_not_change_verify():
    world, tmp = fresh()
    try:
        c = claim()
        for ev in [T, *ARTS, *honest(c)]:
            world.apply(ev)
        cert = world.last_cert
        assert cert.label.value == "CERTIFIED-INTERNAL"
        assert world.kernel.verify(cert.cert_hash()) == []
        world.apply(Read("t", "develop"))  # a later event, after the certificate
        assert world.kernel.verify(cert.cert_hash()) == []
    finally:
        world.close()
        tmp.cleanup()


# ------------------------------------------------------------------ governance: reputation only raises scrutiny
def test_mr_reputation_never_lowers_scrutiny():
    """§16.7 (the xz-utils lesson): a track record of CERTIFIED claims by the same subject, the same comparator and
    a human-approved external role never rescues a flawed claim, and the flawed claim's verdict is the same as in
    a fresh store without any history."""
    ext = Dataset("t", "external", "features", EX, "A")
    other = Dataset("u", "external", "features", frozenset(range(200, 240)), "U")
    past = [claim(f"past{i}", targets=("u",), n_comparisons=1) for i in range(3)]
    flawed = claim("flawed", subject="m1")
    peek = [Read("t", "audit")]
    history = [ext, other, *ARTS, ApproveRole("u"), ApproveRole("t")]
    for p in past:
        history += honest(p)
    with_rep = play([*history, *peek, *honest(flawed)])
    fresh_v = play([ext, *ARTS, ApproveRole("t"), *peek, *honest(flawed)])[0]
    assert [v.label for v in with_rep[:3]] == ["CERTIFIED-VALIDATED"] * 3  # the reputation really exists
    assert with_rep[-1].label == fresh_v.label == "REJECTED"
    assert with_rep[-1].checks == fresh_v.checks


def test_certify_never_evaluates_k3_to_k9_for_an_unregistered_claim():
    """Kills the museum survivor of K-3 (found 2026-10-07): with the Kernel's own guard removed, the vault's refusal
    still hid the labels, so no test noticed the guard was gone. The guard itself must hold: every check after K2
    reports 'not evaluated' instead of running (and failing for some other reason)."""
    c = claim()
    world, tmp = fresh()
    try:
        for ev in [T, *ARTS]:
            world.apply(ev)
        world.certify(c, gen.honest_records(c))
        checks = world.last_cert.checks
    finally:
        world.close()
        tmp.cleanup()
    for k in ("K3", "K4", "K5", "K6", "K7", "K8", "K9"):
        assert checks[k].status == "fail" and checks[k].reasons[0].startswith("not evaluated"), (k, checks[k])


def _labelled_target_verdict(n: int) -> model.Verdict:
    """KD3 scenario: an external target whose PUBLIC file (the program's input) carries the TRUE labels as a column."""
    import real_kernel as rk

    from custom_harness.kernel import DatasetRole
    world, tmp = fresh()
    try:
        labels = rk.token_labels("A")[:n] if n <= rk.N else None
        assert labels is not None
        rows = [rk.example_row(i) for i in range(n)]
        pub = rk._csv([["x1", "x2", "label"], *[[a, b, y] for (a, b, _), y in zip(rows, labels, strict=True)]])
        world.kernel.vault.register_dataset("t", DatasetRole.EXTERNAL, public={"public.csv": pub},
                                            private={"labels.csv": rk.private_bytes("A")})
        world.datasets["t"] = Dataset("t", "external", "labelled", frozenset(range(n)), "A")
        original = rk.public_bytes
        rk.public_bytes = lambda d: pub if d.id == "t" else original(d)  # the record names the registered input
        try:
            for ev in [*ARTS, ApproveRole("t"), *honest(claim())]:
                world.apply(ev)
        finally:
            rk.public_bytes = original
        assert pub.decode().splitlines()[1].endswith("," + labels[0])  # the labels really are in the input
        return world.last
    finally:
        world.close()
        tmp.cleanup()


@pytest.mark.xfail(strict=True, reason="KD3 (found 2026-10-07 while triaging the differential run): a held-out "
                   "target whose public input file carries its true labels as a column is CERTIFIED-VALIDATED; K6 "
                   "compares only against OTHER datasets' public files, K8 only byte-identical private files, and "
                   "K9 label runs need consecutive labels")
def test_kd3_a_target_whose_public_input_carries_its_labels_is_rejected():
    assert _labelled_target_verdict(40).label == "REJECTED"


def test_kd3_attack_really_certifies_today():
    assert _labelled_target_verdict(40).label == "CERTIFIED-VALIDATED"
