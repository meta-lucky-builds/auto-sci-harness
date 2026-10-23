"""The minted-data Court route (GOAL §16.2, §17; court/minted.py + kerneld/mint.py + kerneld/ingest.minted_heldout;
int18 stage-a).

Fail-first reasoning: on int18 before the Stage A commit there is no custom_harness.court, no kerneld/mint.py and no
`minted_heldout` ingest kind (ImportError / AttributeError for every test). Each test pins one rule:
  - the minted bytes are a pure function of (family, claim hash, beacon, sizes): another beacon -> other labels;
  - only generator families can be minted (a public dataset is refused with the retrospective route named);
  - kerneld mints ONLY for a pre-registered claim whose target it is, ONLY after the pre-registration is anchored,
    ONLY with the protocol the claim froze, ONCE per claim (another beacon = beacon shopping), and registers the
    target as FUTURE with private labels plus a `mint` ledger entry (K1's delta floor then applies);
  - a Court case is pre-registered and anchored before anything is minted; refusals for a non-generator family, a
    min_effect below the floor and a metric that is not the family's;
  - host code of the route never builds or reads the minted labels (static);
  - without public dev pairs the held-out size is the UNCALIBRATED default (4,500) and nothing is denied.
Built, NOT test-verified (owner rule: no test runs this round)."""

from __future__ import annotations

import ast
import csv
import io
import json
from datetime import date
from pathlib import Path

import pytest

from custom_harness.audit.spec import Budget, ClaimPart
from custom_harness.court import minted as C
from custom_harness.kernel import DatasetRole
from custom_harness.kernel.ledger import write_anchor
from custom_harness.kerneld import ingest as I
from custom_harness.kerneld.mint import MintSpecError, minted_files, minted_protocol
from kernel_world import World

ROOT = Path(__file__).resolve().parents[1]
H = "c" * 64
PART = ClaimPart(metric="accuracy", min_effect=0.05, probability=0.7, seeds=(0, 1, 2), n_boot=1000,
                 resolve_by=date(2030, 1, 1))
METHOD = b"def build_model(seed):\n    return None  # method\n"
BASELINE = b"def build_model(seed):\n    return None  # baseline\n"


def test_minted_files_are_deterministic_and_bound_to_the_beacon():
    a = minted_files("rings", H, "r1:" + "x" * 16, 50, 200)
    assert a == minted_files("rings", H, "r1:" + "x" * 16, 50, 200)
    assert a.test_y != minted_files("rings", H, "r1:" + "y" * 16, 50, 200).test_y
    assert a.test_y != minted_files("rings", "d" * 64, "r1:" + "x" * 16, 50, 200).test_y
    assert a.train.splitlines()[0].endswith(b",label") and a.test_y.splitlines()[0] == b"label"
    xs = list(csv.reader(io.StringIO(a.test_x.decode())))
    assert "label" not in xs[0] and len(xs) - 1 == a.n_test
    rec = json.loads(a.record)
    assert rec["frozen_hash"] == H and rec["minted"] is True and rec["retrospective_target"] is False


def test_only_generator_families_can_be_minted():
    with pytest.raises(MintSpecError, match="retrospective"):
        minted_protocol("openml-31", 10, 10, "r1")
    with pytest.raises(MintSpecError):
        minted_protocol("rings", 10, 10, "bad round with spaces")


def _claim(family: str = "rings", n_train: int = 50, n_test: int = 200, round_: str = "r1"):
    return C.case_claim(case_id="case1", link="lab:s1:p1", title="t", sentence="a sentence of ten chars",
                        part=PART, budget=Budget(timeout_s=60), method_code=METHOD, baseline_code=BASELINE,
                        family=family, n_train=n_train, n_test=n_test, beacon_round=round_)


def test_kerneld_mints_only_after_the_frozen_claim_is_anchored_and_only_once(tmp_path):
    w = World(tmp_path / "w", seed=51, n_train=60, n_test=60)
    try:
        claim = _claim()
        ch, target = claim.claim_hash(), claim.generalization_targets[0]
        anchor = tmp_path / "anchors" / "a.jsonl"
        args = dict(family="rings", n_train=50, n_test=200, beacon_round="r1", beacon_value="v" * 20,
                    anchor_file=str(anchor))
        with pytest.raises(I.IngestRefused) as e:
            I.minted_heldout(w.vault, "run1", target, ch, **args)
        assert e.value.code == "not_registered"
        w.kernel.preregister(claim)
        with pytest.raises(I.IngestRefused) as e:  # pre-registered but not anchored: nothing minted
            I.minted_heldout(w.vault, "run1", target, ch, **args)
        assert e.value.code == "not_anchored" and target not in w.vault.datasets()
        write_anchor(w.kernel.ledger, anchor, w.store.root)
        with pytest.raises(I.IngestRefused) as e:  # another protocol than the one the claim froze
            I.minted_heldout(w.vault, "run1", target, ch, **{**args, "n_test": 201})
        assert e.value.code == "token_scope"
        with pytest.raises(I.IngestRefused) as e:  # not this claim's target
            I.minted_heldout(w.vault, "run1", "test", ch, **args)
        assert e.value.code == "token_scope"
        res = I.minted_heldout(w.vault, "run1", target, ch, **args)
        ds = w.vault.datasets()
        assert ds[target].role == DatasetRole.FUTURE and ds[f"{target}-train"].role == DatasetRole.DEV
        assert {n for n, f in ds[target].files.items() if f.private} == {"test_y.csv", "mint.json"}
        mints = w.kernel.ledger.entries("mint")
        assert [(m.body["dataset_id"], m.body["frozen_hash"]) for m in mints] == [(target, ch)]
        assert res["n_test"] >= 1 and res["mint_seq"] == mints[0].seq
        assert I.minted_heldout(w.vault, "run1", target, ch, **args) == res  # idempotent: same result
        with pytest.raises(I.IngestRefused) as e:  # beacon shopping
            I.minted_heldout(w.vault, "run1", target, ch, **{**args, "beacon_value": "w" * 20})
        assert e.value.code == "already_registered"
    finally:
        w.close()


def test_a_case_is_preregistered_and_anchored_before_any_mint(tmp_path):
    w = World(tmp_path / "w", seed=52, n_train=60, n_test=60)
    try:
        anchor = tmp_path / "anchors" / "court.jsonl"
        body = C.register_case(w.store, role="promotion", promotion_id="p1", session_id="s1", title="t",
                               sentence="a sentence of ten chars", part=PART, budget=Budget(timeout_s=60),
                               method=METHOD, baseline=BASELINE, lock=b"x==1\n", family="rings", beacon_round="r1",
                               sizing={"n_test": 200}, n_train=50, anchor_file=anchor)
        assert w.vault.registered(body["claim_hash"]) and body["target"] not in w.vault.datasets()
        assert anchor.is_file() and body["anchor"]["length"] >= body["prereg_seq"]
        assert C.case(w.store, body["case_id"])["claim_hash"] == body["claim_hash"]
        with pytest.raises(C.CourtRefused):  # one pre-registration per case
            C.register_case(w.store, role="promotion", promotion_id="p1", session_id="s1", title="t",
                            sentence="a sentence of ten chars", part=PART, budget=Budget(timeout_s=60),
                            method=METHOD, baseline=BASELINE, lock=None, family="rings", beacon_round="r1",
                            sizing={"n_test": 200}, n_train=50, anchor_file=anchor)
    finally:
        w.close()


@pytest.mark.parametrize("family,part,match", [
    ("openml-31", PART, "retrospective"),
    ("rings", PART.model_copy(update={"min_effect": 0.001}), "floor"),
    ("friedman", PART, "metric"),
])
def test_register_refusals(tmp_path, family, part, match):
    w = World(tmp_path / "w", seed=53, n_train=60, n_test=60)
    try:
        with pytest.raises(C.CourtRefused, match=match):
            C.register_case(w.store, role="promotion", promotion_id="p1", session_id="s1", title="t",
                            sentence="a sentence of ten chars", part=part, budget=Budget(timeout_s=60),
                            method=METHOD, baseline=BASELINE, lock=None, family=family, beacon_round="r1",
                            sizing={"n_test": 200}, n_train=50, anchor_file=tmp_path / "a.jsonl")
        assert w.kernel.ledger.entries("prereg") == []  # refused before anything was registered
    finally:
        w.close()


def test_host_code_of_the_route_never_builds_or_reads_minted_labels():
    for p in sorted((ROOT / "src" / "custom_harness" / "court").glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "minted_files" not in names, p
        reads = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "read"]
        assert reads == [], f"{p.name}: a private vault read in host code"


def test_without_dev_pairs_the_default_size_is_used():
    s = C.size_held_out(PART, dev_pairs=None)
    assert s["n_test"] == 4500 and "UNCALIBRATED" in s["preflight"]


# ===================================================================== RE-VALIDATED (owner decision 2026-10-23)
def _promoted_case(w: World, tmp_path: Path, round_: str = "r1") -> dict:
    """A promotion case as `chh court register` makes it, with the lab_promotion `frozen` record it reads."""
    from custom_harness.lab.promote import RECORD_KIND as PROMOTION_KIND
    from custom_harness.lab.promote import PromoteRequest
    req = PromoteRequest(format="chh-lab-promote/1", title="t", sentence="a sentence of ten chars", method="m.py",
                         baseline="b.py", lock="requirements.lock", claim=PART, budget=Budget(timeout_s=60))
    lock = w.store.blobs.put(b"x==1\n")
    w.store.append_record(PROMOTION_KIND, {"promotion_id": "p1", "session_id": "s1", "event": "frozen",
                                           "request": req.model_dump(mode="json"),
                                           "frozen": {"lock": {"blob": lock}}})
    return C.register_case(w.store, role="promotion", promotion_id="p1", session_id="s1", title=req.title,
                           sentence=req.sentence, part=PART, budget=req.budget, method=METHOD, baseline=BASELINE,
                           lock=b"x==1\n", family="rings", beacon_round=round_, sizing={"n_test": 200}, n_train=50,
                           anchor_file=tmp_path / "anchors" / "court.jsonl")


def test_a_revalidation_is_the_same_claim_on_a_second_target_declared_up_front(tmp_path):
    """FAILS against the first Stage A build: no declare_revalidation, no `revalidation` role."""
    from custom_harness.court.levels import claim_core
    w = World(tmp_path / "w", seed=54, n_train=60, n_test=60)
    try:
        first = _promoted_case(w, tmp_path)
        second = C.declare_revalidation(w.store, first["case_id"], "r2", anchor_file=tmp_path / "anchors" / "c.jsonl")
        assert second["role"] == "revalidation" and second["of_case"] == first["case_id"]
        assert second["target"] != first["target"] and second["beacon_round"] == "r2"
        claims = {e.body["claim_hash"]: e.body["claim"] for e in w.kernel.ledger.entries("prereg")}
        assert claim_core(claims[first["claim_hash"]]) == claim_core(claims[second["claim_hash"]])
        assert claims[first["claim_hash"]]["subject"]["hash"] == claims[second["claim_hash"]]["subject"]["hash"]
        with pytest.raises(C.CourtRefused, match="one per case"):
            C.declare_revalidation(w.store, first["case_id"], "r3")
    finally:
        w.close()


def test_a_revalidation_needs_another_round_and_comes_before_the_first_verdict(tmp_path):
    w = World(tmp_path / "w", seed=55, n_train=60, n_test=60)
    try:
        first = _promoted_case(w, tmp_path)
        with pytest.raises(C.CourtRefused, match="different beacon round"):
            C.declare_revalidation(w.store, first["case_id"], "r1")
        w.store.append_record(C.RECORD_KIND, {"case_id": first["case_id"], "event": "outcome", "labels_read": True,
                                              "claim_hash": first["claim_hash"], "verdict": "COULD NOT RUN"})
        with pytest.raises(C.CourtRefused, match="too late"):
            C.declare_revalidation(w.store, first["case_id"], "r2")
    finally:
        w.close()


def test_the_two_mints_are_independent(tmp_path):
    """Both cases minted by kerneld's ingest under their own claim hash and round: different labels, no shared row."""
    from custom_harness.kernel.checks import related_targets
    w = World(tmp_path / "w", seed=56, n_train=60, n_test=60)
    try:
        first = _promoted_case(w, tmp_path)
        second = C.declare_revalidation(w.store, first["case_id"], "r2", anchor_file=tmp_path / "anchors" / "c.jsonl")
        for c, value in ((first, "v" * 20), (second, "u" * 20)):
            I.minted_heldout(w.vault, "run", c["target"], c["claim_hash"], family="rings", n_train=50, n_test=200,
                             beacon_round=c["beacon_round"], beacon_value=value, anchor_file=c["anchor_file"])
        d = w.vault.datasets()
        assert d[first["target"]].files["test_y.csv"].blob != d[second["target"]].files["test_y.csv"].blob
        assert second["target"] not in related_targets(w.vault, first["target"])
    finally:
        w.close()


def test_revalidation_states_are_shown_never_hidden(tmp_path):
    from types import SimpleNamespace

    from custom_harness.court.levels import revalidation_facts
    w = World(tmp_path / "w", seed=57, n_train=60, n_test=60)
    try:
        first = _promoted_case(w, tmp_path)
        cert = SimpleNamespace(claim_hash=first["claim_hash"], claim={"generalization_targets": [first["target"]]})
        assert revalidation_facts(w.store, cert)["state"] == "none"
        second = C.declare_revalidation(w.store, first["case_id"], "r2", anchor_file=tmp_path / "anchors" / "c.jsonl")
        assert revalidation_facts(w.store, cert)["state"] == "declared"
        w.store.append_record(C.RECORD_KIND, {"case_id": second["case_id"], "event": "outcome", "labels_read": True,
                                              "cert_hash": None, "claim_hash": second["claim_hash"],
                                              "verdict": "COULD NOT RUN"})
        assert revalidation_facts(w.store, cert)["state"] == "failed"  # labels read, no certificate: a failure
    finally:
        w.close()
