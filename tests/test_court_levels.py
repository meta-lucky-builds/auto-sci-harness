"""Verification levels (GOAL §17.1; court/levels.py, int18 stage-a): computed by CODE from the certificate, the ledger,
the vault and the Court case records; cumulative; public datasets capped as RETROSPECTIVE.

Fail-first reasoning: on int18 before stage-a's Stage A commit there is no custom_harness.court (ImportError for
every test). Each test also names the rule it pins:
  - ladder: VALIDATED needs REPLICATED needs REPRODUCIBLE (cumulative); a lower gap stops the climb;
  - classify_target: a Claim Audit OpenML split is retrospective whatever its role or timing;
  - a REJECTED certificate is NONE and names the failing checks;
  - a certified claim without a recorded bundle is not REPRODUCIBLE; with `bundle` it is;
  - a CERTIFIED-VALIDATED claim on a public-dataset target is capped (RETROSPECTIVE) and never VALIDATED;
  - a certified claim without a replica stops at REPRODUCIBLE, whatever its label (cumulative levels);
  - the lab trace refuses a released level outside promote/verdict_released.
Built, NOT test-verified (owner rule: no test runs this round)."""

from __future__ import annotations

import pytest

from custom_harness.court.levels import LEVELS, classify_target, ladder, level
from custom_harness.kernel import DatasetRole
from custom_harness.kerneld.scope import ScopedKernel
from kernel_world import World


def test_ladder_is_cumulative():
    assert ladder({}) == "VALIDATED"
    assert ladder({"REPRODUCIBLE": ["no bundle"]}) == "NONE"
    assert ladder({"REPLICATED": ["no replica"]}) == "REPRODUCIBLE"
    assert ladder({"REPLICATED": ["no replica"], "VALIDATED": []}) == "REPRODUCIBLE"  # VALIDATED facts alone: no
    assert ladder({"VALIDATED": ["internal"]}) == "REPLICATED"
    assert LEVELS == ("NONE", "REPRODUCIBLE", "REPLICATED", "VALIDATED")


def test_classify_target():
    assert classify_target("openml_split", False, True, DatasetRole.EXTERNAL) == "retrospective"
    assert classify_target("openml_split", True, True, DatasetRole.FUTURE) == "retrospective"
    assert classify_target("minted_heldout", True, True, DatasetRole.FUTURE) == "minted"
    assert classify_target(None, True, False, DatasetRole.FUTURE) == "sealed"  # minted entry but registered before
    assert classify_target(None, False, True, DatasetRole.FUTURE) == "future"
    assert classify_target(None, False, False, DatasetRole.FUTURE) == "sealed"
    assert classify_target(None, False, False, DatasetRole.EXTERNAL) == "sealed"
    assert classify_target("research_task", False, False, DatasetRole.TEST) == "internal"
    assert classify_target(None, False, True, DatasetRole.SEALED) == "internal"


def _certify(w: World, target: str, *, delta: float = 0.2, **kw) -> str:
    good, comp = w.artifact("good"), w.artifact("baseline", "baseline")
    claim = w.claim(good, comp, delta=delta, targets=(target,), n_boot=1000, **kw)
    w.kernel.preregister(claim)
    return w.kernel.certify(claim, w.evaluate_all(claim)).cert_hash()


def test_rejected_certificate_is_level_none_and_names_the_failure(tmp_path):
    w = World(tmp_path / "w", seed=41, n_train=120, n_test=120)
    try:
        w.register_target("t", DatasetRole.TEST, n=120)
        h = _certify(w, "t", delta=0.6, seeds=(0, 1, 2, 3, 4), alpha=0.01)  # the honest no-effect claim: REJECTED
        res = level(w.store, h)
        assert res.label == "REJECTED" and res.level == "NONE" and res.badge == "NONE"
        assert any("REJECTED" in m for m in res.missing["REPRODUCIBLE"])
    finally:
        w.close()


def test_reproducible_needs_a_recorded_bundle(tmp_path):
    w = World(tmp_path / "w", seed=42, n_train=120, n_test=120)
    try:
        w.register_target("t", DatasetRole.TEST, n=120)
        h = _certify(w, "t")
        before = level(w.store, h)
        assert before.label == "CERTIFIED-INTERNAL", before
        assert before.level == "NONE" and any("Bundle" in m for m in before.missing["REPRODUCIBLE"])
        ScopedKernel(w.store).bundle(h, tmp_path / "b.zip")
        after = level(w.store, h)
        assert after.level == "REPRODUCIBLE" and after.retrospective is False
        assert any("CERTIFIED-INTERNAL" in m for m in after.missing["VALIDATED"])
        assert after.facts["targets"]["t"]["freshness"] == "internal"
    finally:
        w.close()


def test_a_public_dataset_target_is_retrospective_and_never_validated(tmp_path):
    w = World(tmp_path / "w", seed=43, n_train=120, n_test=120)
    try:
        w.register_target("pub", DatasetRole.EXTERNAL, n=120)
        w.kernel.approve_role("pub", "test-human")
        # what kerneld/ingest.openml_split records for a Claim Audit split (a fixed public dataset)
        w.store.append_record("kerneld_ingest", {"key": "k", "run_id": "r", "kind": "openml_split", "subject": "pub",
                                                 "datasets": ["pub"], "result": {}, "at": 0})
        h = _certify(w, "pub")
        ScopedKernel(w.store).bundle(h, tmp_path / "b.zip")
        res = level(w.store, h)
        assert res.label == "CERTIFIED-VALIDATED", res
        assert res.retrospective is True and res.level == "REPRODUCIBLE"
        assert res.badge == "REPRODUCIBLE (RETROSPECTIVE)"
        assert any("RETROSPECTIVE" in m for m in res.missing["VALIDATED"])
    finally:
        w.close()


def test_without_a_replica_a_validated_label_stops_at_reproducible(tmp_path):
    """Cumulative levels: CERTIFIED-VALIDATED on sealed external data is not VALIDATED without REPLICATED; the
    sealed-data assumption is listed as untested."""
    w = World(tmp_path / "w", seed=44, n_train=120, n_test=120)
    try:
        w.register_target("ext", DatasetRole.EXTERNAL, n=120)
        w.kernel.approve_role("ext", "test-human")
        h = _certify(w, "ext")
        ScopedKernel(w.store).bundle(h, tmp_path / "b.zip")
        res = level(w.store, h)
        assert res.label == "CERTIFIED-VALIDATED" and res.level == "REPRODUCIBLE"
        assert "REPLICATED" in res.missing and "VALIDATED" not in res.missing  # only the replica is missing
        assert res.facts["targets"]["ext"]["freshness"] == "sealed"
        assert res.facts.get("untested_assumptions")
    finally:
        w.close()


@pytest.mark.parametrize("op", ["frozen", "handed_off"])
def test_the_lab_trace_refuses_a_level_outside_a_verdict_release(op):
    from custom_harness.lab.trace import guard_problems
    payload = {"op": op, "promotion_id": "p1", "released_level": "VALIDATED"}
    assert guard_problems("promote", payload, source="manager")
    ok = {"op": "verdict_released", "promotion_id": "p1", "released_label": "CERTIFIED-VALIDATED",
          "released_level": "VALIDATED"}
    assert guard_problems("promote", ok, source="manager") == []
    assert guard_problems("promote", ok, source="agent-reported")  # only the manager writes a released verdict
