"""GOAL §5 L8 isolation (s3-verifiers): the coupled-Goodhart report (it holds sealed scores) never reaches the evolver
or another agent-visible surface (invariant 18), and nothing is exported for training without a licence decision
(invariant 11). Attack tests check that the planted value really is in what would be exported before checking that
the export failed closed."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from custom_harness.evolve.export import ExportRefused, export_evidence, screen_digest
from custom_harness.weights import export as WX
from custom_harness.weights import goodhart as GH
from custom_harness.weights.licence import LicencePolicy
from test_evolve_evidence import _add_run, _outcome, world  # noqa: F401  (fixture re-export)
from test_weights_export import OPEN, PAIR, SET_A, SET_B, prov, traj, verdicts_for

SRC = Path(__file__).resolve().parents[1] / "src" / "custom_harness"
POINTS = [GH.GapPoint(t=float(i), a_score=0.71 + 0.0123457 * i, sealed_score=0.6931472 - 0.0011 * i)
          for i in range(6)]


def _recorded(store) -> tuple[GH.GoodhartReport, dict]:
    report = GH.goodhart_indicator(POINTS)
    GH.record_report(store, POINTS, report, note="owner dashboard")
    return report, GH.sealed_fingerprints(store)


def _readable_number(fp: dict) -> str:
    """A fingerprinted sealed score as it would be printed: the planted value."""
    return next(n for n in fp["numbers"] if n.startswith("0.69314"))


def test_reports_are_recorded_owner_only_and_fingerprinted(store):
    report, fp = _recorded(store)
    assert report.status == "alarm"  # a real widening gap: the report carries meaningful sealed information
    assert store.records(GH.RECORD_KIND)[0]["marker"] == GH.MARKER
    assert repr(POINTS[0].sealed_score) in fp["numbers"] and repr(report.slope) in fp["numbers"]
    for short in ("0.0", "1", "0.5", "1.0", "0.71"):  # too short to fingerprint without false positives
        assert short not in fp["numbers"]
    for p in POINTS:  # review M3: the A-scores (the evolver's own fitness) and times are not owner-only
        assert repr(p.a_score) not in fp["numbers"] and repr(p.t) not in fp["numbers"]
    n = _readable_number(fp)
    assert GH.scan(f"score {n} on gate", fp) and not GH.scan(f"score {n}9 on gate", fp)  # whole numbers only


@pytest.mark.parametrize("plant", ["number_in_trace", "number_in_lineage", "marker_in_trace"])
def test_a_planted_goodhart_value_in_the_evidence_export_fails_closed(store, tmp_path, world, plant):  # noqa: F811
    _, fp = _recorded(store)
    n = _readable_number(fp)
    if plant == "number_in_trace":
        rid = _add_run(store, world["spec"], "evo0", [("run", {"notes": f"sealed score was {n}"}, None)])
    elif plant == "marker_in_trace":
        rid = _add_run(store, world["spec"], "evo0", [("run", {"notes": f"see {GH.MARKER}"}, None)])
    else:
        rid = _add_run(store, world["spec"], "evo0", [("run", {"route": "fix"}, None)])
        store.append_record("lineage", {"parent": "p", "status": "rejected_gate", "candidate": "c" * 64,
                                        "reason": f"sealed {n}"})  # `reason` is in the proposer's lineage view
    runs = [*world["outcomes"], _outcome(rid, "evo0", 0.0, seed=3)]
    # the attack reached the export: without the Goodhart fingerprints the same export is written with the value
    honest = tmp_path / "probe"
    from custom_harness.evolve import export as EX
    real = EX.sealed_fingerprints
    try:
        EX.sealed_fingerprints = lambda s: {"markers": [], "numbers": []}  # type: ignore[assignment]
        export_evidence(store, honest, runs=runs, tasks=world["tasks"])
    finally:
        EX.sealed_fingerprints = real  # type: ignore[assignment]
    text = "\n".join(p.read_text(encoding="utf-8") for p in honest.rglob("*") if p.is_file())
    assert (GH.MARKER if plant == "marker_in_trace" else n) in text
    dest = tmp_path / "export"
    with pytest.raises(ExportRefused) as ei:
        export_evidence(store, dest, runs=runs, tasks=world["tasks"])
    assert any("Goodhart" in p for p in ei.value.problems), ei.value.problems
    assert n not in " ".join(ei.value.problems)  # locations, never values
    assert not dest.exists() and not list(tmp_path.glob(".export*"))


def test_an_honest_export_with_recorded_reports_still_succeeds(store, tmp_path, world):  # noqa: F811
    _recorded(store)
    res = export_evidence(store, tmp_path / "export", runs=world["outcomes"], tasks=world["tasks"])
    assert res.path.exists()


def test_the_proposer_diagnosis_withholds_a_goodhart_number(store, world):  # noqa: F811
    _, fp = _recorded(store)
    n = _readable_number(fp)
    digest = {"n_runs": 1, "clusters": [{"signature": "s", "count": 1, "examples": [f"gap {n}"]}]}
    out = screen_digest(digest, store, runs=world["outcomes"], tasks=world["tasks"])
    assert out.get("withheld", 0) >= 1 and n not in json.dumps(out)


# ------------------------------------------------------------------ static check: who may import the report
ALLOWED = {"evolve/export.py": {"scan", "sealed_fingerprints"}}
NAMES = {"GoodhartReport", "goodhart_indicator", "GapPoint", "record_report"}


def test_only_the_export_audit_imports_the_goodhart_module_outside_weights():
    found: dict[str, set[str]] = {}
    for p in SRC.rglob("*.py"):
        rel = p.relative_to(SRC).as_posix()
        if rel.startswith("weights/"):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.endswith("goodhart") or (mod.endswith("weights") and any(a.name == "goodhart"
                                                                                   for a in node.names)):
                    found.setdefault(rel, set()).update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                if any("goodhart" in a.name for a in node.names):
                    found.setdefault(rel, set()).add("<module>")
            elif isinstance(node, ast.Name) and node.id in NAMES:
                found.setdefault(rel, set()).add(node.id)
            elif isinstance(node, ast.Attribute) and (node.attr in NAMES or node.attr == "goodhart"):
                found.setdefault(rel, set()).add(node.attr)
    assert found == ALLOWED, found


# ------------------------------------------------------------------ licence decision at the export boundary
def test_default_policy_exports_no_row(tmp_path):
    ts = [traj("a"), traj("b")]
    vs = [v for t in ts for v in verdicts_for(t, SET_A, SET_B)]
    assert WX.select_for_training(ts, vs, PAIR, OPEN).kept  # the same trajectories are exportable when permitted
    res = WX.export_trajectories(ts, vs, PAIR, LicencePolicy.default(), tmp_path / "out")
    assert res.rows == [] and res.excluded == {"a": [WX.Reason.LICENCE], "b": [WX.Reason.LICENCE]}
    assert (tmp_path / "out" / "sft.jsonl").read_text(encoding="utf-8") == ""
    man = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert man["licence"]["default_nothing_permitted"] is True and man["licence"]["permitted"] == []


class Permissive:  # a duck-typed "licence" that permits everything
    permitted = frozenset({"local/ow"})

    def permits(self, p):
        return True

    def digest(self):
        return "0" * 64


class Sub(LicencePolicy):
    def permits(self, p):
        return True


@pytest.mark.parametrize("bad", [Permissive(), Sub(frozenset()), None])
def test_a_duck_typed_licence_is_refused(tmp_path, bad):
    t = traj("a", p=prov("closedai", terms="forbidden"))
    vs = verdicts_for(t, SET_A, SET_B)
    for call in (lambda: WX.select_for_training([t], vs, PAIR, bad),
                 lambda: WX.export_trajectories([t], vs, PAIR, bad, tmp_path / "out")):
        with pytest.raises(WX.ExportRefused) as e:
            call()
        assert e.value.code == "LICENCE_INVALID"
    assert not (tmp_path / "out").exists()


def test_an_unpermitted_row_never_reaches_sft_even_if_selection_is_bypassed(tmp_path, monkeypatch):
    t = traj("a", p=prov("local", terms="forbidden"))
    vs = verdicts_for(t, SET_A, SET_B)
    assert not OPEN.permits(t.steps[2].provenance)
    monkeypatch.setattr(WX, "select_for_training", lambda *a, **k: WX.Selection(kept=[t]))  # a broken selection
    with pytest.raises(WX.ExportRefused) as e:
        WX.export_trajectories([t], vs, PAIR, OPEN, tmp_path / "out")
    assert e.value.code == "LICENCE_RECHECK" and not (tmp_path / "out").exists()


def test_a_goodhart_number_in_training_rows_is_refused(store, tmp_path):
    _, fp = _recorded(store)
    n = _readable_number(fp)
    t = traj("a", answer=f"the sealed score is {n}")
    vs = verdicts_for(t, SET_A, SET_B)
    ok = WX.export_trajectories([t], vs, PAIR, OPEN, tmp_path / "probe")  # without the store: the value is written
    assert n in json.dumps(ok.rows)
    with pytest.raises(WX.ExportRefused) as e:
        WX.export_trajectories([t], vs, PAIR, OPEN, tmp_path / "out", store=store)
    assert e.value.code == "GOODHART_LEAK" and not (tmp_path / "out").exists()
    marked = traj("b", answer=f"from {GH.MARKER}")
    with pytest.raises(WX.ExportRefused):  # the marker is refused even without a store
        WX.export_trajectories([marked], verdicts_for(marked, SET_A, SET_B), PAIR, OPEN, tmp_path / "out2")
