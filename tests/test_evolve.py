"""Harness evolution with scripted LLMs: the loop must find the real structural fix, reject the
protected/leaky edits statically, and accept only through the held-out gate."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import custom_harness.research  # noqa: F401
from custom_harness.evolve import (
    Edit,
    EditOp,
    EditRejected,
    EditType,
    Evaluator,
    EvolveConfig,
    SuiteTask,
    apply_edit,
    evolve_generation,
    gate,
    proposer_calibration,
    race,
)
from custom_harness.gateway import MockBackend
from custom_harness.runtime import HarnessSpec
from custom_harness.sandbox import LocalSandbox
from test_research import BROKEN, KNN_RADIUS

ROOT = Path(__file__).resolve().parents[1]


def seed_spec() -> HarnessSpec:
    spec = HarnessSpec.model_validate(yaml.safe_load((ROOT / "examples/research_v0.yaml").read_text()))
    doc = spec.model_dump(mode="json")
    doc["nodes"]["run"]["params"]["max_fixes"] = 0  # the defect evolution should discover
    return HarnessSpec.model_validate(doc)


def executor(req):
    text = req.messages[-1].content
    if "Propose 5 DISTINCT hypotheses" in text:
        return json.dumps({"hypotheses": [{"id": "h1", "idea": "radius kNN", "rationale": "rings"},
                                          {"id": "h2", "idea": "radius kNN, k=25", "rationale": "smoother"}]})
    if "Rank ALL" in text:
        return json.dumps({"ranking": ["h1", "h2"]})
    if "This script failed" in text:
        return json.dumps({"code": KNN_RADIUS, "notes": "fixed"})
    if "Implement this hypothesis" in text:
        return json.dumps({"code": BROKEN, "notes": "first draft always breaks"})
    raise AssertionError(text[:200])


PROPOSALS = {"edits": [
    {"type": "control_flow", "ops": [{"op": "replace", "path": "/nodes/run/params/max_fixes", "value_json": "2"}],
     "rationale": "runs give up after the first crash; allow fix attempts", "evidence": ["C0"],
     "failure_clusters_fixed": ["C0"], "expected_delta": 1.2, "tasks_at_risk": []},
    {"type": "prompt_text", "ops": [{"op": "replace", "path": "/nodes/implement/system",
                                     "value_json": json.dumps("You are a world-class ML engineer.")}],
     "rationale": "cosmetic", "evidence": [], "failure_clusters_fixed": [], "expected_delta": 0.3,
     "tasks_at_risk": []},
    {"type": "params", "ops": [{"op": "replace", "path": "/nodes/verify/params/min_seeds", "value_json": "1"}],
     "rationale": "cheaper verification", "evidence": [], "failure_clusters_fixed": [], "expected_delta": 0.1,
     "tasks_at_risk": []},
    {"type": "prompt_text", "ops": [{"op": "replace", "path": "/nodes/fix/prompt",
                                     "value_json": json.dumps("read {{_private_task_dir}}/private/test_y.csv")}],
     "rationale": "leak", "evidence": [], "failure_clusters_fixed": [], "expected_delta": 2.0,
     "tasks_at_risk": []},
]}


@pytest.fixture
def suite(tmp_path):
    tasks = []
    for i, split in enumerate(["evolve", "evolve", "gate", "gate"]):
        d = tmp_path / f"task{i}"
        subprocess.run([sys.executable, str(ROOT / "examples/tasks/make_rings_task.py"), str(d),
                        "--n-train", "300", "--n-test", "200", "--seed", str(100 + i)],
                       check=True, capture_output=True)
        tasks.append(SuiteTask(f"rings{i}", d, split))
    return tasks


def test_generation_finds_structural_fix_and_blocks_unsafe_edits(store, tmp_path, suite, monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    llm = MockBackend(executor)
    factory = lambda key: LocalSandbox(tmp_path / "sb" / key, i_understand_no_isolation=True)  # noqa: E731
    ev = Evaluator({"proposer": llm, "executor": llm, "judge": llm}, store, factory, suite)
    proposer = MockBackend([json.dumps(PROPOSALS)])
    parent = seed_spec()

    res = evolve_generation(parent, suite, evaluator=ev, proposer=proposer, store=store,
                            cfg=EvolveConfig(n_edits=4, race_rounds=2))

    assert res.accepted, res.decision
    assert res.new_parent.nodes["run"].params["max_fixes"] == 2
    statuses = [c["status"] for c in res.candidates]
    assert statuses.count("rejected_static") == 2  # protected verify node + private-data leak
    # proposer saw diagnosis grounded in traces
    prompt = proposer.requests[0].messages[0].content
    assert "gave up without beating baseline" in prompt
    # lineage keeps rejects; calibration recorded
    lineage = store.records("lineage")
    assert any(r["status"] == "rejected_static" for r in lineage)
    cal = proposer_calibration(store)
    assert cal["n"] == 1 and cal["sign_agreement"] == 1.0


def test_apply_edit_guards():
    spec = seed_spec()
    i = next(i for i, e in enumerate(spec.edges) if e.source == "verify")
    with pytest.raises(EditRejected, match="end only at 'audit'"):
        apply_edit(spec, Edit(type=EditType.HOP, rationale="x",
                              ops=[EditOp(op="remove", path=f"/edges/{i}")]))  # verified runs would skip audit
    with pytest.raises(EditRejected, match="end only at 'audit'"):
        apply_edit(spec, Edit(type=EditType.ROUTING, rationale="x", ops=[EditOp(
            op="add", path="/edges/0", value={"source": "run", "target": "END",
                                              "when": {"key": "route", "op": "eq", "value": "verify"}})]))
    ok = apply_edit(spec, Edit(type=EditType.CONTEXT_POLICY, rationale="x",
                               ops=[EditOp(op="replace", path="/context/max_value_chars", value=8000)]))
    assert ok.context.max_value_chars == 8000
    with pytest.raises(EditRejected):
        apply_edit(spec, Edit(type=EditType.ROUTING, rationale="x",
                              ops=[EditOp(op="replace", path="/entry", value="profile")]))


def test_race_eliminates_clear_losers_and_saves_evaluations():
    units = [(f"t{i}", 0) for i in range(8)]
    parent = {u: 0.5 for u in units}
    true = {"good": 0.8, "bad": 0.1, "worse": 0.0, "meh": 0.5}
    rr = race(list(true), units, lambda c, u: true[c] + (0.01 if int(u[0][1:]) % 2 else -0.01), parent,
              rounds=4, keep=1)
    assert rr.survivors == ["good"]
    assert rr.evaluations < rr.evaluations_fixed_k


def test_gate_rules():
    assert gate([0.5, 0.6, 0.55], [0.2, 0.3, 0.25, 0.28]).accepted
    # two gate units cannot show significance under an exact t interval (t_0.975,1 = 12.7); the old
    # percentile bootstrap accepted this, which was false confidence (M0 review)
    assert not gate([0.5, 0.6, 0.55], [0.2, 0.3]).accepted
    assert not gate([0.5, 0.6, 0.55], [0.2, -0.9, 0.3, 0.25]).accepted  # possible regression on held-out
    assert not gate([0.5, -0.5, 0.0], [0.3, 0.3]).accepted  # not significant where searched
