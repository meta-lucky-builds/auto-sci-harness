"""Verification badges (GOAL §17.1 as decided 2026-10-23; court/levels.py, int18 stage-a): SEPARATE badges
REPRODUCIBLE / REPLICATED / VALIDATED / RE-VALIDATED, computed by CODE from the certificate, the ledger, the vault and
the Court case records; public datasets RETROSPECTIVE (never VALIDATED or RE-VALIDATED).

Fail-first reasoning: on int18 before Stage A there is no custom_harness.court (ImportError for every test). Against
the first Stage A build (a strict ladder, commit 0464555) these fail because:
  - VALIDATED without REPLICATED: the ladder stopped at REPRODUCIBLE; now VALIDATED is the fresh-data test alone;
  - VALIDATED without a bundle: the ladder needed REPRODUCIBLE first;
  - the result had `level`, no `held` / `badges` / `release_value`, and no RE-VALIDATED;
  - claim_core / revalidation_facts did not exist.
Each test names the rule it pins:
  - classify_target: a Claim Audit OpenML split is retrospective whatever its role or timing;
  - a REJECTED certificate holds no badge and names the failing checks;
  - REPRODUCIBLE needs a recorded bundle; it does not need VALIDATED, and VALIDATED does not need it;
  - a CERTIFIED-VALIDATED claim on a public-dataset target is RETROSPECTIVE and never VALIDATED;
  - VALIDATED holds on sealed external data without any replica, and lists the sealed-data assumption;
  - the Lab release value has at most 16 values and carries no family, reason or number;
  - claim_core ignores only where the claim is tested (targets, case id, Lab link, beacon round);
  - the lab trace refuses a released badge set outside promote/verdict_released.
Built, NOT test-verified (owner rule: no test runs this round)."""

from __future__ import annotations

import itertools

import pytest

from custom_harness.court.levels import BADGES, LevelResult, claim_core, classify_target, level
from custom_harness.kernel import DatasetRole
from custom_harness.kerneld.scope import ScopedKernel
from kernel_world import World


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


def test_a_rejected_certificate_holds_no_badge_and_names_the_failure(tmp_path):
    w = World(tmp_path / "w", seed=41, n_train=120, n_test=120)
    try:
        w.register_target("t", DatasetRole.TEST, n=120)
        h = _certify(w, "t", delta=0.6, seeds=(0, 1, 2, 3, 4), alpha=0.01)  # the honest no-effect claim: REJECTED
        res = level(w.store, h)
        assert res.label == "REJECTED" and res.badges == [] and res.badge == "NONE"
        assert not any(res.held.values())
        assert any("REJECTED" in m for m in res.missing["REPRODUCIBLE"])
    finally:
        w.close()


def test_reproducible_needs_a_recorded_bundle_and_nothing_else(tmp_path):
    w = World(tmp_path / "w", seed=42, n_train=120, n_test=120)
    try:
        w.register_target("t", DatasetRole.TEST, n=120)
        h = _certify(w, "t")
        before = level(w.store, h)
        assert before.label == "CERTIFIED-INTERNAL", before
        assert not before.held["REPRODUCIBLE"] and any("Bundle" in m for m in before.missing["REPRODUCIBLE"])
        ScopedKernel(w.store).bundle(h, tmp_path / "b.zip")
        after = level(w.store, h)
        assert after.held == {"REPRODUCIBLE": True, "REPLICATED": False, "VALIDATED": False, "RE-VALIDATED": False}
        assert after.badges == ["REPRODUCIBLE (one machine; across machines not checked)"]
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
        res = level(w.store, h)
        assert res.label == "CERTIFIED-VALIDATED", res
        assert res.retrospective is True and not res.held["VALIDATED"] and not res.held["RE-VALIDATED"]
        assert res.badge.endswith("(RETROSPECTIVE)")
        assert any("RETROSPECTIVE" in m for m in res.missing["VALIDATED"])
    finally:
        w.close()


def test_validated_is_the_fresh_data_test_alone(tmp_path):
    """No bundle, no replica: VALIDATED still holds on human-approved sealed external data (the sealed-data
    assumption is listed as untested); REPRODUCIBLE and REPLICATED do not."""
    w = World(tmp_path / "w", seed=44, n_train=120, n_test=120)
    try:
        w.register_target("ext", DatasetRole.EXTERNAL, n=120)
        w.kernel.approve_role("ext", "test-human")
        h = _certify(w, "ext")
        res = level(w.store, h)
        assert res.label == "CERTIFIED-VALIDATED"
        assert res.held == {"REPRODUCIBLE": False, "REPLICATED": False, "VALIDATED": True, "RE-VALIDATED": False}
        assert res.badge == "VALIDATED" and res.revalidation == "none"
        assert res.facts["targets"]["ext"]["freshness"] == "sealed" and res.facts.get("untested_assumptions")
        assert res.release_value() == "VALIDATED"
    finally:
        w.close()


def test_the_release_value_is_a_closed_alphabet_of_at_most_16_values():
    values = set()
    for r, p, v in itertools.product((False, True), repeat=3):
        for rv in (["not applicable"] if not v else ["none", "declared", "passed", "failed", "invalid"]):
            x = LevelResult("h", "c", "L")
            x.held = {"REPRODUCIBLE": r, "REPLICATED": p, "VALIDATED": v, "RE-VALIDATED": v and rv == "passed"}
            x.revalidation = rv
            values.add(x.release_value())
    assert len(values) <= 16 and "NONE" in values
    assert all(set(v) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ+-") for v in values)
    assert BADGES == ("REPRODUCIBLE", "REPLICATED", "VALIDATED", "RE-VALIDATED")


def test_claim_core_ignores_only_where_the_claim_is_tested():
    def claim(target: str, case: str, round_: str, subject_hash: str = "s" * 64) -> dict:
        return {"kind": "improvement", "evidence_class": "data", "generalization_targets": [target],
                "development": [f"{target}-train"], "statement": f"... {target} ...",
                "subject": {"artifact_id": f"court-{case}-method", "hash": subject_hash},
                "comparator": {"artifact_id": f"court-{case}-baseline", "hash": "b" * 64},
                "effect": {"min_effect": 0.05}, "analysis_plan": {"seeds": [0, 1, 2]},
                "scope": {"datasets": [target], "conditions": {
                    "audit_id": case, "paper_doi": f"lab:s:p:{case}",
                    "protocol": f"chh-court-minted/1 family=rings n_train=600 n_test=4500 beacon_round={round_}: x"}}}
    assert claim_core(claim("lab-mint-a", "a", "R1")) == claim_core(claim("lab-mint-b", "b", "R2"))
    assert claim_core(claim("lab-mint-a", "a", "R1")) != claim_core(claim("lab-mint-b", "b", "R2", "t" * 64))


@pytest.mark.parametrize("op", ["frozen", "handed_off"])
def test_the_lab_trace_refuses_badges_outside_a_verdict_release(op):
    from custom_harness.lab.trace import guard_problems
    payload = {"op": op, "promotion_id": "p1", "released_level": "VALIDATED"}
    assert guard_problems("promote", payload, source="manager")
    ok = {"op": "verdict_released", "promotion_id": "p1", "released_label": "CERTIFIED-VALIDATED",
          "released_level": "REPRODUCIBLE+VALIDATED+RE-VALIDATED"}
    assert guard_problems("promote", ok, source="manager") == []
    assert guard_problems("promote", ok, source="agent-reported")  # only the manager writes a released verdict
