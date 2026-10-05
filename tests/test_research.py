"""End-to-end research loop with a scripted (mock) LLM and trusted, stdlib-only solution code."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.gateway import MockBackend
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.runtime.template import PrivateDataAccess, render
from custom_harness.sandbox import LocalSandbox
from custom_harness.verify.ranking import bradley_terry, rankings_to_wins
from custom_harness.verify.stats import bootstrap_diff_ci, t2_verdict

ROOT = Path(__file__).resolve().parents[1]

# A genuinely better stdlib solution for the rings task: kNN on radius (x1^2 + x2^2).
KNN_RADIUS = '''
import argparse, csv, math
p = argparse.ArgumentParser()
for k in ("--train", "--test", "--out"): p.add_argument(k)
p.add_argument("--seed", type=int)
a = p.parse_args()
def load(path):
    with open(path, newline="") as f:
        r = list(csv.reader(f))
    return r[0], r[1:]
h, rows = load(a.train); th, trows = load(a.test)
rad = lambda r, hh: math.hypot(float(r[hh.index("x1")]), float(r[hh.index("x2")]))
pts = sorted((rad(r, h), r[h.index("label")]) for r in rows)
def knn(x, k=15):
    near = sorted(pts, key=lambda t: abs(t[0] - x))[:k]
    votes = {}
    for _, y in near: votes[y] = votes.get(y, 0) + 1
    return max(sorted(votes), key=votes.get)
with open(a.out, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["prediction"])
    for r in trows: w.writerow([knn(rad(r, th))])
'''

BROKEN = "import sys; sys.exit('boom: ModuleNotFoundError: no module named sklearnx')"


@pytest.fixture
def rings_task(tmp_path):
    out = tmp_path / "rings"
    subprocess.run([sys.executable, str(ROOT / "examples/tasks/make_rings_task.py"), str(out),
                    "--n-train", "400", "--n-test", "300"], check=True, capture_output=True)
    return out


def scripted_llm(req):
    text = req.messages[-1].content
    if "Propose 5 DISTINCT hypotheses" in text:
        return json.dumps({"hypotheses": [
            {"id": "h_radius", "idea": "kNN on radial feature", "rationale": "classes are rings"},
            {"id": "h_noise", "idea": "drop noise columns", "rationale": "n1..n4 are noise"}]})
    if "Rank ALL of these hypotheses" in text:
        return json.dumps({"ranking": ["h_noise", "h_radius"]})  # judge prefers the weaker idea
    if "This script failed" in text:
        return json.dumps({"code": KNN_RADIUS, "notes": "rewrote"})
    if "Implement this hypothesis" in text:
        if "h_noise" in text:
            return json.dumps({"code": BROKEN, "notes": "first try"})
        return json.dumps({"code": KNN_RADIUS, "notes": "radius kNN"})
    raise AssertionError(f"unexpected prompt: {text[:200]}")


def test_research_loop_end_to_end(store, tmp_path, rings_task, monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")  # trusted test code only
    spec = HarnessSpec.model_validate(yaml.safe_load((ROOT / "examples/research_v0.yaml").read_text()))
    llm = MockBackend(scripted_llm)
    eng = Engine(spec, backends={"proposer": llm, "executor": llm, "judge": llm}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True))
    res = eng.run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    s = res.state
    assert s["route"] == "verify"
    # h_noise was tried first (judge ranking), failed, got fixed into a working script
    assert s["attempts"][0]["error"] and s["attempts"][0]["hypothesis_id"] == "h_noise"
    assert s["best"]["dev_score"] > s["baseline_dev"]
    assert s["verdict"]["verified"] is True, s["verdict"]
    assert s["verdict"]["solution_mean"] > s["verdict"]["baseline_mean"]
    assert s["audit"]["passed"], s["audit"]
    assert "[T2_STATISTICAL]" in s["report"]
    # hidden labels never entered any prompt
    for req in llm.requests:
        for m in req.messages:
            assert "test_y" not in m.content and "_private" not in m.content


def test_unisolated_execution_refused_by_default(store, tmp_path, rings_task, monkeypatch):
    monkeypatch.delenv("CHH_ALLOW_UNISOLATED", raising=False)
    spec = HarnessSpec.model_validate(yaml.safe_load((ROOT / "examples/research_v0.yaml").read_text()))
    eng = Engine(spec, backends={"proposer": MockBackend(["{}"])}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True))
    res = eng.run("rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.FAILED and "isolation" in res.detail


def test_private_state_never_renders():
    with pytest.raises(PrivateDataAccess):
        render("{{_private_dev_labels_ref}}", {"_private_dev_labels_ref": "x"})


def test_t2_rules():
    assert t2_verdict([0.9, 0.91, 0.92], [0.7, 0.71, 0.69], higher_is_better=True).verified
    assert not t2_verdict([0.9, 0.91], [0.7, 0.71], higher_is_better=True).verified  # too few seeds
    assert not t2_verdict([0.7, 0.9, 0.5], [0.7, 0.6, 0.8], higher_is_better=True).verified
    assert t2_verdict([1.0, 1.1, 0.9], [2.0, 2.1, 1.9], higher_is_better=False).verified  # rmse-like
    ci = bootstrap_diff_ci([1.0] * 3, [0.0] * 3)
    assert ci.lo == ci.hi == 1.0


def test_bradley_terry_orders_consistent_winner():
    bt = bradley_terry(["a", "b", "c"], rankings_to_wins([["a", "b", "c"], ["a", "c", "b"], ["a", "b", "c"]]))
    assert max(bt, key=bt.get) == "a" and bt["b"] > bt["c"]
