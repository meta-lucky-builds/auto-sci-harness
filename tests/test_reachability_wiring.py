"""Item 3 of s4-wiring (GOAL invariant 12, no README-only features): capabilities that were library-level only are
reachable from a shipped spec or the CLI, each behind an explicit opt-in; the defaults are unchanged.

Fail-first (stated, not run in the no-test round): at the parent commit `chh evolve` has no --race-allocator
(argparse exits 2), `research.execution_rank` ignores params (no tournament, so no 'ablated' tournament mode), and
`live_client` / `offline_client` take no `ablation` keyword (TypeError)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_harness.ablation import NO_ABLATION
from custom_harness.scorecard.runner import ablation_set


# ------------------------------------------------------------------------------------------- race allocators
@pytest.mark.parametrize("flag,expected", [(None, "batch"), ("voi", "voi"), ("per_completion", "per_completion")])
def test_chh_evolve_race_allocator_is_an_explicit_opt_in(tmp_path, monkeypatch, flag, expected):
    import custom_harness.evolve.driver as driver
    from custom_harness.cli import main
    from test_evolve import seed_spec
    seen = {}

    def fake_evolve(spec, suite, **kw):
        seen["cfg"] = kw["cfg"]
        return spec
    monkeypatch.setattr(driver, "evolve", fake_evolve)
    monkeypatch.setattr(driver, "load_suite", lambda p: [])
    spec = tmp_path / "spec.yaml"
    import yaml
    spec.write_text(yaml.safe_dump(seed_spec().model_dump(mode="json")), encoding="utf-8")
    argv = ["evolve", str(spec), "--suite", str(tmp_path / "suite.yaml"), "--proposer", "mock", "--dev-sandbox"]
    assert main(argv + (["--race-allocator", flag] if flag else [])) == 0
    assert seen["cfg"].race_allocator == expected and seen["cfg"].race_slots == 1  # never concurrent by a flag
    with pytest.raises(SystemExit):
        main(argv + ["--race-allocator", "bogus"])


# ------------------------------------------------------------------------------------------- BTL tournament (v2)
def _rank_state():
    hyps = [{"id": f"h{i}", "idea": f"idea {i}", "rationale": "r"} for i in range(3)]
    return {"hypotheses": {"hypotheses": hyps}, "task": {"description": "d", "metric": "acc"}, "profile": {}}


def test_execution_rank_is_the_default_and_the_tournament_is_opt_in():
    from custom_harness.research.modules import execution_rank
    ctx = SimpleNamespace(ablation=NO_ABLATION)
    out = execution_rank(_rank_state(), {}, ctx)
    assert out["ranking"]["order"] == ["h0", "h1", "h2"] and "execution" in out["ranking"]["method"]
    # opt-in: params.tournament delegates to research/tournament.py, so its ablation switches reach research_v2
    ctx_off = SimpleNamespace(ablation=ablation_set({"btl_tournament"}))
    out2 = execution_rank(_rank_state(), {"tournament": {"rounds": 3}}, ctx_off)
    assert out2["ranking"]["mode"] == "ablated" and "btl_tournament" in out2["ranking"]["method"]
    assert [h["id"] for h in out2["queue"]] == ["h0", "h1", "h2"]
    with pytest.raises(ValueError, match=r"params.tournament"):
        execution_rank(_rank_state(), {"tournament": 3}, ctx)


def test_shipped_research_v2_still_ranks_by_execution():
    import yaml
    doc = yaml.safe_load((Path(__file__).resolve().parent.parent / "examples" / "research_v2.yaml").read_text(
        encoding="utf-8"))
    rank = doc["nodes"]["rank"]
    assert rank["module"] == "research.execution_rank" and "tournament" not in (rank.get("params") or {})


# ------------------------------------------------------------------------------------------- literature retrieval
def test_literature_client_factories_thread_the_ablation_set(tmp_path):
    from custom_harness.literature.client import live_client
    from custom_harness.literature.snapshot import SnapshotStore
    store = SnapshotStore(tmp_path / "lit")
    assert live_client(store).ablation == NO_ABLATION  # production default: every source, with fallback
    abl = ablation_set({"multi_source_retrieval"})
    assert live_client(store, ablation=abl).ablation.off("multi_source_retrieval")
