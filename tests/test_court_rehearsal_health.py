"""Rehearsal, the power check and the Court health run (GOAL 18.6; court/rehearsal.py, court/health.py)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from _court_world import part
from custom_harness.court import health as H
from custom_harness.court import minted as C
from custom_harness.court import rehearsal as R
from custom_harness.kernel import Kernel
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore
from test_sealed_project import Project, rings_csv


def _factory(root: Path):
    n = [0]

    def make(key: str) -> LocalSandbox:
        n[0] += 1
        return LocalSandbox(root / "sb" / f"{n[0]}-{key}", i_understand_no_isolation=True)
    return make


@pytest.fixture
def pj(tmp_path):
    p = Project(tmp_path / "p", parts=1)
    p.session()
    yield p
    p.close()


def _lab_file(tmp_path: Path) -> Path:
    f = tmp_path / "lab.csv"
    f.write_bytes(rings_csv(900, seed=11))
    return f


def test_a_rehearsal_shows_everything_and_spends_nothing(pj, tmp_path):
    ledger = Kernel(pj.store).ledger
    before = len(ledger.entries())
    pid = pj.freeze()
    rep = R.rehearse(pj.store, pid, _lab_file(tmp_path), label_column="outcome", rows=300, sandbox_factory=_factory)
    assert rep["label"] == R.NOT_EVIDENCE and rep["state"] == "RAN"
    assert set(rep["checks"]) == {f"K{i}" for i in range(10)}
    assert rep["would_label"].startswith("CERTIFIED") and rep["failed"] == []
    assert rep["scores"]["subject"][0] > rep["scores"]["comparator"][0]
    assert len(ledger.entries()) == before  # not one ledger entry in the real store
    assert pj.store.records(R.RECORD_KIND)[-1]["label"] == R.NOT_EVIDENCE
    assert not C.cases_of_promotion(pj.store, pid)  # the promotion can still go to the Court
    assert rep["readiness"]["label"] == "UNCALIBRATED" and rep["readiness"]["can_only"] == "deny or warn"


def test_a_failing_rehearsal_names_the_failure(pj, tmp_path):
    bad = b"def build_model(seed):\n    raise ValueError('broken interface')\n"
    rep = R.rehearse(pj.store, pj.freeze(method=bad), _lab_file(tmp_path), label_column="outcome", rows=300,
                     sandbox_factory=_factory)
    assert rep["state"] == "COULD NOT RUN" and "broken interface" in rep["reason"]
    assert rep["readiness"]["verdict"] == "not run"


def test_a_rehearsal_refuses_private_data(pj, tmp_path):
    f = tmp_path / "copy.csv"
    k = Kernel(pj.store)
    target = pj.seal["targets"][0]
    f.write_bytes(pj.store.blobs.get(k.vault.dataset(target).files["test_y.csv"].blob))
    with pytest.raises(R.RehearsalRefused, match="private"):
        R.rehearse(pj.store, pj.freeze(), f, label_column="label", rows=2, sandbox_factory=_factory)


def test_readiness_only_denies_or_warns():
    p = part(min_effect=0.05)
    good = R.readiness(p, (500, 300, 40, 160), sizes=(4500,))
    assert good["verdict"] == "no objection" and good["smallest_size_with_no_objection"] == 4500
    hopeless = R.readiness(p, (500, 40, 300, 160), sizes=(1000,))
    assert hopeless["verdict"].startswith("unlikely to certify") and "grant" not in str(hopeless)
    assert R.readiness(p, None)["verdict"] == "not run"
    assert R.readiness(part("rmse", 0.1), (1, 1, 1, 1))["verdict"] == "not run"


def test_outcome_counts():
    assert R.outcome_counts(["a", "b", "a", "b"], ["a", "b", "b", "a"], ["a", "a", "a", "a"]) == (1, 1, 1, 1)


# ------------------------------------------------------------------------------------------- health run
@pytest.fixture(scope="module")
def health(tmp_path_factory):
    root = tmp_path_factory.mktemp("h")
    store = EvidenceStore(root / "store")
    with pytest.MonkeyPatch.context() as m:  # module scope: the function-scoped conftest switch is not on yet
        m.setenv("CHH_ALLOW_UNISOLATED", "1")
        body = H.run_health(store, _factory)
    yield store, body
    store.close()


@pytest.mark.parametrize("plant", H.PLANTS, ids=[p.name for p in H.PLANTS])
def test_each_plant_gets_its_right_answer(health, plant):
    r = next(x for x in health[1]["results"] if x["plant"] == plant.name)
    assert r["correct"], r


def test_the_health_run_is_recorded_and_passes(health):
    store, body = health
    assert body["passed"] and H.passing(store)["health_hash"] == body["health_hash"]
    assert H.require_passed(store) == body["health_hash"]
    assert len(body["health_hash"]) == 64


def test_a_changed_court_needs_a_new_health_run(health, monkeypatch):
    store, _ = health
    monkeypatch.setattr(H, "court_version", lambda: "0" * 64)
    assert H.passing(store) is None
    with pytest.raises(C.CourtRefused, match="health run"):
        H.require_passed(store)


def test_a_plant_that_merely_failed_to_run_is_not_a_correct_answer():
    plant = next(p for p in H.PLANTS if p.name == "leaky")
    assert not H._correct(plant, {"cert_hash": None, "reason": "infrastructure: no sandbox"}, False)
    straw = next(p for p in H.PLANTS if p.name == "strawman_baseline")
    assert not H._correct(straw, {"cert_hash": None, "reason": "infrastructure: no sandbox"}, False)
    assert H._correct(straw, {"cert_hash": None, "reason": "the comparator is a strawman: ..."}, False)


def test_comparator_rules():
    from custom_harness.court import comparator as K
    assert K.worse_than_trivial("accuracy", 0.1, 0.33) and not K.worse_than_trivial("accuracy", 0.30, 0.33)
    assert K.worse_than_trivial("rmse", 2.0, 1.0) and not K.worse_than_trivial("rmse", 1.01, 1.0)
    assert K.trivial_predictions("accuracy", ["a", "b", "b"], 2) == ["b", "b"]
    assert K.trivial_predictions("rmse", ["1", "3"], 1) == ["2.0"]
    _, x, y, labels = K.public_split(b"a,label\n" + b"".join(f"{i},{i % 2}\n".encode() for i in range(30)))
    assert len(y) + len(labels) == 30 and y and labels and b"label" not in x.splitlines()[0]


def test_a_comparator_that_cannot_run_is_left_to_the_real_run(tmp_path):
    from custom_harness.court import comparator as K
    train = b"a,label\n" + b"".join(f"{i},{i % 2}\n".encode() for i in range(30))
    sb = LocalSandbox(tmp_path / "s", i_understand_no_isolation=True)
    assert K.comparator_problem(b"raise SystemExit(3)\n", "accuracy", train, sb, 0, 60) is None


def test_a_partial_or_failing_run_does_not_count(tmp_path, monkeypatch):
    store = EvidenceStore(tmp_path / "store")
    try:
        one = (replace(H.PLANTS[3], certifies=True),)  # told to expect a certificate: the plant fails that
        body = H.run_health(store, _factory, one)
        assert not body["passed"] and H.passing(store) is None
    finally:
        store.close()


def test_a_case_cannot_register_without_a_health_run(tmp_path):
    p = Project(tmp_path / "p", parts=1, health=False)
    try:
        p.session()
        with pytest.raises(C.CourtRefused, match="health run"):
            C.register_sealed(p.store, p.freeze(), p.project, anchor_file=p.anchor())
    finally:
        p.close()


def test_the_certificate_carries_the_health_hash(tmp_path):
    p = Project(tmp_path / "p", parts=2)
    try:
        p.session()
        body = C.register_sealed(p.store, p.freeze(), p.project, anchor_file=p.anchor(), revalidate=True)
        out = p.run(body)
        cert = Kernel(p.store).certificate(out["cert_hash"])
        hash_ = H.require_passed(p.store)
        assert any(hash_ in a.text for a in cert.assumptions)
        assert body["revalidation"]["health_hash"] == body["health_hash"] == hash_
    finally:
        p.close()
