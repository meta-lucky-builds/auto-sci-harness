"""s4-wiring items 1 (driver part), 2 and 5:
- the evolve driver runs a multi-generation ABLATION ARM given the scorecard runner's AblationSet (`:ABLATED` ledger,
  never accepts, resumes only under its own set, no branching);
- the L7 islands driver is on a run path (`chh evolve --islands FILE`, opt-in), with no weights lever;
- the driver's agent-visible output carries the verdict bit and a closed reason code, never the free-text decision.

Fail-first (stated, not run in the no-test round): at the parent commit `evolve()` and `evolve_islands()` take no
`ablation` (TypeError), `chh evolve` has no `--islands` (argparse exits 2), `load_islands_config` / `decision_code`
do not exist (ImportError), and gen000.json carries `decision` (free text), not `decision_code`."""

from __future__ import annotations

import json

import pytest

from custom_harness.ablation import MARKER, NO_ABLATION
from custom_harness.evolve import EvolveConfig, GenerationResult
from custom_harness.evolve.branches import BranchConfig
from custom_harness.evolve.generations import LedgerError
from custom_harness.gateway import MockBackend
from custom_harness.scorecard.runner import ablation_set
from test_ablation_switches_evolve import CFG, EDIT0, _proposal, _store, _Table, _tasks
from test_evolve import seed_spec

OFF = "novelty-gate"


# ------------------------------------------------------------------------------------------- item 1: driver arms
def _drive(monkeypatch, store, tasks, parent):
    import custom_harness.evolve.driver as driver
    monkeypatch.setattr(driver, "Evaluator", lambda backends, st, sf, working, bpr, **kw: _Table(
        st, working, parent.spec_hash(), ablation=kw.get("ablation", NO_ABLATION)))
    monkeypatch.setattr(driver, "check_campaign", lambda *a, **k: 0)

    def run(generations, out, **kw):
        return driver.evolve(parent, tasks, generations=generations, backends={},
                             proposer=MockBackend([_proposal(EDIT0)]), store=store, sandbox_factory=lambda k: None,
                             cfg=EvolveConfig(**CFG), out_dir=out, log=lambda s: None, **kw)
    return run


def test_driver_runs_a_multi_generation_ablation_arm_apart_from_production(tmp_path, monkeypatch):
    store = _store(tmp_path, "s")
    tasks, parent = _tasks(tmp_path), seed_spec()
    run = _drive(monkeypatch, store, tasks, parent)
    abl = ablation_set({OFF})
    out = tmp_path / "out"
    final = run(3, out, campaign="arm", ablation=abl)
    assert final.spec_hash() == parent.spec_hash()  # an arm never accepts: its parent never changes
    assert len(store.records(f"generation_ledger:{MARKER}")) == 3
    for kind in ("generation_ledger", "evolve_campaign", "lineage", "generation_stats", "harness_edit",
                 "evolve_proposal"):
        assert store.records(kind) == [], kind  # nothing in the production kinds (no proposal log in an arm)
    for g in range(3):
        summary = json.loads((out / f"gen{g:03d}.json").read_text(encoding="utf-8"))
        assert summary["accepted"] is False and summary["decision_code"] == "ablated"
        assert summary[MARKER] is True and summary["disabled"] == [OFF]
    # resume: the arm's numbering continues; another switch set is refused; an arm makes no branches
    run(1, out, campaign="arm", ablation=abl)
    assert len(store.records(f"generation_ledger:{MARKER}")) == 4 and (out / "gen003.json").exists()
    with pytest.raises(LedgerError, match="switched off"):
        run(1, out, campaign="arm", ablation=ablation_set({OFF, "cost-term"}))
    with pytest.raises(ValueError, match="branch"):
        run(1, out, campaign="arm2", ablation=abl, branching=BranchConfig(cluster_key="family"))
    # the production campaign of the same name is untouched and starts at generation 0
    run(1, tmp_path / "prod", campaign="arm")
    assert [r["generation"] for r in store.records("generation_ledger")] == [0]
    store.close()


# ------------------------------------------------------------------------------------------- item 5: closed codes
def _res(status, *, accepted=False, rejected_on=None):
    cands = [{"status": "raced"}] + ([{"status": status, "rejected_on": rejected_on}] if status else [])
    return GenerationResult("p" * 64, seed_spec(), accepted, "no improvement on gate split (mean=-0.5)", cands)


@pytest.mark.parametrize("status,accepted,stage,code", [
    ("accepted", True, None, "accepted"), ("rejected_gate", False, "held_out_gate", "held_out_gate"),
    ("rejected_gate", False, "evolve", "evolve"), ("rejected_gate", False, "cost", "cost"),
    ("rejected_gate", False, "exclusion_asymmetry", "exclusion_asymmetry"),
    ("infra_aborted", False, None, "inconclusive"), ("ablated_not_accepted", False, None, "ablated"),
    (None, False, None, "not_gated")])
def test_decision_code_is_closed_and_never_the_free_text(status, accepted, stage, code):
    from custom_harness.evolve.driver import DECISION_CODES, decision_code
    assert decision_code(_res(status, accepted=accepted, rejected_on=stage)) == code
    assert code in DECISION_CODES and all("=" not in c and " " not in c for c in DECISION_CODES)


def test_decision_code_fails_closed_on_an_unreleased_stage_or_a_mismatched_bit():
    from custom_harness.evolve.driver import decision_code
    with pytest.raises(ValueError):
        decision_code(_res("rejected_gate", rejected_on="mean=-0.5"))
    with pytest.raises(ValueError):
        decision_code(_res("rejected_gate", accepted=True, rejected_on="cost"))
    with pytest.raises(ValueError):
        decision_code(_res(None, accepted=True))


# ------------------------------------------------------------------------------------------- item 2: islands
ISLANDS = """
rounds: 2
total_budget: 6
policy: {version: p1}
proposers: {a: mock, b: mock}
islands:
  - {id: i1, proposers: {a: 1}, mutators: {prompt_text: 1, control_flow: 1}}
  - {id: i2, proposers: {b: 1}, mutators: {control_flow: 1}}
"""


def test_islands_file_is_checked_closed_keys_and_no_weights_lever(tmp_path):
    from custom_harness.evolve.search import load_islands_config
    p = tmp_path / "islands.yaml"
    p.write_text(ISLANDS, encoding="utf-8")
    cfg = load_islands_config(p)
    assert cfg.rounds == 2 and [i.island_id for i in cfg.islands] == ["i1", "i2"] and cfg.policy.version == "p1"
    assert cfg.challenger is None and cfg.trial is None and cfg.controller is None
    for bad in (ISLANDS + "weights_export: x\n", ISLANDS + "bogus: 1\n", ISLANDS.replace("rounds: 2", "rounds: 0"),
                ISLANDS.replace("{version: p1}", "{version: p1, nope: 1}"),
                ISLANDS.replace("id: i2", "id: ../x"), ISLANDS.replace("id: i2", "id: i1"),
                ISLANDS + "trial: {n_pairs: 4}\n"):
        p.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError):
            load_islands_config(p)


def test_chh_evolve_islands_runs_the_l7_driver_opt_in(tmp_path, monkeypatch):
    import yaml

    import custom_harness.evolve.driver as driver
    import custom_harness.evolve.search as search
    from custom_harness.cli import main
    seen = {}

    def fake(seed, suite, **kw):
        seen.update(kw)
        return search.SearchResult({i.island_id: seed for i in kw["islands"]}, policy_version="p1")
    monkeypatch.setattr(search, "evolve_islands", fake)
    monkeypatch.setattr(driver, "load_suite", lambda p: [])
    spec = tmp_path / "spec.yaml"
    spec.write_text(yaml.safe_dump(seed_spec().model_dump(mode="json")), encoding="utf-8")
    isl = tmp_path / "islands.yaml"
    isl.write_text(ISLANDS, encoding="utf-8")
    base = ["evolve", str(spec), "--suite", str(tmp_path / "suite.yaml"), "--dev-sandbox", "--campaign-id", "c1"]
    assert main(base + ["--islands", str(isl)]) == 0
    assert seen["rounds"] == 2 and sorted(seen["proposers"]) == ["a", "b"] and seen["campaign"] == "c1"
    assert seen.get("weights_export") is None and seen["controller"] is None  # no weights lever from the CLI
    assert "ablation" not in seen  # production: the CLI never switches a capability off
    from custom_harness.config import data_dir
    summary = json.loads((data_dir() / "evolve" / "c1" / "islands.json").read_text(encoding="utf-8"))
    assert set(summary) == {"parents", "policy_version", "stopped", "n_decisions", "trial_decisions"}
    for extra in (["--proposer", "mock"], ["--generations", "3"], ["--branch-by", "family"]):
        with pytest.raises(SystemExit):
            main(base + ["--islands", str(isl)] + extra)
    with pytest.raises(SystemExit):  # without --islands the proposer is required
        main(base)


def test_an_ablated_islands_campaign_refuses_a_controller(tmp_path):
    from custom_harness.control.islands import Island, OrchestratorPolicy
    from custom_harness.evolve.search import evolve_islands
    store = _store(tmp_path, "s")
    with pytest.raises(ValueError, match="controller"):
        evolve_islands(seed_spec(), _tasks(tmp_path), islands=[Island("i1", {"a": 1}, {"control_flow": 1})],
                       policy=OrchestratorPolicy("p1"), rounds=1, total_budget=2, proposers={"a": MockBackend(["x"])},
                       backends={}, store=store, sandbox_factory=lambda k: None, cfg=EvolveConfig(**CFG),
                       campaign="c", controller=MockBackend(["x"]), ablation=ablation_set({"lever-choice"}))
    store.close()


def test_islands_mode_refuses_a_usd_cap_on_unpriced_backends(tmp_path, monkeypatch):
    """Review finding: `cmd_evolve` refuses --max-usd for backends without a calibrated usd rate (invariant 9);
    the islands mode must too (mock proposers report no real usd). Fail-first: before the fix the cap was accepted."""
    import yaml

    import custom_harness.evolve.driver as driver
    import custom_harness.evolve.search as search
    from custom_harness.cli import main
    monkeypatch.setattr(search, "evolve_islands", lambda *a, **k: pytest.fail("must refuse before running"))
    monkeypatch.setattr(driver, "load_suite", lambda p: [])
    spec = tmp_path / "spec.yaml"
    spec.write_text(yaml.safe_dump(seed_spec().model_dump(mode="json")), encoding="utf-8")
    isl = tmp_path / "islands.yaml"
    isl.write_text(ISLANDS, encoding="utf-8")
    with pytest.raises(SystemExit, match="max-usd"):
        main(["evolve", str(spec), "--suite", str(tmp_path / "s.yaml"), "--dev-sandbox", "--islands", str(isl),
              "--max-usd", "5"])
