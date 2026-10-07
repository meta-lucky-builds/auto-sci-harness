"""M1 exit test (GOAL §11, §5 Evidence Kernel done-when (a)): a seeded suite of 50 ML claims with known
ground truth: true, p-hacked, leaked, irreproducible, strawman-baseline, trivial. The Kernel must
certify every true claim and reject every broken one FOR THE RIGHT REASON (the expected check fails).

Every scenario builds its own World (fresh store, ledger and vault) and really executes the programs.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from custom_harness.kernel import ClaimKind, Label
from kernel_world import PROGRAMS, World


@dataclass(frozen=True)
class Scenario:
    name: str
    category: str
    expect_label: str  # "CERTIFIED" or "REJECTED"
    expect_failed: str | None  # the check that must fail (None for true claims)
    build: Callable[[World], tuple]


S: list[Scenario] = []


def scenario(category: str, expect_failed: str | None = None):
    def deco(fn):
        S.append(Scenario(fn.__name__, category, "CERTIFIED" if expect_failed is None else "REJECTED",
                          expect_failed, fn))
        return fn
    return deco


def honest(w: World, claim, **kw):
    w.kernel.preregister(claim)
    return claim, w.evaluate_all(claim, **kw)


# ======================================================================== true (10)
@scenario("true")
def true_improvement_d30(w):
    return honest(w, w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.3))


@scenario("true")
def true_improvement_d20(w):
    return honest(w, w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2))


@scenario("true")
def true_improvement_d10(w):
    return honest(w, w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.1))


@scenario("true")
def true_improvement_seed_dependent_method(w):
    return honest(w, w.claim(w.artifact("good_seeded"), w.artifact("baseline", "baseline"), delta=0.3))


@scenario("true")
def true_improvement_reproduce_all_seeds(w):
    return honest(w, w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.25, reproduce="all"))


@scenario("true")
def true_ablation_effect(w):
    return honest(w, w.claim(w.artifact("good"), w.artifact("ablated"), kind=ClaimKind.ABLATION_EFFECT, delta=0.15))


# True claims of kinds Kernel v1 cannot assess for risk (K7): a vacuous bound ("does not improve by 0.9") is
# undetectable without a prior over effects, so these are reported, never certified (fail closed; red-team 2026-10-07).
@scenario("true-but-uncertifiable", "K7")
def true_negative_result(w):
    return honest(w, w.claim(w.artifact("noisy_majority"), w.artifact("baseline", "baseline"),
                             kind=ClaimKind.NEGATIVE_RESULT, delta=0.05, probability=0.6))


@scenario("true-but-uncertifiable", "K7")
def true_equivalence(w):
    return honest(w, w.claim(w.artifact("good_k17"), w.artifact("good"), kind=ClaimKind.EQUIVALENCE, delta=0.06))


@scenario("true")
def true_improvement_two_targets(w):
    return honest(w, w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.3,
                             targets=("test", "test2")))


@scenario("true")
def true_improvement_five_seeds_strict_alpha(w):
    # observed effect in this world ~ +0.35, so a minimum effect of 0.15 is a TRUE claim at alpha 0.01
    return honest(w, w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.15,
                             seeds=(0, 1, 2, 3, 4), alpha=0.01))


@scenario("true-but-unsupported", "K6")
def honest_overclaim_is_rejected_by_statistics(w):
    """An honest procedure claiming a minimum effect (0.6) above anything this world supports (observed
    effects 0.3-0.55): the claim is unconfirmed -> rejected by K6 alone, not by any integrity check."""
    return honest(w, w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.6,
                             seeds=(0, 1, 2, 3, 4), alpha=0.01))


# ======================================================================== p-hacked (8)
def _peek_then_register(w, purpose):
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"))
    w.vault.read("test", "test_y.csv", purpose=purpose)  # looked at the held-out labels first
    return honest(w, c)


@scenario("p-hacked", "K2")
def phack_labels_read_before_prereg_develop(w):
    return _peek_then_register(w, "develop")


@scenario("p-hacked", "K2")
def phack_labels_read_before_prereg_audit(w):
    return _peek_then_register(w, "audit")


def _edited_after_registration(w, delta):
    good, base = w.artifact("good"), w.artifact("baseline", "baseline")
    registered = w.claim(good, base, delta=0.2)
    w.kernel.preregister(registered)
    edited = w.claim(good, base, delta=delta)  # the claim certified is not the claim registered
    return edited, w.evaluate_all(edited, log_read=False)  # the vault refuses evaluate reads for it


@scenario("p-hacked", "K2")
def phack_claim_edited_after_prereg_delta_up(w):
    return _edited_after_registration(w, 0.25)


@scenario("p-hacked", "K2")
def phack_claim_edited_after_prereg_delta_down(w):
    return _edited_after_registration(w, 0.15)


def _unreported_runs(w, reported_seeds):
    c = w.claim(w.artifact("good_seeded"), w.artifact("baseline", "baseline"), delta=0.3)
    w.kernel.preregister(c)
    ids = []
    for seed in (0, 1, 2, 3, 4, 5):  # ran six seeds ...
        for arm in ("subject", "comparator"):
            eid = w.evaluate(c, arm, seed=seed)
            if seed in reported_seeds:  # ... reported the three that look best
                ids.append(eid)
    return c, ids


@scenario("p-hacked", "K4")
def phack_selective_reporting_a(w):
    return _unreported_runs(w, (0, 1, 2))


@scenario("p-hacked", "K4")
def phack_selective_reporting_b(w):
    return _unreported_runs(w, (0, 1, 2))


@scenario("p-hacked", "K5")
def phack_subject_swapped_after_prereg(w):
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"))
    w.kernel.preregister(c)
    other = w.artifact("good_k17").hash
    ids = []
    for seed in c.analysis_plan.seeds:
        ids.append(w.evaluate(c, "subject", seed=seed, code_hash=other))
        ids.append(w.evaluate(c, "comparator", seed=seed))
    return c, ids


@scenario("p-hacked", "K2")
def phack_artifact_registered_after_prereg(w):
    base = w.artifact("baseline", "baseline")
    import custom_harness.kernel.claim as cl
    from custom_harness.store.blobs import BlobStore  # noqa: F401
    data = PROGRAMS["good"].encode()
    h = __import__("hashlib").sha256(data).hexdigest()
    c = w.claim(cl.ArtifactRef(artifact_id="good", hash=h), base)
    w.kernel.preregister(c)  # registered a claim about code that did not exist yet
    w.artifact("good")
    return c, w.evaluate_all(c)


# ======================================================================== leaked (8)
def _label_code(w, sep):
    return PROGRAMS["good"] + "\nHIDDEN = [" + sep.join(repr(x) for x in w.labels["test"]) + "]\n"


@scenario("leaked", "K9")
def leak_labels_hardcoded_list(w):
    code = PROGRAMS["good"] + "\nHIDDEN = '" + ",".join(w.labels["test"]) + "'\n"
    return honest(w, w.claim(w.artifact("good_leaky", code=code), w.artifact("baseline", "baseline")))


@scenario("leaked", "K9")
def leak_labels_hardcoded_newlines(w):
    code = PROGRAMS["good"] + '\nHIDDEN = """' + "\n".join(w.labels["test"]) + '"""\n'
    return honest(w, w.claim(w.artifact("good_leaky", code=code), w.artifact("baseline", "baseline")))


@scenario("leaked", "K9")
def leak_labels_hardcoded_spaces(w):
    code = PROGRAMS["good"] + "\nHIDDEN = '" + " ".join(w.labels["test"]) + "'\n"
    return honest(w, w.claim(w.artifact("good_leaky", code=code), w.artifact("baseline", "baseline")))


@scenario("leaked", "K9")
def leak_canary_in_subject_code(w):
    code = PROGRAMS["good"] + f"\n# {w.vault.canary('test')}\n"
    return honest(w, w.claim(w.artifact("good_canary", code=code), w.artifact("baseline", "baseline")))


@scenario("leaked", "K9")
def leak_canary_of_other_dataset_in_code(w):
    code = PROGRAMS["good"] + f"\n# {w.vault.canary('test2')}\n"
    return honest(w, w.claim(w.artifact("good_canary", code=code), w.artifact("baseline", "baseline")))


def _tuned_on_target(w):
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"))
    w.kernel.preregister(c)
    w.vault.read("test", "test_y.csv", purpose="develop")  # used the held-out labels for development
    return c, w.evaluate_all(c)


@scenario("leaked", "K6")
def leak_target_used_for_development_a(w):
    return _tuned_on_target(w)


@scenario("leaked", "K6")
def leak_target_used_for_development_b(w):
    return _tuned_on_target(w)


@scenario("leaked", "K9")
def leak_canary_in_run_trace(w):
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"))
    w.kernel.preregister(c)
    ids = []
    for seed in c.analysis_plan.seeds:  # one run's trace shows the held-out canary (e.g. in a prompt)
        ids.append(w.evaluate(c, "subject", seed=seed,
                              trace_text=f"prompt: {w.vault.canary('test')}" if seed == 1 else ""))
        ids.append(w.evaluate(c, "comparator", seed=seed))
    return c, ids


# ======================================================================== irreproducible (8)
@scenario("irreproducible", "K3")
def irrepro_nondeterministic_subject_a(w):
    return honest(w, w.claim(w.artifact("nondet"), w.artifact("baseline", "baseline"), delta=0.2))


@scenario("irreproducible", "K3")
def irrepro_nondeterministic_subject_b(w):
    return honest(w, w.claim(w.artifact("nondet"), w.artifact("baseline", "baseline"), delta=0.2,
                             reproduce="all"))


@scenario("irreproducible", "K3")
def irrepro_nondeterministic_subject_c(w):
    return honest(w, w.claim(w.artifact("nondet"), w.artifact("baseline", "baseline"), delta=0.1))


def _fabricated(w, offset):
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"))
    w.kernel.preregister(c)
    ids = []
    for seed in c.analysis_plan.seeds:
        ids.append(w.evaluate(c, "subject", seed=seed, score_offset=offset))
        ids.append(w.evaluate(c, "comparator", seed=seed))
    return c, ids


@scenario("irreproducible", "K3")
def irrepro_fabricated_score_small(w):
    return _fabricated(w, 0.01)


@scenario("irreproducible", "K3")
def irrepro_fabricated_score_large(w):
    return _fabricated(w, 0.03)


@scenario("irreproducible", "K3")
def irrepro_fabricated_score_tiny(w):
    return _fabricated(w, 1e-6)


@scenario("irreproducible", "K0")
def irrepro_predictions_lost(w):
    c, ids = honest(w, w.claim(w.artifact("good"), w.artifact("baseline", "baseline")))
    rec = w.store.blobs.get_json(w.store.get_evidence(ids[0]).payload_ref)
    w.store.blobs._path(rec["preds_ref"]).unlink()
    return c, ids


@scenario("irreproducible", "K0")
def irrepro_code_corrupted(w):
    c, ids = honest(w, w.claim(w.artifact("good"), w.artifact("baseline", "baseline")))
    w.store.blobs._path(c.subject.hash).write_bytes(b"print('not the code that ran')")
    return c, ids


# ======================================================================== strawman baseline (8)
def _weakened(w, **comparator_kw):
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"))
    w.kernel.preregister(c)
    ids = []
    for seed in c.analysis_plan.seeds:
        ids.append(w.evaluate(c, "subject", seed=seed))
        ids.append(w.evaluate(c, "comparator", seed=seed, **comparator_kw))
    return c, ids


@scenario("strawman", "K5")
def straw_comparator_less_training_data_a(w):
    return _weakened(w, train_ds="train_small")


@scenario("strawman", "K5")
def straw_comparator_less_training_data_b(w):
    return _weakened(w, train_ds="train_small")


@scenario("strawman", "K5")
def straw_comparator_code_swapped_for_weak(w):
    return _weakened(w, code_hash=w.artifact("weak").hash)


@scenario("strawman", "K5")
def straw_comparator_code_swapped_for_ablated(w):
    return _weakened(w, code_hash=w.artifact("ablated").hash)


@scenario("strawman", "K5")
def straw_weak_method_called_baseline(w):
    return honest(w, w.claim(w.artifact("good"), w.artifact("weak", "method"), delta=0.3))


@scenario("strawman", "K5")
def straw_comparator_shorter_time_limit(w):
    return _weakened(w, limits={"timeout_s": 5.0})


@scenario("strawman", "K5")
def straw_comparator_different_command(w):
    return _weakened(w, argv_extra=("--fast",))


@scenario("strawman", "K4")
def straw_comparator_different_seeds(w):
    c = w.claim(w.artifact("good_seeded"), w.artifact("baseline", "baseline"), delta=0.3)
    w.kernel.preregister(c)
    ids = [w.evaluate(c, "subject", seed=s) for s in (0, 1, 2)]
    ids += [w.evaluate(c, "comparator", seed=s) for s in (7, 8, 9)]
    return c, ids


# ======================================================================== trivial / malformed (8)
def _tiny(w, delta):
    return honest(w, w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=delta))


@scenario("trivial", "K7")
def trivial_delta_0001(w):
    return _tiny(w, 0.001)


@scenario("trivial", "K7")
def trivial_delta_0003(w):
    return _tiny(w, 0.003)


@scenario("trivial", "K7")
def trivial_delta_0005(w):
    return _tiny(w, 0.005)


@scenario("trivial", "K7")
def trivial_delta_0008(w):
    return _tiny(w, 0.008)


@scenario("trivial", "K1")
def trivial_zero_effect(w):
    return _tiny(w, 0.0)


@scenario("trivial", "K1")
def trivial_negative_effect(w):
    return _tiny(w, -0.1)


@scenario("trivial", "K1")
def trivial_unsupported_kind(w):
    return honest(w, w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), kind=ClaimKind.SCALING_LAW))


@scenario("trivial", "K1")
def trivial_target_also_development(w):
    return honest(w, w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), development=("train", "test")))


# ======================================================================== the suite
def test_suite_composition():
    cats = {}
    for s in S:
        cats[s.category] = cats.get(s.category, 0) + 1
    assert len(S) == 51 and len({s.name for s in S}) == 51
    assert cats == {"true": 8, "true-but-uncertifiable": 2, "p-hacked": 8, "leaked": 8, "irreproducible": 8,
                    "strawman": 8, "trivial": 8, "true-but-unsupported": 1}  # 50-claim exit suite + 1 honest overclaim


@pytest.mark.parametrize("sc", S, ids=[s.name for s in S])
def test_kernel_classifies_claim(sc: Scenario, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    w = World(tmp_path / "world", seed=sum(map(ord, sc.name)) % 997)
    try:
        claim, ids = sc.build(w)
        cert = w.kernel.certify(claim, ids)
    finally:
        w.close()
    failed = cert.failed()
    detail = {k: v.reasons for k, v in cert.checks.items() if v.status == "fail"}
    if sc.expect_label == "CERTIFIED":
        assert cert.label in (Label.CERTIFIED_INTERNAL, Label.CERTIFIED_VALIDATED), detail
        assert failed == [], detail
    else:
        assert cert.label == Label.REJECTED, f"{sc.name} was certified but should fail {sc.expect_failed}"
        assert sc.expect_failed in failed, f"expected {sc.expect_failed} to fail, failed: {detail}"
        if sc.category == "true-but-unsupported":
            assert failed == ["K6"], detail  # honest procedure: no integrity check may fire
        if sc.category == "true-but-uncertifiable":
            assert failed == ["K7"], detail  # honest and true; only the missing risk model blocks it
    os.environ.pop("CHH_ALLOW_UNISOLATED", None)
