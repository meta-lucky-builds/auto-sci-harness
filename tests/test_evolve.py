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
from test_research import BROKEN, KNN_RADIUS, KNN_REPLICA, critic_answers

ROOT = Path(__file__).resolve().parents[1]


def seed_spec() -> HarnessSpec:
    spec = HarnessSpec.model_validate(yaml.safe_load((ROOT / "examples/research_v1.yaml").read_text()))
    doc = spec.model_dump(mode="json")
    doc["nodes"]["run"]["params"]["max_fixes"] = 0  # the defect evolution should discover
    return HarnessSpec.model_validate(doc)


def declared_seed_spec(cfg: EvolveConfig, generations: int = 1) -> HarnessSpec:
    """seed_spec() with the campaign's multiplicity bound pre-declared on its (protected) claim nodes (R2-2): set by
    the human author of a campaign's seed; the driver checks the same bound (check_campaign), the Evaluator enforces
    it after every run."""
    from custom_harness.evolve.multiplicity import runs_per_task_bound
    per_task = runs_per_task_bound(generations=generations, n_edits=cfg.n_edits, n_seeds=len(cfg.seeds),
                                   n_fresh_seeds=len(cfg.seeds), infra_retries=cfg.infra_retries)
    doc = seed_spec().model_dump(mode="json")
    for nid in ("claim", "replica_claim"):
        doc["nodes"][nid]["params"]["n_comparisons"] = 2 * per_task
    return HarnessSpec.model_validate(doc)


def executor(req):
    text = req.messages[-1].content
    if "Answer every critic question" in text:
        return critic_answers(text)
    if "Propose 5 DISTINCT hypotheses" in text:
        return json.dumps({"hypotheses": [{"id": "h1", "idea": "radius kNN", "rationale": "rings"},
                                          {"id": "h2", "idea": "radius kNN, k=25", "rationale": "smoother"}]})
    if "Rank ALL" in text:
        return json.dumps({"ranking": ["h1", "h2"]})
    if "Re-implement the method described below from scratch" in text:
        return json.dumps({"code": KNN_REPLICA, "notes": "replica from the description"})
    if "Ask at most 5 short questions" in text:
        return json.dumps({"questions": ["What would falsify the radius hypothesis?", "Use a random forest.",
                                         "Is the color column leaking the label?"]})
    if "This script works and is the best so far" in text:
        return json.dumps({"code": KNN_RADIUS, "notes": "no real change"})
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
    {"type": "params", "ops": [{"op": "replace", "path": "/nodes/claim/params/seeds", "value_json": "[0]"}],
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
        # the gate tasks form their own family, held out of evolution (leave-one-family-out gate, invariant 16)
        tasks.append(SuiteTask(f"rings{i}", d, split, family="rings" if split == "evolve" else "rings-heldout"))
    return tasks


def test_generation_finds_structural_fix_and_blocks_unsafe_edits(store, tmp_path, suite, monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    llm = MockBackend(executor)
    factory = lambda key: LocalSandbox(tmp_path / "sb" / key, i_understand_no_isolation=True)  # noqa: E731
    ev = Evaluator({"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store, factory, suite)
    proposer = MockBackend([json.dumps(PROPOSALS)])
    cfg = EvolveConfig(n_edits=4, race_rounds=2)
    parent = declared_seed_spec(cfg)
    from custom_harness.evolve.multiplicity import check_campaign
    per_task = parent.nodes["claim"].params["n_comparisons"] // 2
    assert check_campaign(parent, store, [t.task_dir for t in suite], runs_per_task=per_task) == 2 * per_task

    res = evolve_generation(parent, suite, evaluator=ev, proposer=proposer, store=store, cfg=cfg)

    assert res.accepted, res.decision
    assert res.new_parent.nodes["run"].params["max_fixes"] == 2
    # GOAL §6.1 / invariant 10: internal gate data -> MECHANICAL, labelled in the result and in the ledger
    assert res.loop_label == "MECHANICAL"
    assert [r["loop_label"] for r in store.records("harness_edit")][-1:] == ["MECHANICAL"]
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
    i = next(i for i, e in enumerate(spec.edges) if e.source == "report")
    with pytest.raises(EditRejected, match=r"end only at .audit.|.audit. became unreachable"):
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


def test_edits_cannot_fake_kernel_state_or_duplicate_protected_modules():
    """An evolved harness must not be able to write the state that scoring and reporting trust, nor run a
    second copy of a protected step outside its protected position."""
    spec = seed_spec()
    # The sneak node is WIRED into the graph (hypothesize -> sneak -> tournament), so the only possible reason to
    # refuse it is the key it writes; an unwired node would be refused anyway (not a terminal), proving nothing.
    i = next(n for n, e in enumerate(spec.model_dump(mode="json")["edges"]) if e["source"] == "hypothesize")

    def sneak(key: str) -> Edit:
        return Edit(type=EditType.HOP, rationale="x", ops=[
            EditOp(op="add", path="/nodes/sneak", value={"kind": "llm", "prompt": "p", "output_key": key}),
            EditOp(op="replace", path=f"/edges/{i}/target", value="sneak"),
            EditOp(op="add", path="/edges/-", value={"source": "sneak", "target": "tournament"})])

    apply_edit(spec, sneak("notes"))  # positive control: the same wiring with a harmless key is accepted
    # dev_folds: an added node replacing the folds (e.g. with training files that contain the validation rows)
    # would let candidates score themselves perfectly on dev; dev_scheme is reported in the checklist
    for key in ("certificate", "claim", "best", "audit", "_private_canary", "dev_folds", "dev_scheme"):
        with pytest.raises(EditRejected, match=r"reserved state key|private data"):
            apply_edit(spec, sneak(key))
    with pytest.raises(EditRejected, match="protected module"):
        apply_edit(spec, Edit(type=EditType.HOP, rationale="x", ops=[
            EditOp(op="add", path="/nodes/certify2", value={"kind": "module", "module": "research.certify"})]))
    for nid in ("claim", "confirm", "certify", "report"):
        with pytest.raises(EditRejected, match="protected"):
            apply_edit(spec, Edit(type=EditType.PARAMS, rationale="x", ops=[
                EditOp(op="replace", path=f"/nodes/{nid}/params", value={"seeds": [0]})]))
