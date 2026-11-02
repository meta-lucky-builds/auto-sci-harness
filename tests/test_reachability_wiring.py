"""Item 3 of s4-wiring (GOAL invariant 12, no README-only features): capabilities that were library-level only are
reachable from a shipped spec or the CLI, each behind an explicit opt-in; the defaults are unchanged.

Fail-first (stated, not run in the no-test round): at the parent commit `chh evolve` has no --race-allocator
(argparse exits 2), the registry texts do not name research_v2's opt-in tournament, and
`live_client` / `offline_client` take no `ablation` keyword (TypeError)."""

from __future__ import annotations

from pathlib import Path

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
def test_shipped_research_v2_ranks_by_execution_and_the_tournament_is_opt_in():
    """research_v2's rank node is research.admit_rank (s3-hypo) with `tournament: null`: execution ranks as shipped;
    the opt-in `params.tournament` reaches research/tournament.py, where btl_tournament / data_profile_kway_rank act
    (their registry text says so)."""
    import yaml

    from custom_harness.ablation import SWITCHES
    doc = yaml.safe_load((Path(__file__).resolve().parent.parent / "examples" / "research_v2.yaml").read_text(
        encoding="utf-8"))
    rank = doc["nodes"]["rank"]
    assert rank["module"] == "research.admit_rank" and rank["params"]["tournament"] is None
    for sw in ("btl_tournament", "data_profile_kway_rank"):
        assert "research.admit_rank params.tournament" in " ".join(SWITCHES[sw].turns_off.split())


# ------------------------------------------------------------------------------------------- literature retrieval
def test_literature_client_factories_thread_the_ablation_set(tmp_path):
    from custom_harness.literature.client import live_client
    from custom_harness.literature.snapshot import SnapshotStore
    store = SnapshotStore(tmp_path / "lit")
    assert live_client(store).ablation == NO_ABLATION  # production default: every source, with fallback
    abl = ablation_set({"multi_source_retrieval"})
    assert live_client(store, ablation=abl).ablation.off("multi_source_retrieval")
