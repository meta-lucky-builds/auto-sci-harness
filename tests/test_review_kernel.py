"""Adversarial integrity review of the Evidence Kernel, the adapter registry, the approval node and the LOFO gate
(review round r1, 2026-10-07).

Every test asserts the SECURE behaviour. A test that fails on the reviewed code is a verified finding; a test that
passes is a regression test for a defence that held. Tests marked xfail(strict=True) document a limitation that
STATUS.md already lists (they turn into an XPASS failure the day the limitation is removed).
"""

from __future__ import annotations

import ast
import importlib
import math
import os
import sys
from types import SimpleNamespace

import pytest

from custom_harness.evolve.evaluate import SuiteTask, check_held_out, held_out
from custom_harness.evolve.selection import PairedSample, non_regression
from custom_harness.kernel import DatasetRole, Label, VaultError
from custom_harness.kernel import adapters as A
from custom_harness.kernel import checks as K
from custom_harness.kernel.certificate import CheckResult
from custom_harness.kernel.claim import EvidenceClass
from custom_harness.kernel.execution import ExecRecord
from custom_harness.runtime.engine import approval_item_hash
from kernel_world import KNN, World

os.environ.setdefault("CHH_ALLOW_UNISOLATED", "1")


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path / "w", seed=21, n_train=120, n_test=120)
    yield world
    world.close()


def _record(w: World, eid: str) -> ExecRecord:
    return ExecRecord.model_validate(w.store.blobs.get_json(w.store.get_evidence(eid).payload_ref))


def _honest(w: World, subject: str = "good", **kw):
    return w.claim(w.artifact(subject), w.artifact("baseline", "baseline"), delta=0.2, n_boot=1000, **kw)


# ------------------------------------------------------------------ control: the honest world certifies
def test_r1_control_honest_claim_certifies(w):
    """Control for the tests below: without the attack, the same claim is CERTIFIED (so a REJECTED outcome in an
    attack test is caused by the defence, not by a weak scenario)."""
    c = _honest(w)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    assert cert.label == Label.CERTIFIED_INTERNAL, cert.failed()


# ------------------------------------------------------------------ K2/K4: peeking under a registered claim
_LEAKSCAN_BOUNDARY = (
    "documented TRUST BOUNDARY (docs/kernel.md, 'Integrity reads'): K2 lists a 'leakscan' read before registration "
    "as an observation instead of counting it as peeking, because the research audit must build leak fingerprints "
    "from the held-out labels of a task that later runs claim again (K2-TWO-RUN). Vault purposes are declared by "
    "trusted host code; the Kernel cannot tell a mislabelled peek from a real leak scan. Strict: an XPASS means the "
    "boundary was closed and this marker must go.")


@pytest.mark.parametrize("purpose", ["evaluate", "kernel",
                                     pytest.param("leakscan", marks=pytest.mark.xfail(strict=True,
                                                                                      reason=_LEAKSCAN_BOUNDARY)),
                                     "reproduce"])
def test_r1_label_read_under_a_registered_claim_on_another_target_is_peeking(w, purpose):
    """Attack: register a throwaway claim on target test2, then read the labels of target `test` 'for' it (a
    registered claim, so the vault allows the read and K2 treats it as test-set reuse), then register and certify
    the real claim on `test`. The throwaway claim does not name `test`, so K4's Bonferroni family does not count it
    either: the look at the labels is charged nowhere. Secure: the read is refused, or the later claim is rejected."""
    cover = _honest(w, targets=("test2",))
    w.kernel.preregister(cover)
    kw = {"arm": "subject", "seed": 0} if purpose == "evaluate" else {}
    try:
        w.vault.read("test", "test_y.csv", purpose=purpose, claim_hash=cover.claim_hash(), **kw)
    except VaultError:
        return  # defence held at the vault
    c = _honest(w, subject="good_k17")
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    assert cert.label == Label.REJECTED, (
        f"labels of 'test' read ({purpose}) under a registered claim on another target before pre-registration; "
        f"certificate {cert.label.value}; K2 obs {cert.checks['K2'].observations}, K4 obs "
        f"{cert.checks['K4'].observations}")


# ------------------------------------------------------------------ K3: the sampled replay seed can be steered
STEERED = KNN.format(k=15, pre="", post=(
    "if a.seed != 1:  # deterministic ONLY for seed 1\n"
    "    rng = random.SystemRandom()\n"
    "    preds = [p if rng.random() > 0.1 else rng.choice(['0', '1', '2']) for p in preds]"))


def _replay_seed(c, w: World, ids: list[str], arm: str, target: str) -> int:
    recs = [(e, _record(w, e)) for e in ids]
    ctx = SimpleNamespace(records=recs, claim_hash=c.claim_hash(), claim=c)
    rec = next(r for _, r in recs if r.arm == arm and r.target == target)
    return K.replay_seed(ctx, rec)  # type: ignore[arg-type]


def test_r1_replay_seed_cannot_be_steered_with_records_on_non_target_datasets(w):
    """Attack on K3 'sampled_seed' (the v1.1 fix for a predictable replay seed): the seed is derived from a hash of
    ALL submitted records, and records on datasets that are NOT generalization targets of the claim are accepted
    by every check (K4/K5/K6 only look at the claim's targets). So the claimant appends harmless extra records
    (here: the deterministic comparator on another registered dataset) until the hash selects the one seed on
    which the subject is deterministic. Secure: the claim is not certified.

    Revised with the R1-F2 fix: (a) the extra records are written WITHOUT a vault read (since R1-F1 the vault refuses
    an 'evaluate' read of a dataset that is not a target of the named claim, so a claimant would fabricate them; the
    attack on the hash is the same); (b) the original version was flaky by construction: with probability 1/3 the
    UNSTEERED seed is already 1, and the claim then certified without any attack (the documented single-replay
    residual, not this finding). The secure behaviour asserted now holds in every case: extra records never move the
    replay seed, and a submission containing them is rejected."""
    c = w.claim(w.artifact("steered", code=STEERED), w.artifact("baseline", "baseline"), delta=0.2, n_boot=1000)
    w.kernel.preregister(c)
    ids = w.evaluate_all(c)
    unsteered = _replay_seed(c, w, ids, "subject", "test")
    if unsteered != 1:  # control: unsteered, K3 catches the irreproducible subject
        assert "K3" in w.kernel.certify(c, ids, record=False).failed()
    extras: list[str] = []
    for i in range(40):  # the attack: add records until the seed MOVES to 1 (at least one record)
        if extras and _replay_seed(c, w, ids + extras, "subject", "test") == 1:
            break
        extras.append(w.evaluate(c, "comparator", seed=100 + i, target="test2", log_read=False))
    assert _replay_seed(c, w, ids + extras, "subject", "test") == unsteered, (
        f"{len(extras)} extra non-target record(s) moved K3's replay seed from {unsteered}")
    cert = w.kernel.certify(c, ids + extras)
    assert "outside the analysis plan" in " ".join(cert.checks["K4"].reasons), cert.checks["K4"].reasons
    assert cert.label == Label.REJECTED, (
        f"subject is irreproducible on seeds 0 and 2, yet {len(extras)} extra non-target record(s) steered K3's "
        f"replay to seed 1 and the claim was {cert.label.value}")


# ------------------------------------------------------------------ K3: certification shopping
FLAKY = KNN.format(k=15, pre="", post=(
    "if random.SystemRandom().random() < 0.5:  # per PROCESS: half the executions corrupt 10 predictions\n"
    "    preds = ['0'] * 10 + preds[10:]"))


def test_r1_repeated_certification_cannot_shop_for_a_lucky_replay(w):
    """Attack: the subject reproduces only on half of its executions. K3 re-executes the sampled seed once per
    certify call, and certify may be called again on the same claim and evidence; the newest certificate is what
    the prediction ledger scores. Re-certifying until one replay happens to match turns a K3 rejection into a
    certificate. Secure: once a claim's evidence was rejected because it did not reproduce, the same claim and
    evidence never become CERTIFIED."""
    c = w.claim(w.artifact("flaky", code=FLAKY), w.artifact("baseline", "baseline"), delta=0.2, n_boot=1000)
    w.kernel.preregister(c)
    ids = w.evaluate_all(c)
    labels, k3_rejected = [], False
    for _ in range(25):
        cert = w.kernel.certify(c, ids)
        labels.append(cert.label.value)
        k3_rejected |= "K3" in cert.failed()
        if k3_rejected and cert.label != Label.REJECTED:
            break
    assert k3_rejected, f"K3 never failed in 25 attempts (scenario too weak): {labels}"
    assert labels[-1] == Label.REJECTED.value, f"irreproducible claim certified by retrying: {labels}"


# ------------------------------------------------------------------ K4: multiplicity family through shared labels
def test_r1_bonferroni_family_includes_targets_with_byte_identical_private_labels(w):
    """The same held-out labels (byte-identical private file) registered under another dataset id, with the public
    feature file projected to a subset of columns (n1 dropped: the programs never use it). `shared_examples`
    requires the target's whole header in the other file, so the two targets are not 'related' and a claim on one
    is not charged for the claim on the other, although they are tested against the same labels. Secure: the
    family has 2 claims."""
    lines = w.vault.read_public("test", "test_X.csv").decode().splitlines()
    proj = "\n".join(",".join(ln.split(",")[:2]) for ln in lines) + "\n"
    labels = w.store.blobs.get(w.vault.dataset("test").files["test_y.csv"].blob)
    w.vault.register_dataset("test_proj", DatasetRole.TEST, public={"test_X.csv": proj.encode()},
                             private={"test_y.csv": labels})
    other = _honest(w, targets=("test_proj",))
    w.kernel.preregister(other)
    c = _honest(w, subject="good_k17")
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    obs = cert.checks["K4"].observations
    assert any("2 claim(s) tested on this target" in o for o in obs), obs


@pytest.mark.xfail(strict=True, reason="documented in STATUS.md: shared_examples is an exact-text match")
def test_r1_shared_examples_survive_a_column_rename(w):
    """Documented limitation, confirmed: renaming one feature column of a copy of the held-out examples (with
    their labels appended) hides the copy from K6 and from the K4 family."""
    lines = w.vault.read_public("test", "test_X.csv").decode().splitlines()
    header = lines[0].replace("n1", "noise")
    leaky = "\n".join([f"{header},label", *[f"{r},{y}" for r, y in zip(lines[1:], w.labels["test"], strict=True)]])
    w.vault.register_dataset("renamed", DatasetRole.DEV, public={"train.csv": (leaky + "\n").encode()}, private={})
    assert K.shared_examples(w.vault, "test", "renamed")


# ------------------------------------------------------------------ K4: unreported arms (held)
def test_r1_extra_evaluate_reads_under_another_arm_name_are_selective_reporting(w):
    """Held: evaluating an extra variant under an invented arm name ('subject_alt') leaves a logged read that is
    not among the reported records, so K4 fails it."""
    c = _honest(w)
    w.kernel.preregister(c)
    w.vault.read("test", "test_y.csv", purpose="evaluate", claim_hash=c.claim_hash(), arm="subject_alt", seed=0)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    assert "K4" in cert.failed() and "not reported" in " ".join(cert.checks["K4"].reasons)


# ------------------------------------------------------------------ fail closed (held)
def test_r1_a_check_returning_na_never_counts_as_pass(w, monkeypatch):
    c = _honest(w)
    w.kernel.preregister(c)
    ids = w.evaluate_all(c)
    monkeypatch.setitem(K.ALL_CHECKS, "K7", lambda ctx: CheckResult(status="n/a", reasons=("no model",)))
    assert w.kernel.certify(c, ids, record=False).label == Label.REJECTED


def test_r1_a_check_raising_any_non_infra_exception_fails(w, monkeypatch):
    c = _honest(w)
    w.kernel.preregister(c)
    ids = w.evaluate_all(c)

    def boom(ctx):
        raise ZeroDivisionError("x")

    monkeypatch.setitem(K.ALL_CHECKS, "K8", boom)
    cert = w.kernel.certify(c, ids, record=False)
    assert cert.label == Label.REJECTED and cert.checks["K8"].status == "fail"


# ------------------------------------------------------------------ claim hashes (held)
def test_r1_claim_hash_distinguishes_every_evidence_class_and_omits_only_the_default(w):
    c = _honest(w)
    hashes = {c.model_copy(update={"evidence_class": e}).claim_hash() for e in EvidenceClass}
    assert len(hashes) == len(EvidenceClass)  # no two classes collide (only `data` is omitted from the bytes)
    assert c.model_copy(update={"evidence_class": EvidenceClass.DATA}).claim_hash() == c.claim_hash()


# ------------------------------------------------------------------ adapters
def test_r1_adapter_identity_covers_the_modules_its_modules_import(tmp_path, monkeypatch):
    """Invariant 13: an adapter change needs a new human approval. The identity hashes only the LISTED modules'
    source bytes, not what they import: changing a helper module the adapter imports changes the adapter's
    behaviour while its approval stays valid. Secure: the approval no longer resolves."""
    (tmp_path / "rv_dep_mod.py").write_text("THRESHOLD = 1\n")
    (tmp_path / "rv_adapter_mod.py").write_text("from rv_dep_mod import THRESHOLD\n\ndef ok(x):\n"
                                                "    return x >= THRESHOLD\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = A.AdapterSpec("ml", "1", frozenset({EvidenceClass.DATA}), ("rv_adapter_mod",), frozenset({"accuracy"}))
    monkeypatch.setitem(A.REGISTRY, "ml", (spec, (("trivially true", lambda: True),)))
    w = World(tmp_path / "w", approve_adapters=False)
    try:
        w.kernel.approve_adapter("ml", approver="alice")
        assert A.resolve(w.kernel.ledger, EvidenceClass.DATA, "accuracy")[0] is not None
        (tmp_path / "rv_dep_mod.py").write_text("THRESHOLD = -1e9  # unapproved change, accepts everything\n")
        for m in ("rv_dep_mod", "rv_adapter_mod"):
            sys.modules.pop(m, None)
        assert importlib.import_module("rv_adapter_mod").ok(-5)  # behaviour did change
        adapter, _why = A.resolve(w.kernel.ledger, EvidenceClass.DATA, "accuracy")
        assert adapter is None, "adapter behaviour changed through an imported module; the approval still resolves"
    finally:
        w.close()


def _internal_imports(modname: str) -> set[str]:
    mod = importlib.import_module(modname)
    tree = ast.parse(open(str(mod.__file__), encoding="utf-8").read())
    pkg = modname.rsplit(".", 1)[0]
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = pkg.split(".")
            base = base[: len(base) - (node.level - 1)]
            if node.module:
                out.add(".".join([*base, node.module]))
            else:  # `from . import name`
                out |= {".".join([*base, a.name]) for a in node.names}
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("custom_harness"):
            out.add(str(node.module))
    return {m for m in out if m.startswith("custom_harness") and importlib.util.find_spec(m) is not None}


def test_r1_ml_adapter_hash_covers_its_internal_imports():
    """The real ML adapter: every custom_harness module imported by one of its modules is inside its hash.
    (Since the R1-F5 fix the hash covers `A.covered_modules(spec)`, the listed modules plus their transitive project
    imports; this test checks that set, which is exactly what `adapter_hash` hashes, and its transitive closure.)"""
    covered = set(A.covered_modules(A.ML))
    assert set(A.ML.modules) <= covered
    missing = sorted({m for mod in covered for m in _internal_imports(mod) if m not in covered})
    assert missing == [], f"ML adapter behaviour depends on modules outside its hash: {missing}"


# ------------------------------------------------------------------ approval item hash (held)
def test_r1_approval_item_hash_is_canonical_and_type_exact():
    assert approval_item_hash({"a": 1, "b": [1, 2]}) == approval_item_hash({"b": [1, 2], "a": 1})
    distinct = [1, 1.0, True, "1", -0.0, 0.0, None, [1], {"a": 1}, "é", "é"]
    assert len({approval_item_hash(x) for x in distinct}) == len(distinct)


# ------------------------------------------------------------------ LOFO (held)
def _t(i: str, split: str, family: str, domain: str = "ml") -> SuiteTask:
    from pathlib import Path
    return SuiteTask(i, Path("."), split, family=family, domain=domain)  # type: ignore[arg-type]


def test_r1_a_family_present_only_in_sealed_or_also_in_evolve_is_not_held_out():
    assert held_out([("sealed", "x"), ("evolve", "a"), ("gate", "a")]) == []
    with pytest.raises(ValueError, match="no held-out family"):
        check_held_out([_t("e", "evolve", "a"), _t("g", "gate", "a"), _t("s", "sealed", "x")])


def test_r1_held_out_family_with_mostly_excluded_or_too_few_units_fails_closed():
    units = [("g", s) for s in range(4)]
    mostly_excluded = PairedSample(units, [0.5], excluded=units[1:])
    assert non_regression(mostly_excluded)["passed"] is False
    one_unit = PairedSample(units[:1], [0.5], [])
    assert non_regression(one_unit)["passed"] is False


def test_r1_non_regression_rejects_a_non_finite_difference_instead_of_raising():
    """non_regression's docstring promises to fail closed on non-finite differences, but it computes the CI first,
    and mean_t_interval raises on NaN; _lofo runs even after the pooled gate rejected, so the generation crashes.
    (Production scores are clipped finite by score_state, so this is defence in depth.)"""
    units = [("g", s) for s in range(2)]
    for bad in (math.nan, math.inf):
        try:
            r = non_regression(PairedSample(units, [0.1, bad], []))
        except Exception as e:
            pytest.fail(f"non_regression raised {type(e).__name__} on {bad!r} instead of failing closed")
        assert r["passed"] is False
