"""Invariant-12 coverage on the scorecard counts only real, tested switches, and keeps saying "no ablation run
recorded" until an ablation run is recorded (custom_harness/ablation.py, scorecard/registry.switch_fact).

No ablation run is made in this repository: a run needs model calls and a pinned benchmark (owner decisions). So with
the shipped capabilities.yaml every row stays an invariant-12 gap, and the scorecard must say exactly why."""

from __future__ import annotations

from types import MappingProxyType

import pytest

import custom_harness.ablation as A
import custom_harness.scorecard.registry as G
import custom_harness.scorecard.runner as R
from custom_harness.ablation import Switch
from custom_harness.records import Run
from custom_harness.scorecard import build_scorecard, check_scorecard, load_capabilities
from custom_harness.scorecard.registry import DEFAULT_CAPABILITIES, switch_fact


def test_shipped_registry_reports_every_switch_as_no_ablation_run_recorded(store, tmp_path):
    card = build_scorecard(store, tmp_path / "card", capabilities=DEFAULT_CAPABILITIES)
    cov = card["coverage"]
    caps = load_capabilities()
    assert cov["status"] == "fail" and len(cov["rows"]) == len(caps) == 37
    assert cov["capabilities_with_ablation"]["value"] == 0 and cov["capabilities_with_ablation"]["of"] == 37
    assert sorted(cov["gaps"]) == sorted(c.id for c in caps)  # no row is covered without a recorded run
    rows = {r["id"]: r for r in cov["rows"]}
    switched = [c for c in caps if c.ablation is not None and c.ablation.switch is not None
                and not c.ablation.not_covered and c.ablation.switch in A.SWITCHES]
    assert cov["capabilities_with_switch"]["value"] == len(switched)
    for c in switched:
        assert rows[c.id]["why"] == f"switch {c.ablation.switch!r}: no ablation run recorded", c.id
    for c in caps:
        if c.ablation is not None and c.ablation.not_ablatable is not None:
            assert rows[c.id]["why"] == f"not ablatable: {c.ablation.not_ablatable}", c.id
        elif c.ablation is not None and c.ablation.not_covered:
            assert rows[c.id]["why"].startswith(f"partial switch: {c.ablation.switch!r}"), c.id
    assert any("coverage (§2.1, invariant 12): fail" in x for x in card["losses"])
    md = (tmp_path / "card" / "scorecard.md").read_text(encoding="utf-8")
    assert "no ablation run recorded" in md or not switched
    assert check_scorecard(tmp_path / "card", store)["reproduced"]


def test_switch_fact_counts_only_a_registered_tested_full_switch_for_its_own_row(monkeypatch):
    reg = MappingProxyType({**A.SWITCHES, "s_ok": Switch("s_ok", "row-a", "x", "tests/t.py::f"),
                            "s_untested": Switch("s_untested", "row-u", "x", " ")})
    for mod in (A, R, G):
        monkeypatch.setattr(mod, "SWITCHES", reg)
    assert switch_fact("row-a", {"switch": "s_ok"}) == {"has_switch": True, "switch": "s_ok", "test": "tests/t.py::f"}
    assert switch_fact("row-a", "s_ok")["has_switch"]  # a bare string is a switch name
    cases = {
        "no ablation switch": switch_fact("row-a", None),
        "not in the closed registry": switch_fact("row-a", {"switch": "made_up"}),
        "not in the closed registry ": switch_fact("row-b", {"switch": "s_ok"}),  # another row's switch
        "names no test": switch_fact("row-u", {"switch": "s_untested"}),
        "partial switch": switch_fact("row-a", {"switch": "s_ok", "not_covered": {"part": "not built"}}),
        "not ablatable: integrity": switch_fact("row-a", {"not_ablatable": "integrity"}),
        "malformed": switch_fact("row-a", {"switch": "s_ok", "not_ablatable": "both"}),
        "malformed ": switch_fact("row-a", {"not_ablatable": " "}),
        "malformed  ": switch_fact("row-a", {"switch": "s_ok", "not_covered": {"part": ""}}),
        "malformed   ": switch_fact("row-a", {"switch": "s_ok", "extra": 1}),
    }
    for want, fact in cases.items():
        assert fact["has_switch"] is False and want.strip() in fact["why"], (want, fact)


def test_ablated_runs_are_listed_apart_and_never_counted_on_production_axes(store, tmp_path):
    prod = Run(harness_id="h", harness_hash="h", task_id="t")
    abl = Run(harness_id="h", harness_hash="h", task_id="t", ablated=("x",))
    store.add_run(prod)
    store.add_run(abl)
    card = build_scorecard(store, tmp_path / "card")
    facts = __import__("json").loads((tmp_path / "card" / "facts.json").read_text(encoding="utf-8"))
    assert [r["run_id"] for r in facts["runs"]] == [prod.id]
    assert [(r["run_id"], r["ablated"]) for r in facts["ablated_runs"]] == [(abl.id, ["x"])]
    assert card["facts_sha256"] and check_scorecard(tmp_path / "card", store)["reproduced"]


def test_run_ablation_refuses_a_switch_outside_the_closed_registry(store, tmp_path):
    from custom_harness.scorecard import ScorecardError, load_registry, run_ablation
    from test_scorecard import FixtureSystem, _benchmark
    reg = load_registry(_benchmark(tmp_path))
    with pytest.raises(ScorecardError, match="closed registry"):
        run_ablation(store, reg, reg.benchmarks[0], FixtureSystem("ours", "v1", passes=3), "skills_typo")
    assert store.records("scorecard_run") == []
