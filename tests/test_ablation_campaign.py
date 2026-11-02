"""Multi-generation ablation arms (GOAL invariant 12; custom_harness/ablation.py): the campaign ledger accepts ABLATED
generations in its own `:ABLATED` kinds, marked, never accepting, apart from production; the switch is still only
made by the scorecard runner's entry point.

Fail-first (stated, not run in the no-test round): at the parent commit `open_campaign` has no `ablation` argument
(TypeError) and `CampaignLedger.record` reads only the production kinds, so an ablated generation (records under
`<kind>:ABLATED`) is refused with "expected exactly one new generation_stats record"."""

from __future__ import annotations

import pytest

from custom_harness.ablation import MARKER, NO_ABLATION, AblationError, AblationSet
from custom_harness.evolve import EvolveConfig
from custom_harness.evolve.generations import (
    LedgerError,
    first_parent,
    last_parent,
    open_campaign,
    verify_generation,
    verify_stopped,
)
from custom_harness.evolve.loop import evolve_generation
from custom_harness.gateway import MockBackend
from custom_harness.scorecard.runner import ablation_set
from test_ablation_switches_evolve import CFG, EDIT0, _proposal, _store, _Table, _tasks
from test_evolve import seed_spec

OFF = "novelty-gate"


def _arm(tmp_path, store, abl: AblationSet, campaign: str, generations: int):
    tasks = _tasks(tmp_path)
    parent = seed_spec()
    ev = _Table(store, tasks, parent.spec_hash(), ablation=abl)
    cfg = EvolveConfig(**CFG)
    led = open_campaign(store, campaign, None, cfg, ablation=abl)
    out = []
    for _ in range(generations):
        gcfg = led.begin(cfg)
        res = evolve_generation(parent, tasks, evaluator=ev, proposer=MockBackend([_proposal(EDIT0)]), store=store,
                                cfg=gcfg, ablation=abl)
        out.append((res, led.record(res)))
        parent = res.new_parent
    return led, out


def test_ablated_generations_are_ledgered_apart_marked_and_never_accept(tmp_path):
    store = _store(tmp_path, "s")
    abl = ablation_set({OFF})
    _led, gens = _arm(tmp_path, store, abl, "arm", 3)
    assert [g["generation"] for _, g in gens] == [0, 1, 2]
    for res, g in gens:
        assert res.accepted is False and g["signal"]["accepted"] is False
        assert g[MARKER] is True and g["disabled"] == [OFF]
    # the arm's parent never changes (it never accepts), so its stall rule fires after the window
    assert gens[-1][1]["stall"]["accepted"] == 0 and gens[-1][1]["stall"]["stalled"] is True
    # everything lives in the :ABLATED kinds; production kinds stay empty
    for kind in ("evolve_campaign", "generation_ledger", "generation_stats", "lineage", "harness_edit"):
        assert store.records(kind) == [], kind
    decl = store.records(f"evolve_campaign:{MARKER}")
    assert len(decl) == 1 and decl[0][MARKER] is True and decl[0]["disabled"] == [OFF]
    assert len(store.records(f"generation_ledger:{MARKER}")) == 3
    # recomputable from its own records, and invisible to the production readers
    assert all(verify_generation(store, n, ablated=True) == [] for n in range(3))
    assert verify_generation(store, 0) == ["generation_ledger #0 does not exist"]
    assert last_parent(store, "arm") is None and first_parent(store, "arm") is None
    assert last_parent(store, "arm", ablated=True) == seed_spec().spec_hash()
    store.close()


def test_an_ablation_arm_resumes_only_under_its_own_switch_set_and_never_as_production(tmp_path):
    store = _store(tmp_path, "s")
    _arm(tmp_path, store, ablation_set({OFF}), "arm", 1)
    cfg = EvolveConfig(**CFG)
    with pytest.raises(LedgerError, match="switched off"):
        open_campaign(store, "arm", None, cfg, ablation=ablation_set({OFF, "cost-term"}))
    led = open_campaign(store, "arm", None, cfg, ablation=ablation_set({OFF}))
    assert led is not None and led.generation == 1  # numbering continues in the arm
    # the same name as a production campaign is a different campaign (separate kinds), numbered from 0
    prod = open_campaign(store, "arm", None, cfg)
    assert prod is not None and prod.generation == 0 and not prod.ablated
    store.close()


def test_a_production_ledger_refuses_an_ablated_generation_and_vice_versa(tmp_path):
    store = _store(tmp_path, "s")
    tasks = _tasks(tmp_path)
    parent = seed_spec()
    cfg = EvolveConfig(**CFG)
    abl = ablation_set({OFF})
    prod = open_campaign(store, "p", None, cfg)
    prod.begin(cfg)
    res = evolve_generation(parent, tasks, evaluator=_Table(store, tasks, parent.spec_hash(), ablation=abl),
                            proposer=MockBackend([_proposal(EDIT0)]), store=store, cfg=cfg, ablation=abl)
    with pytest.raises(LedgerError):  # its records are in the :ABLATED kinds: not attributable to production
        prod.record(res)
    arm = open_campaign(store, "a", None, cfg, ablation=abl)
    arm.begin(cfg)
    res2 = evolve_generation(parent, tasks, evaluator=_Table(store, tasks, parent.spec_hash()),
                             proposer=MockBackend([_proposal(EDIT0)]), store=store, cfg=cfg)
    with pytest.raises(LedgerError):  # a production generation is not an arm's generation
        arm.record(res2)
    store.close()


def test_an_ablated_ledger_refuses_an_accept_and_another_arms_records(tmp_path):
    store = _store(tmp_path, "s")
    abl = ablation_set({OFF})
    led, [(res, _)] = _arm(tmp_path, store, abl, "arm", 1)
    # planted: an accept in this arm's records
    lin = store.records(f"lineage:{MARKER}")
    stats = store.records(f"generation_stats:{MARKER}")[-1]
    led.begin(EvolveConfig(**CFG))
    for r in lin:
        store.append_record(f"lineage:{MARKER}", {**r, "status": "accepted"} if r["status"] == "ablated_not_accepted"
                            else r)
    store.append_record(f"generation_stats:{MARKER}", {**stats, "accepted": 1, "gated": 1})
    forged = type(res)(res.parent_hash, res.new_parent, True, "forged", [*lin], 0, 0, "MECHANICAL",
                       {**res.counters, "accepted": 1, "gated": 1})
    with pytest.raises(LedgerError, match="ABLATED"):
        led.record(forged)
    # records of another arm (another switched-off set) are refused
    led.begin(EvolveConfig(**CFG))
    for r in lin:
        store.append_record(f"lineage:{MARKER}", {**r, "disabled": ["cost-term"]})
    store.append_record(f"generation_stats:{MARKER}", {**stats, "disabled": ["cost-term"]})
    with pytest.raises(LedgerError, match="switched off"):
        led.record(res)
    store.close()


def test_stopped_generation_of_an_arm_is_marked_and_verifiable(tmp_path):
    store = _store(tmp_path, "s")
    abl = ablation_set({OFF})
    led = open_campaign(store, "arm", None, EvolveConfig(**CFG), ablation=abl)
    led.begin(EvolveConfig(**CFG))
    rec = led.record_stopped(seed_spec().spec_hash(), "budget_exceeded", "campaign budget exhausted")
    assert rec[MARKER] is True and store.records("generation_stopped") == []
    assert verify_stopped(store, 0, ablated=True) == []
    store.close()


def test_the_switch_is_still_only_made_by_the_scorecard_runner(tmp_path):
    with pytest.raises(AblationError, match="scorecard runner"):
        AblationSet({OFF})
    assert open_campaign(_store(tmp_path, "s"), None, None, EvolveConfig(**CFG), ablation=NO_ABLATION) is None
    with pytest.raises(TypeError):
        open_campaign(_store(tmp_path, "s2"), "c", None, EvolveConfig(**CFG), ablation=frozenset({OFF}))
