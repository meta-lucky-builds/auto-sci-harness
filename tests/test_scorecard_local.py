"""Local scorecard (`chh scorecard local`; GOAL §2 axes F, G, H and B partial, invariant 8, §2 rule 5).

A fixture store holds: a Kernel-certified claim from model-free runs (kernel_world) with its bundle and a research-style
report whose checklist predates the loop-label item; a run with subscription (byos) model calls whose USD is not
reported; a scripted (mock) run. A fixture repo holds a findings registry with a git history on two dates, a museum
patch set and a museum log. Every number must resolve to its artifact, unknown costs must stay unknown, and every
axis that cannot be computed must be listed as NOT MEASURED with its reason.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from custom_harness.kernel.bundle import build_bundle
from custom_harness.records import Cost, Provenance, Run, StepKind, TraceStep
from custom_harness.scorecard.axes import numbers_without_sources
from kernel_world import World
from test_scorecard import _report_run


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")


def _git(repo: Path, *args: str, date: str | None = None) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    if date:
        env |= {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def _repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "redteam").mkdir(parents=True)
    reg = repo / "redteam" / "findings.yaml"
    _git(repo, "init", "-q")
    reg.write_text(yaml.safe_dump({"findings": [{"id": "A", "status": "open", "museum": "redteam/museum/A.patch"}]}),
                   encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one", date="2026-10-01T10:00:00+00:00")
    reg.write_text(yaml.safe_dump({"findings": [
        {"id": "A", "status": "fixed", "museum": "redteam/museum/A.patch"},
        {"id": "B", "status": "fixed", "museum": "redteam/museum/B.patch"},
        {"id": "C", "status": "accepted"}]}), encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two", date="2026-10-03T10:00:00+00:00")
    return repo


def _step(store, run_id: str, i: int, *, cost: Cost, mode: str | None) -> None:
    prov = Provenance(backend="codex" if mode == "byos" else "mock", provider="x", model="m", mode=mode,
                      training_on_outputs="unknown") if mode else None
    now = time.time()
    store.add_step(TraceStep(run_id=run_id, index=i, node_id=f"n{i}", kind=StepKind.LLM, input={}, output={},
                             provenance=prov, cost=cost, started_at=now, ended_at=now))


def _world(tmp_path: Path):
    w = World(tmp_path / "world", seed=11)
    c = w.claim(w.artifact("good"), w.artifact("baseline", "baseline"), delta=0.2)
    w.kernel.preregister(c)
    cert = w.kernel.certify(c, w.evaluate_all(c))
    assert cert.label.value == "CERTIFIED-INTERNAL"
    bh = build_bundle(w.kernel, cert.cert_hash(), tmp_path / "bundles" / "c.zip")
    _report_run(w, cert, bh)  # its checklist has no loop_label item (it predates it)
    live = Run(harness_id="research_v1", harness_hash="h", task_id="t")
    w.store.add_run(live)
    _step(w.store, live.id, 0, cost=Cost(prompt_tokens=1000, completion_tokens=50, quota_units=1.0, usd=0.0,
                                        estimated=True), mode="byos")
    _step(w.store, live.id, 1, cost=Cost(wall_s=2.5), mode=None)
    mock = Run(harness_id="research_v1", harness_hash="h", task_id="t")
    w.store.add_run(mock)
    _step(w.store, mock.id, 0, cost=Cost(prompt_tokens=10, completion_tokens=5, estimated=True), mode="mock")
    return w, cert


def _museum(tmp_path: Path, lines: str) -> Path:
    p = tmp_path / "museum.log"
    p.write_text("worktree: X  HEAD: abc1234\n" + lines, encoding="utf-8", newline="")
    return p


def _card(w, repo, museum):
    from custom_harness.scorecard.local_axes import build_local, check_local, extract_local
    facts = extract_local(w.store, repo, museum_log=museum)
    card = build_local(facts)
    return card, check_local(card, w.store, repo)


def test_wilson_interval_known_values():
    from custom_harness.scorecard.local_axes import wilson
    lo, hi = wilson(45, 45)
    assert lo == pytest.approx(0.92134, abs=1e-5) and hi == 1.0
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.27753, abs=1e-5)
    lo, hi = wilson(5, 10)
    assert lo == pytest.approx(0.23659, abs=1e-5) and hi == pytest.approx(0.76341, abs=1e-5)
    with pytest.raises(ValueError):
        wilson(3, 2)


def test_local_axes_every_number_resolves_and_nothing_is_hidden(tmp_path):
    w, _cert = _world(tmp_path)
    repo = _repo(tmp_path)
    museum = _museum(tmp_path, "KILLED    A: 1 test(s) failed\nKILLED    Z: stale entry\nmuseum: 2/2 KILLED\n")
    card, problems = _card(w, repo, museum)
    assert problems == [] and numbers_without_sources(card) == []
    ax = card["axes"]
    # A, C, D, E: no pinned benchmark run -> NOT MEASURED with the reason, and listed in the footer
    for a in ("A", "C", "D", "E"):
        assert ax[a]["status"] == "not measured" and ax[a]["why"]
        assert any(x.startswith(f"axis {a}: NOT MEASURED (") for x in card["not_measured"]), card["not_measured"]
    # B: no evolve ledger in this store -> NOT MEASURED, never a number
    assert ax["B"]["gain_per_1k_rollouts"]["value"] is None and ax["B"]["status"] == "not measured"
    # G: subscription calls report no USD: unknown, never 0; the certified claim is from model-free runs
    g = ax["G"]
    assert g["total_cost"]["value"]["usd"] == "unknown" and g["total_cost"]["value"]["gpu_hours"] == "not metered"
    assert g["total_cost"]["value"]["quota_units"] == 1.0 and g["total_cost"]["estimated"] is True
    assert g["total_cost"]["runs_scripted"] == 1 and g["total_cost"]["runs_with_real_spend"] == 1
    why = g["cost_per_certified_live_claim"]["why"]
    assert g["cost_per_certified_live_claim"]["value"] is None and why.startswith("undefined: 0 certified live claims")
    # H: catch rate over the registry's patches, with the Wilson interval; B's patch is not in the log
    h = ax["H"]["planted_exploit_catch_rate"]
    assert (h["numerator"], h["denominator"], h["value"]) == (1, 1, 1.0)
    assert h["museum_patches_not_in_log"] == ["B"] and h["log_findings_not_in_registry"] == ["Z"]
    assert h["status"] == "fail" and h["log_head"] == "abc1234"
    assert h["ci95_wilson_two_sided"] == pytest.approx([0.20654931, 1.0])
    t = ax["H"]["red_team_trend_by_date"]["value"]
    assert [(p["date"], p["open"], p["fixed"], p["accepted"], p["total"]) for p in t] == [
        ("2026-10-01", 1, 0, 0, 1), ("2026-10-03", 0, 2, 1, 3)]
    rt = ax["H"]["red_team_findings"]
    assert rt["value"]["unfixed_rate"] == pytest.approx(1 / 3) and rt["status"] == "fail"  # accepted = unfixed
    # F: the report's checklist lacks the loop-label item -> reported as a failure with the reason
    f = ax["F"]["reports_with_checklist_and_bundle"]
    assert f["status"] == "fail" and "loop_label" in json.dumps(f["offenders"])
    runs = ax["F"]["runs_with_bundle_and_cost_record"]
    assert runs["status"] == "fail" and runs["numerator"] == 0  # these fixture runs have no run bundle (inv. 9)
    assert any("axis F" in x for x in card["losses"])
    # loop labels: the report run has no label line (fixture report) -> flagged, not hidden
    assert card["loop_labels"]["status"] == "fail"


def test_a_changed_source_artifact_is_reported(tmp_path):
    w, _ = _world(tmp_path)
    repo = _repo(tmp_path)
    museum = _museum(tmp_path, "KILLED    A: ok\n")
    card, problems = _card(w, repo, museum)
    assert problems == []
    from custom_harness.scorecard.local_axes import check_local
    museum.write_text("KILLED    A: edited\n", encoding="utf-8")
    (repo / "redteam" / "findings.yaml").write_text("findings: []\n", encoding="utf-8")
    probs = check_local(card, w.store, repo)
    assert any(p.startswith("file:") for p in probs) and any(p.startswith("repo:redteam/findings.yaml") for p in probs)


def test_unknown_usd_is_never_summed_as_zero(tmp_path):
    """The shared axes.axis_g sums Cost.usd, so a subscription run shows $0.0; the local axis G must not."""
    from custom_harness.scorecard.facts import extract
    from custom_harness.scorecard.local_axes import axis_g, extract_local
    from custom_harness.store import EvidenceStore
    s = EvidenceStore(tmp_path / "store")
    r = Run(harness_id="x", harness_hash="h", task_id="t")
    s.add_run(r)
    _step(s, r.id, 0, cost=Cost(prompt_tokens=5, quota_units=1.0, usd=0.0, estimated=True), mode="byos")
    from custom_harness.scorecard import axes
    assert axes.axis_g(extract(s))["total_cost"]["value"]["usd"] == 0.0  # the shared axis: unknown shown as 0
    (tmp_path / "redteam").mkdir()
    (tmp_path / "redteam" / "findings.yaml").write_text("findings: []\n", encoding="utf-8")
    g = axis_g(extract_local(s, tmp_path, use_git=False))
    assert g["total_cost"]["value"]["usd"] == "unknown" and g["total_cost"]["runs_usd_unknown"] == [r.id]
    s.close()


def test_cli_prints_the_table_and_the_honesty_footer(tmp_path, monkeypatch, capsys):
    from custom_harness.cli import main
    from custom_harness.store import EvidenceStore
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    EvidenceStore(tmp_path / "data" / "store").close()
    repo = _repo(tmp_path)
    capsys.readouterr()
    rc = main(["scorecard", "local", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "HONESTY FOOTER" in out
    for a in ("A", "C", "D", "E"):
        assert f"axis {a}: NOT MEASURED (" in out
    assert "axis G cost_per_certified_live_claim: NOT MEASURED (undefined: 0 certified claims (no runs" in out
    assert "axis H planted_exploit_catch_rate: NOT MEASURED (no museum run log given" in out
    assert "axis B gain_per_1k_rollouts: NOT MEASURED" in out
    rc = main(["scorecard", "local", "--repo", str(repo), "--json"])
    card = json.loads(capsys.readouterr().out)
    assert rc == 0 and card["source_problems"] == [] and card["format"] == "chh-scorecard-local/1"
