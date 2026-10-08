"""Scorecard framework (GOAL §2, §11 M8, invariants 8 and 12).

A fixture campaign (a real Kernel certificate with its bundle, a research-style run whose final state carries the
report, checklist, citation audit and leak audit, re-runs on a pinned fixture benchmark, an as-reported number, an
ablation, and museum / findings / salts inputs) must produce a scorecard whose every number resolves to an artifact
and reproduces with one command; a planted claim without evidence must make axis F fail."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from custom_harness.kernel.bundle import build_bundle
from custom_harness.kernel.checklist import fill
from custom_harness.records import Claim, Cost, Evidence, EvidenceKind, Run, StepKind, TraceStep
from custom_harness.scorecard import (
    ScorecardError,
    build_scorecard,
    check_scorecard,
    content_hash,
    load_capabilities,
    load_registry,
    paired_compare,
    record_as_reported,
    run_ablation,
    run_system,
)
from custom_harness.scorecard.axes import all_sources, numbers_without_sources
from custom_harness.scorecard.refs import resolve
from custom_harness.tiers import Tier
from kernel_world import World

N_ITEMS = 40


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")


@pytest.fixture(autouse=True)
def _fixture_switch(monkeypatch):
    """The fixture capability `skills` gets a test-only switch `skills` in the closed ablation registry (the registry
    is closed: an unregistered switch is never counted, tests/test_ablation_scorecard.py)."""
    from types import MappingProxyType

    import custom_harness.ablation as A
    import custom_harness.scorecard.registry as G
    import custom_harness.scorecard.runner as R
    reg = MappingProxyType({**A.SWITCHES, "skills": A.Switch("skills", "skills", "fixture", "tests/x.py::f")})
    for mod in (A, R, G):
        monkeypatch.setattr(mod, "SWITCHES", reg)


@dataclass
class FixtureSystem:
    name: str
    version: str
    passes: int  # items 0..passes-1 pass
    lost_without: dict[str, int] | None = None  # switch -> items lost when it is disabled

    def run(self, items, disabled):
        lost = sum((self.lost_without or {}).get(s, 0) for s in disabled)
        return {it["id"]: 1.0 if int(it["id"][1:]) < self.passes - lost else 0.0 for it in items}


def _benchmark(root: Path, axis: str = "C") -> Path:
    d = root / "bench" / "toy"
    d.mkdir(parents=True)
    (d / "items.jsonl").write_text("".join(json.dumps({"id": f"i{k:02d}", "x": k}) + "\n" for k in range(N_ITEMS)),
                                   encoding="utf-8", newline="")
    reg = root / "bench" / "registry.yaml"
    reg.write_text(yaml.safe_dump({"benchmarks": [
        {"name": "toy", "version": "1", "axis": axis, "licence": "CC0-1.0 (fixture)", "content_hash": content_hash(d),
         "path": "toy"}]}), encoding="utf-8", newline="")
    return reg


def _caps(root: Path) -> Path:
    p = root / "caps.yaml"
    p.write_text(yaml.safe_dump({"capabilities": [
        {"id": "skills", "source": "EvoMaster", "capability": "skills", "home": "L5", "ablation": "skills"},
        {"id": "router", "source": "MoB", "capability": "router", "home": "L6.9", "ablation": None}]}),
        encoding="utf-8", newline="")
    return p


def _inputs(root: Path) -> dict[str, Path]:
    museum = root / "museum.log"
    museum.write_text("KILLED    K-1: 1 failed\nKILLED    K-2a: 2 failed\nmuseum: 2/2 KILLED\n", encoding="utf-8")
    findings = root / "findings.yaml"
    findings.write_text(yaml.safe_dump({"findings": [{"id": "K-1", "status": "fixed"},
                                                     {"id": "K-2a", "status": "fixed"}]}), encoding="utf-8")
    salts = root / "salts.json"
    salts.write_text(json.dumps({"campaign_hash": "c" * 64,
                                 "false_certification": {"k": 0, "n": 10, "rate": 0.0, "ci": [0.0, 0.26]},
                                 "coverage": {"k": 9, "n": 10, "rate": 0.9, "ci": [0.55, 0.997]},
                                 "inflated": {"k": 0, "n": 10, "rate": 0.0, "ci": [0.0, 0.26]},
                                 "short": {"k": 1, "n": 10, "rate": 0.1, "ci": [0.003, 0.45]},
                                 "pooled_inflation_z": -0.4, "pooled_inflation_flag": False, "missing": []}),
                     encoding="utf-8")
    return {"museum_log": museum, "findings": findings, "salts_audit": salts}


def _report_run(w: World, cert, bundle_hash: str | None, *, citation_status: str = "verified",
                checklist: bool = True) -> str:
    store = w.store
    cl = fill(cert, run_costs={}, models=["fixture/none (mock)"], bundle_hash=bundle_hash,
              selection_policy="fixture: single candidate", human_in_the_loop="none (fixture)",
              novelty="not performed (fixture)") if checklist else None
    if cl is not None:
        cl["traces"] = "fixture trace"
        cl["cost"] = "fixture cost"
    state = {"report": "# fixture report", "certificate": {"hash": cert.cert_hash(), "label": cert.label.value,
                                                           "bundle_hash": bundle_hash},
             "checklist": cl, "citations": [{"key": "c1", "id": "10.1/x", "title": "T", "year": 2024}],
             "audit": {"I5": True, "citations": {"results": [{"key": "c1", "status": citation_status}]}}}
    run = Run(harness_id="fixture", harness_hash="h", task_id="toy")
    store.add_run(run)
    now = time.time()
    store.add_step(TraceStep(run_id=run.id, index=0, node_id="report", kind=StepKind.MODULE, input={},
                             output={"report": "# fixture report"}, cost=Cost(usd=0.5, prompt_tokens=100),
                             started_at=now, ended_at=now, checkpoint_ref=store.blobs.put_json({"state": state})))
    return run.id


def _campaign(tmp_path: Path, **kw):
    w = World(tmp_path / "world", seed=11)
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    assert cert.label.value == "CERTIFIED-INTERNAL"
    bh = build_bundle(w.kernel, cert.cert_hash(), tmp_path / "bundles" / "c.zip")
    _report_run(w, cert, bh, **kw)
    reg_path = _benchmark(tmp_path)
    reg = load_registry(reg_path)
    bench = reg.benchmarks[0]
    ours = FixtureSystem("ours", "v1", passes=36, lost_without={"skills": 10})
    run_system(w.store, reg, bench, ours, "ours")
    run_system(w.store, reg, bench, FixtureSystem("base-a", "a1", passes=20), "baseline")
    record_as_reported(w.store, bench, "base-a", 0.95, "fixture paper, Table 2")  # re-run exists: not used
    run_ablation(w.store, reg, bench, ours, "skills")
    inputs = {**_inputs(tmp_path), "registry": reg_path, "capabilities": _caps(tmp_path)}
    return w, cert, inputs


def test_fixture_campaign_every_number_resolves_and_reproduces(tmp_path):
    w, _cert, inputs = _campaign(tmp_path)
    out = tmp_path / "card"
    card = build_scorecard(w.store, out, **inputs)
    assert numbers_without_sources(card) == []
    srcs = all_sources(card)
    kinds = {s.split(":")[0] for s in srcs}
    assert {"ledger", "blob", "record", "run", "input"} <= kinds, kinds  # every ref kind is exercised
    assert all(resolve(s, w.store, out / "inputs") is None for s in srcs)
    rep = check_scorecard(out, w.store)
    assert rep["reproduced"], rep["problems"]
    f = card["axes"]["F"]
    assert f["claims_without_evidence"]["value"] == 0 and f["claims_without_evidence"]["status"] == "pass"
    assert f["unverifiable_citations"]["value"] == 0
    assert f["reports_with_checklist_and_bundle"]["value"] == 100.0 and f["status"] == "pass"
    g = card["axes"]["G"]["cost_per_certified_claim"]
    assert g["certified_claims"] == 1 and g["value"]["usd"] == pytest.approx(0.5)
    h = card["axes"]["H"]
    assert h["planted_exploit_catch_rate"]["value"] == 1.0 and h["leak_audit_pass_rate"]["value"] == 1.0
    assert h["salts"]["status"] == "pass" and h["red_team_open_rate"]["value"] == 0.0
    c = card["axes"]["C"]["benchmarks"][0]
    assert c["basis"] == "re-run" and c["best_baseline"] == "base-a" and c["verdict"] == "win"
    assert c["as_reported"][0]["label"] == "as reported (not re-run)" and c["as_reported"][0]["used"].startswith("no")
    cov = card["coverage"]
    assert cov["gaps"] == ["router"]
    assert cov["rows"][0]["results"][0]["contribution"] == "shown"
    # honesty: axes without measurements, G without a baseline and the coverage gap are all listed, not hidden
    assert card["overall"].startswith("NOT best")
    assert any("coverage" in x for x in card["losses"])
    assert any(x.startswith("axis A: not measured") for x in card["not_shown"])
    assert any(x.startswith("axis G") for x in card["not_shown"])
    assert "## Losses and failures" in (out / "scorecard.md").read_text(encoding="utf-8")
    w.close()


def _plant_claim_without_evidence(store, text: str) -> Claim:
    """A claim row with no evidence. `add_claim` refuses one since 2026-10-08 (d75497d), so the row is written the
    way an older store or a tampered database would hold it: axis F must still catch it (defence in depth)."""
    planted = Claim(text=text, evidence_ids=())
    with pytest.raises(ValueError, match="no evidence"):
        store.add_claim(planted)  # the store's own guard refuses it
    store._exec("INSERT INTO claims VALUES (?, ?)", (planted.id, planted.model_dump_json()))
    return planted


def test_planted_claim_without_evidence_makes_axis_f_fail(tmp_path):
    w, _cert, inputs = _campaign(tmp_path)
    before = build_scorecard(w.store, tmp_path / "before", **inputs)
    assert before["axes"]["F"]["status"] == "pass"
    planted = _plant_claim_without_evidence(w.store, "our method is SOTA")
    assert w.store.get_claim(planted.id).evidence_ids == ()
    card = build_scorecard(w.store, tmp_path / "after", **inputs)
    m = card["axes"]["F"]["claims_without_evidence"]
    assert m["value"] == 1 and m["status"] == "fail"
    assert [o for o in m["offenders"] if o.startswith(f"claim:{planted.id}:")]
    assert card["axes"]["F"]["status"] == "fail"
    assert any(x.startswith("axis F") for x in card["losses"])
    assert check_scorecard(tmp_path / "after", w.store)["reproduced"]  # the failing scorecard still reproduces
    w.close()


def test_claim_citing_missing_evidence_blob_fails_axis_f(tmp_path):
    w, _cert, inputs = _campaign(tmp_path)
    ev = Evidence(kind=EvidenceKind.LOG, tier=Tier.T6_NONE, verifier_id="x", produced_by_run=None,
                  payload_ref=w.store.blobs.put(b"payload"))
    w.store.add_evidence(ev)
    w.store.add_claim(Claim(text="backed", evidence_ids=(ev.id,)))
    blob = w.store.blobs._path(ev.payload_ref)
    blob.unlink()  # the evidence row survives, its payload does not
    card = build_scorecard(w.store, tmp_path / "card", **inputs)
    assert card["axes"]["F"]["claims_without_evidence"]["value"] == 1
    w.close()


def test_unverifiable_citation_and_missing_checklist_fail_axis_f(tmp_path):
    w, cert, inputs = _campaign(tmp_path)
    _report_run(w, cert, None, citation_status="unverified", checklist=False)
    card = build_scorecard(w.store, tmp_path / "card", **inputs)
    f = card["axes"]["F"]
    assert f["unverifiable_citations"]["value"] == 1 and f["unverifiable_citations"]["status"] == "fail"
    r = f["reports_with_checklist_and_bundle"]
    assert r["numerator"] == 1 and r["denominator"] == 2 and r["value"] == 50.0 and r["status"] == "fail"
    w.close()


def test_check_fails_on_tampered_bundle_or_changed_store(tmp_path):
    w, _cert, inputs = _campaign(tmp_path)
    out = tmp_path / "card"
    build_scorecard(w.store, out, **inputs)
    card = json.loads((out / "scorecard.json").read_text(encoding="utf-8"))
    card["axes"]["F"]["claims_without_evidence"]["value"] = -1
    (out / "scorecard.json").write_text(json.dumps(card), encoding="utf-8")
    rep = check_scorecard(out, w.store)
    assert not rep["reproduced"] and any("recomputation" in p for p in rep["problems"])
    out2 = tmp_path / "card2"
    build_scorecard(w.store, out2, **inputs)
    _plant_claim_without_evidence(w.store, "added later")
    rep2 = check_scorecard(out2, w.store)
    assert not rep2["reproduced"] and any("store_claims" in p for p in rep2["problems"])
    (out2 / "inputs" / next(iter((out2 / "inputs").iterdir())).name).write_bytes(b"tampered")
    assert any("input copy corrupted" in p for p in check_scorecard(out2, w.store)["problems"])
    assert not check_scorecard(out, None)["reproduced"]  # without the store nothing is reproduced
    w.close()


def test_losing_axis_is_reported_and_as_reported_stands_in_only_without_rerun(tmp_path):
    from custom_harness.store import EvidenceStore
    store = EvidenceStore(tmp_path / "store")
    reg = load_registry(_benchmark(tmp_path, axis="A"))
    bench = reg.benchmarks[0]
    run_system(store, reg, bench, FixtureSystem("ours", "v1", passes=10), "ours")
    record_as_reported(store, bench, "big", 0.9, "fixture leaderboard")  # never re-run: it stands in
    card = build_scorecard(store, tmp_path / "card", registry=reg.path)
    row = card["axes"]["A"]["benchmarks"][0]
    assert row["basis"] == "as reported (not re-run)" and row["verdict"] == "lose" and row["best_baseline"] == "big"
    assert card["axes"]["A"]["status"] == "lose" and any("lose vs big" in x for x in card["losses"])
    run_system(store, reg, bench, FixtureSystem("big", "b1", passes=5), "baseline")  # now it is re-run
    card2 = build_scorecard(store, tmp_path / "card2", registry=reg.path)
    row2 = card2["axes"]["A"]["benchmarks"][0]
    assert row2["basis"] == "re-run" and row2["verdict"] == "win"
    assert row2["as_reported"][0]["used"].startswith("no")
    store.close()


def test_registry_pins_are_enforced(tmp_path):
    reg_path = _benchmark(tmp_path)
    reg = load_registry(reg_path)
    from custom_harness.store import EvidenceStore
    store = EvidenceStore(tmp_path / "store")
    (reg.directory(reg.benchmarks[0]) / "items.jsonl").write_text('{"id": "i00"}\n', encoding="utf-8")
    with pytest.raises(ScorecardError, match="does not match the pin"):
        run_system(store, reg, reg.benchmarks[0], FixtureSystem("ours", "v1", passes=1), "ours")
    assert store.records("scorecard_run") == []  # nothing was run or recorded
    doc = yaml.safe_load(reg_path.read_text(encoding="utf-8"))
    doc["benchmarks"][0]["licence"] = " "
    reg_path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ScorecardError, match="no licence"):
        load_registry(reg_path)
    doc["benchmarks"][0]["licence"] = "MIT"
    doc["benchmarks"].append(dict(doc["benchmarks"][0]))
    reg_path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ScorecardError, match="duplicate"):
        load_registry(reg_path)
    store.close()


def test_runner_refuses_partial_scores_and_unpaired_comparisons(tmp_path):
    from custom_harness.store import EvidenceStore
    store = EvidenceStore(tmp_path / "store")
    reg = load_registry(_benchmark(tmp_path))

    class Partial(FixtureSystem):
        def run(self, items, disabled):
            return {it["id"]: 1.0 for it in items[:-1]}
    with pytest.raises(ScorecardError, match="every item must be scored"):
        run_system(store, reg, reg.benchmarks[0], Partial("p", "v", 1), "ours")
    with pytest.raises(ScorecardError, match="identical item set"):
        paired_compare({"a": 1.0}, {"b": 1.0})
    a = {f"i{k}": float(k % 2) for k in range(30)}
    b = {k: 0.0 for k in a}
    assert paired_compare(a, b) == paired_compare(a, b)  # seeded: byte-identical intervals
    assert paired_compare(a, a)["verdict"] == "inconclusive"
    assert paired_compare(b, a)["verdict"] == "lose"
    store.close()


def test_shipped_registries_are_honest():
    assert load_registry().benchmarks == ()  # no real benchmark without an owner-approved download
    caps = load_capabilities()
    assert len(caps) == 37 and all(c.ablation is None for c in caps)  # GOAL §9, every row; no switch exists yet


def test_cli_build_and_check(tmp_path, monkeypatch):
    from custom_harness.cli import main
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    from custom_harness.store import EvidenceStore
    store = EvidenceStore(tmp_path / "data" / "store")
    reg = load_registry(_benchmark(tmp_path))
    run_system(store, reg, reg.benchmarks[0], FixtureSystem("ours", "v1", passes=10), "ours")
    store.close()
    out = tmp_path / "card"
    assert main(["scorecard", "build", "--out", str(out), "--registry", str(reg.path)]) == 0
    assert main(["scorecard", "check", str(out)]) == 0
    (out / "facts.json").write_text("{}", encoding="utf-8")
    assert main(["scorecard", "check", str(out)]) == 1
