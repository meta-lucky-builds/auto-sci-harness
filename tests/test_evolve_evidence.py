"""M4 pieces of harness evolution that need no real model (GOAL §5 L6 steps 1, 3, 6, 8, 10):

- read-only evidence export for the proposer (evolve split only; fail closed on any planted private byte/canary);
- structured first-divergence diagnosis (deterministic causal signature);
- cost term in the gate (relative cost bound on paired gate units, fail closed without cost data);
- ledger upgrades (per-task flipped/regressed, regressed_unpredicted, yield/prune counters), novelty gate, decline.

Every score and cost comes from fixed tables or hand-built traces: all decisions are deterministic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from custom_harness.evolve import Evaluator, EvolveConfig, RunOutcome, SuiteTask, evolve_generation
from custom_harness.evolve.diagnosis import check_claims, first_divergence
from custom_harness.evolve.edits import Edit, EditOp, EditType
from custom_harness.evolve.export import ExportRefused, export_evidence
from custom_harness.evolve.loop import edit_signature
from custom_harness.evolve.propose import build_prompt, diagnose
from custom_harness.evolve.selection import cost_bound
from custom_harness.gateway import MockBackend
from custom_harness.kernel.ledger import Ledger
from custom_harness.records import Cost, Run, RunStatus, StepKind, TraceStep
from custom_harness.sandbox.snapshot import take_snapshot
from custom_harness.store import EvidenceStore
from test_evolve import PROPOSALS, seed_spec

ONE_EDIT = json.dumps({"edits": [PROPOSALS["edits"][0]]})  # max_fixes 0 -> 2: a valid control-flow edit
CANARY = "CHH-CANARY-" + "ab" * 16
LABELS = [str(i % 3) for i in range(60)]


# ================================================================ fixtures: tasks, runs, traces
def _task_dir(root: Path, name: str, canary: str = CANARY) -> Path:
    d = root / name
    (d / "private").mkdir(parents=True)
    (d / "private" / "test_y.csv").write_text("label\n" + "\n".join(LABELS) + "\n", encoding="utf-8")
    (d / "private" / "canary.txt").write_text(canary + "\n", encoding="utf-8")
    (d / "train.csv").write_text("x,label\n1,0\n", encoding="utf-8")
    return d


def _step(run_id: str, i: int, node: str, out: dict | None = None, error: str | None = None,
          cp: str | None = None) -> TraceStep:
    return TraceStep(run_id=run_id, index=i, node_id=node, kind=StepKind.MODULE, input={"node": {"kind": "module"}},
                     output=out or {}, error=error, cost=Cost(prompt_tokens=10), started_at=0.0, ended_at=1.0,
                     checkpoint_ref=cp)


def _add_run(store, spec, task_id: str, steps: list[tuple], *, seed: int = 0, private_state: dict | None = None,
             status: RunStatus = RunStatus.COMPLETED) -> str:
    """steps: (node, output, error) tuples. `private_state` goes only into the checkpoint and inputs blobs."""
    h = store.add_harness_version(spec.id, spec.model_dump(mode="json"), parent_hash=None)
    state = {"input": {"task_dir": "x"}, **(private_state or {})}
    run = Run(harness_id=spec.id, harness_hash=h, task_id=task_id, seed=seed, inputs_ref=store.blobs.put_json(state))
    store.add_run(run)
    cp = store.blobs.put_json({"state": state, "next": None})
    for i, (node, out, err) in enumerate(steps):
        store.add_step(_step(run.id, i, node, out, err, cp))
    store.set_run_status(run.id, status)
    return run.id


def _outcome(run_id: str, task: str, score: float | None, seed: int = 0, verified: bool = False,
             status: str = "completed") -> RunOutcome:
    return RunOutcome(task, run_id, seed, score, verified, status, Cost(prompt_tokens=10), [])


GOOD_STEPS = [("setup", {"task": "ok"}, None), ("implement", {"code": "print(1)"}, None),
              ("run", {"route": "claim"}, None), ("claim", {}, None)]


@pytest.fixture
def world(store, tmp_path):
    """One evolve task, one gate task (held-out family 'heldfam'), one sealed task; runs on each; gate-split
    ledger details recorded the way the loop records them."""
    spec = seed_spec()
    tasks = [SuiteTask("evo0", _task_dir(tmp_path, "evo0"), "evolve", family="fam"),
             SuiteTask("gatetask7", _task_dir(tmp_path, "gatetask7", "CHH-CANARY-" + "cd" * 16), "gate",
                       family="heldfam"),
             SuiteTask("sealedtask9", _task_dir(tmp_path, "sealedtask9", "CHH-CANARY-" + "ef" * 16), "sealed",
                       family="fam")]
    cert_blob = store.blobs.put_json({"label": "REJECTED", "checks": {
        "K7": {"status": "fail", "reasons": ["success probability without an effect too high"]},
        "K0": {"status": "pass", "reasons": []}}})
    Ledger(store).append("certificate", {"cert_hash": "c" * 64, "claim_hash": "d" * 64, "label": "REJECTED",
                                         "blob": cert_blob, "resolution": {}})
    evo_run = _add_run(store, spec, "evo0", [
        *GOOD_STEPS[:3],
        ("certify", {"certificate": {"hash": "c" * 64, "label": "REJECTED", "failed": ["K7"],
                                     "bundle_path": "C:/data/bundles/cccc.zip", "bundle_hash": "e" * 64}}, None)],
        private_state={"_private_canary": "CHH-CANARY-" + "77" * 16, "_private_task_dir": str(tasks[0].task_dir)})
    gate_run = _add_run(store, spec, "gatetask7", GOOD_STEPS)
    sealed_run = _add_run(store, spec, "sealedtask9", GOOD_STEPS)
    cand = "f" * 64
    store.append_record("lineage", {"parent": spec.spec_hash(), "status": "rejected_gate", "candidate": cand,
                                    "edit": json.loads(ONE_EDIT)["edits"][0] | {"ops": []},
                                    "gate": {"mean_diff": -0.31, "n": 4}, "evolve_fresh": {"mean_diff": 0.2},
                                    "lofo": {"families": {"heldfam": {"passed": False}}},
                                    "decision": "rejected: held-out family heldfam: possible regression"})
    store.append_record("harness_edit", {"parent": spec.spec_hash(), "candidate": cand, "type": "control_flow",
                                         "rationale": "r", "accepted": False,
                                         "decision": "rejected: held-out family heldfam: possible regression",
                                         "actual_gate_delta": -0.31, "actual_evolve_delta": 0.2,
                                         "lofo": {"families": {"heldfam": {}}, "ood_drop": {"family:heldfam": -0.3}},
                                         "per_task": {"gatetask7": {"split": "gate", "mean_diff": -0.3},
                                                      "evo0": {"split": "evolve", "mean_diff": 0.2}},
                                         "regressed": ["gatetask7"], "flipped": ["evo0"],
                                         "regressed_unpredicted": ["gatetask7"], "predicted_delta": 1.0})
    outcomes = [_outcome(evo_run, "evo0", 0.5), _outcome(gate_run, "gatetask7", 1.0),
                _outcome(sealed_run, "sealedtask9", 1.0)]
    return {"spec": spec, "tasks": tasks, "outcomes": outcomes, "evo_run": evo_run, "gate_run": gate_run,
            "sealed_run": sealed_run}


def _all_text(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file())


# ================================================================ 1. evidence export
def test_export_has_evolve_runs_only_and_aggregate_gate_decisions(store, tmp_path, world):
    out = tmp_path / "export"
    res = export_evidence(store, out, runs=world["outcomes"], tasks=world["tasks"])
    text = _all_text(out)
    assert world["evo_run"] in text
    # gate and sealed runs never appear, not even their ids or task ids
    for secret in (world["gate_run"], world["sealed_run"], "gatetask7", "sealedtask9", "heldfam"):
        assert secret not in text, secret
    # gate details stripped from lineage / ledger: only the aggregate decision remains
    for key in ('"gate"', '"lofo"', "actual_gate_delta", "ood_drop", "-0.31"):
        assert key not in text, key
    ledger = json.loads((out / "ledger.json").read_text(encoding="utf-8"))
    assert ledger[-1]["accepted"] is False and ledger[-1]["rejected_on"] == "held_out_gate"
    assert ledger[-1]["flipped"] == ["evo0"] and ledger[-1]["regressed"] == []
    lineage = json.loads((out / "lineage.json").read_text(encoding="utf-8"))
    assert lineage[-1]["status"] == "rejected_gate" and lineage[-1]["evolve_fresh"] == {"mean_diff": 0.2}
    # candidates: spec + edit + parent
    cand = json.loads((out / "candidates" / f"{'f' * 64}.json").read_text(encoding="utf-8"))
    assert cand["parent"] == world["spec"].spec_hash() and cand["edits"]
    parent = json.loads((out / "candidates" / f"{world['spec'].spec_hash()}.json").read_text(encoding="utf-8"))
    assert parent["spec"]["nodes"]["run"]["params"]["max_fixes"] == 0
    # certificate summary: label, failed checks, reasons; never the bundle path
    certs = json.loads((out / "certificates.json").read_text(encoding="utf-8"))
    assert certs[0]["label"] == "REJECTED" and certs[0]["failed"] == ["K7"]
    # verdict embargo (GOAL §16.5.3): reason TEXT quotes held-out statistics, so it is withheld; check ids stay
    assert certs[0]["reasons"]["K7"] == "<embargoed until the target retires>"
    assert "bundle_path" not in text and "bundles/cccc.zip" not in text
    # scores of the exported runs
    scores = json.loads((out / "scores.json").read_text(encoding="utf-8"))
    assert [s["run_id"] for s in scores] == [world["evo_run"]] and scores[0]["score"] == 0.5
    assert res.excluded == {"gate": 1, "sealed": 1} and res.n_runs == 1


def test_export_is_read_only_on_disk(store, tmp_path, world):
    out = tmp_path / "export"
    export_evidence(store, out, runs=world["outcomes"], tasks=world["tasks"])
    files = [p for p in out.rglob("*") if p.is_file()]
    assert files
    for p in files:
        assert not os.access(p, os.W_OK), p
        with pytest.raises(PermissionError):
            p.write_text("tampered", encoding="utf-8")


def test_export_never_writes_private_state_checkpoints_or_workspaces(store, tmp_path, world):
    """Private values in the run's state (checkpoints, inputs blob) and in its workspace are never exported."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text(CANARY, encoding="utf-8")
    (ws / "copy.csv").write_text("label\n" + "\n".join(LABELS) + "\n", encoding="utf-8")
    take_snapshot(ws, store.blobs)  # the workspace snapshot exists in the store; it must not be exported
    # an unscrubbed `_private*` key planted straight into a trace (bypassing the engine's scrubber) is dropped
    rid = _add_run(store, world["spec"], "evo0", [("run", {"_private_note": "TOPSECRETVALUE-123", "route": "x"},
                                                   None)])
    out = tmp_path / "export"
    export_evidence(store, out, runs=[*world["outcomes"], _outcome(rid, "evo0", 0.0, seed=1)], tasks=world["tasks"])
    text = _all_text(out)
    for secret in (CANARY, "CHH-CANARY-" + "77" * 16, "TOPSECRETVALUE-123", "_private", "\n".join(LABELS[:40]),
                   str(world["tasks"][0].task_dir / "private"), "checkpoint_ref", "inputs_ref"):
        assert secret not in text, secret


@pytest.mark.parametrize("plant", ["task_canary_in_output", "private_file_bytes_in_error", "run_canary_in_output",
                                   "label_window_in_output", "private_dir_path_in_output"])
def test_export_refuses_and_deletes_when_a_private_byte_reached_a_trace(store, tmp_path, world, plant):
    task_dir = world["tasks"][0].task_dir
    run_canary = "CHH-CANARY-" + "99" * 16
    labels_text = (task_dir / "private" / "test_y.csv").read_text(encoding="utf-8")
    out, err = {"route": "fix"}, None
    if plant == "task_canary_in_output":
        out["notes"] = f"the token is {CANARY}"
    elif plant == "private_file_bytes_in_error":
        err = "ValueError: could not parse\n" + labels_text
    elif plant == "run_canary_in_output":
        out["notes"] = run_canary  # the run's own canary (held only in private state) reached its trace
    elif plant == "label_window_in_output":
        out["notes"] = ",".join(LABELS[:45])
    else:
        out["notes"] = f"open {task_dir / 'private'}"
    rid = _add_run(store, world["spec"], "evo0", [("run", out, err)], private_state={"_private_canary": run_canary})
    dest = tmp_path / "export"
    with pytest.raises(ExportRefused) as ei:
        export_evidence(store, dest, runs=[*world["outcomes"], _outcome(rid, "evo0", 0.0, seed=2)],
                        tasks=world["tasks"])
    assert ei.value.problems
    assert not any(c in " ".join(ei.value.problems) for c in (CANARY, run_canary))  # locations, never values
    assert not dest.exists() and not list(tmp_path.glob("export*"))  # nothing (partial or final) left behind


def test_export_refuses_a_run_of_an_unknown_task_and_an_existing_destination(store, tmp_path, world):
    with pytest.raises(ExportRefused, match="unknown task"):
        export_evidence(store, tmp_path / "e1", runs=[_outcome(world["evo_run"], "nope", 0.1)], tasks=world["tasks"])
    (tmp_path / "e2").mkdir()
    with pytest.raises(ExportRefused, match="exists"):
        export_evidence(store, tmp_path / "e2", runs=world["outcomes"], tasks=world["tasks"])


# ================================================================ 2. structured diagnosis
def _trace(store, spec, steps, status=RunStatus.COMPLETED):
    rid = _add_run(store, spec, "evo0", steps, status=status)
    return rid, store.trace(rid)


def test_first_divergence_on_an_error(store):
    spec = seed_spec()
    ok_id, ok = _trace(store, spec, GOOD_STEPS)
    bad_id, bad = _trace(store, spec, [GOOD_STEPS[0], GOOD_STEPS[1],
                                       ("run", {"route": "fix", "run_error": "Traceback\nValueError: shape (3, 4)"},
                                        None), ("fix", {}, None), ("report", {}, None)])
    sig = first_divergence(bad, ok, failing_score=0.0)
    assert sig is not None and sig["step"] == 2 and sig["mechanism"] == "error"
    assert sig["node"] == "run" and "ValueError: shape" in sig["cause"] and "<n>" in sig["cause"]
    assert sig["criticality"] == "fatal"
    assert f"{bad_id}#2" in sig["evidence"] and f"{ok_id}#2" in sig["evidence"]


def test_first_divergence_on_a_route_a_path_and_none(store):
    spec = seed_spec()
    _, ok = _trace(store, spec, GOOD_STEPS)
    _, routed = _trace(store, spec, [*GOOD_STEPS[:2], ("run", {"route": "give_up"}, None), ("report", {}, None)])
    sig = first_divergence(routed, ok, failing_score=0.2)
    assert sig["step"] == 2 and sig["mechanism"] == "route" and "give_up" in sig["cause"]
    assert sig["criticality"] == "degrading"
    _, other = _trace(store, spec, [GOOD_STEPS[0], ("profile", {}, None), *GOOD_STEPS[1:]])
    sig = first_divergence(other, ok, failing_score=0.0)
    assert sig["step"] == 1 and sig["mechanism"] == "path"
    _, short = _trace(store, spec, GOOD_STEPS[:2], status=RunStatus.FAILED)
    sig = first_divergence(short, ok, failing_score=0.0)
    assert sig["step"] == 2 and sig["mechanism"] == "truncated"
    _, same = _trace(store, spec, GOOD_STEPS)
    assert first_divergence(same, ok, failing_score=0.0) is None
    # deterministic: same inputs, same signature
    assert first_divergence(routed, ok, failing_score=0.2) == first_divergence(routed, ok, failing_score=0.2)


def test_diagnosis_pairs_runs_of_the_same_task_and_reaches_the_prompt(store):
    spec = seed_spec()
    ok_id, _ = _trace(store, spec, GOOD_STEPS)
    bad_id, _ = _trace(store, spec, [*GOOD_STEPS[:2], ("run", {"route": "give_up"}, None), ("report", {}, None)])
    lone_id, _ = _trace(store, spec, GOOD_STEPS[:2], status=RunStatus.FAILED)
    outcomes = [_outcome(bad_id, "evo0", 0.0), RunOutcome("other", lone_id, 0, 0.0, False, "failed", Cost(), [])]
    references = [_outcome(ok_id, "evo0", 1.0, seed=7, verified=True)]
    digest = diagnose(outcomes, store, references=references)
    divs = digest["divergences"]
    assert len(divs) == 1 and divs[0]["id"] == "D0" and divs[0]["task"] == "evo0"
    assert divs[0]["failing_run"] == bad_id and divs[0]["reference_run"] == ok_id
    assert digest["no_reference"] == ["other"]
    prompt = build_prompt(spec, digest, [], 3, ())
    assert "D0" in prompt and "give_up" in prompt and "first divergence" in prompt.lower()
    edit = Edit(type=EditType.PARAMS, rationale="x", ops=[], evidence=["D0", "C9"])
    edit.predicted.failure_clusters_fixed.extend(["D0", "C7"])
    chk = check_claims(edit, digest)
    assert chk["supported"] == ["D0"] and chk["unsupported"] == ["C7", "C9"] and chk["passed"] is False


# ================================================================ 3-4. cost term, ledger, novelty, decline
class CostTable(Evaluator):
    """Scores and costs from tables: score(arm, task, seed), cost(arm, task, seed) -> Cost. No harness is run."""

    def __init__(self, store, tasks, parent_hash, score, cost):
        super().__init__({}, store, lambda k: None, tasks)
        self.parent_hash, self._score, self._cost = parent_hash, score, cost

    def outcome(self, spec, unit):
        key = (spec.spec_hash(), unit[0], unit[1])
        if key not in self.cache:
            self.calls += 1
            arm = "parent" if spec.spec_hash() == self.parent_hash else "cand"
            score = self._score(arm, unit[0], unit[1])
            # a run that earned nothing carries a failure signature, so the parent's diagnosis has a cluster C0 (the
            # fixtures that cite C0 cite something real: citations are checked, claims.py)
            self.cache[key] = RunOutcome(unit[0], "", unit[1], score, False, "completed",
                                         self._cost(arm, unit[0], unit[1]),
                                         ["run: gave up after a crash"] if score == 0.0 else [])
        return self.cache[key]


def _tasks(tmp_path):
    tasks = [SuiteTask(f"e{i}", tmp_path, "evolve", family="A") for i in range(3)]
    tasks += [SuiteTask(f"g{i}", tmp_path, "gate", family="A") for i in range(4)]
    return tasks + [SuiteTask(f"h{i}", tmp_path, "gate", family="H") for i in range(2)]


def _gain(arm, task, seed, *, regress: str | None = None):
    if arm == "parent":
        return 0.0
    v = 1.0 + 0.001 * (int(task[1:]) + 3 * (seed % 1000))
    return -0.01 - 0.001 * seed if task == regress else v


def _tokens(n):
    return Cost(prompt_tokens=n)


def _gen(store, tmp_path, *, score=_gain, cost=lambda a, t, s: _tokens(100), proposal=ONE_EDIT,
         cfg: EvolveConfig | None = None, evaluator=None, parent=None):
    parent = parent or seed_spec()
    tasks = _tasks(tmp_path)
    ev = evaluator or CostTable(store, tasks, parent.spec_hash(), score, cost)
    res = evolve_generation(parent, tasks, evaluator=ev, proposer=MockBackend([proposal]), store=store,
                            cfg=cfg or EvolveConfig(seeds=(0, 1), n_edits=1, race_rounds=1))
    return res, ev


def test_cost_bound_unit_rules():
    p = {("a", 0): _tokens(100), ("b", 0): _tokens(100)}
    c = {("a", 0): _tokens(150), ("b", 0): _tokens(150)}
    units = list(p)
    r = cost_bound(units, c.get, p.get, max_increase=0.25, metric="tokens")
    assert not r["passed"] and r["ratio"] == pytest.approx(1.5) and "cost" in r["reason"]
    assert cost_bound(units, c.get, p.get, max_increase=0.5, metric="tokens")["passed"]
    r = cost_bound(units, lambda u: Cost(), lambda u: Cost(), max_increase=0.25, metric="tokens")
    assert not r["passed"] and "missing" in r["reason"]
    r = cost_bound(units, lambda u: None, p.get, max_increase=0.25, metric="tokens")
    assert not r["passed"] and "missing" in r["reason"]
    w = cost_bound(units, lambda u: Cost(wall_s=2.0), lambda u: Cost(wall_s=2.0), max_increase=0.0, metric="wall_s")
    assert w["passed"] and w["ratio"] == 1.0
    with pytest.raises(ValueError):
        cost_bound(units, c.get, p.get, max_increase=0.25, metric="bogus")


def test_costlier_candidate_that_otherwise_passes_is_rejected(store, tmp_path):
    cost = lambda arm, t, s: _tokens(300 if arm == "cand" else 100)  # noqa: E731
    res, _ = _gen(store, tmp_path, cost=cost, cfg=EvolveConfig(seeds=(0, 1), n_edits=1, race_rounds=1,
                                                              max_cost_increase=0.5))
    final = [c for c in res.candidates if c["status"] in ("accepted", "rejected_gate")][-1]
    assert final["pooled_gate_accepted"] and final["lofo"]["passed"]  # everything else passed
    assert not res.accepted and "cost" in res.decision
    rec = store.records("harness_edit")[-1]
    assert rec["cost"]["passed"] is False and rec["cost"]["ratio"] == pytest.approx(3.0)
    assert rec["cost"]["metric"] == "tokens" and rec["cost"]["limit"] == pytest.approx(1.5)
    assert rec["rejected_on"] == "cost"


def test_equal_cost_candidate_is_accepted_and_cost_recorded(store, tmp_path):
    res, _ = _gen(store, tmp_path)
    assert res.accepted, res.decision
    rec = store.records("harness_edit")[-1]
    assert rec["cost"]["passed"] is True and rec["cost"]["ratio"] == pytest.approx(1.0)
    assert rec["cost"]["n"] == 12  # the 6 gate tasks x 2 seeds, paired


def test_missing_cost_data_fails_closed(store, tmp_path):
    res, _ = _gen(store, tmp_path, cost=lambda a, t, s: Cost())
    assert not res.accepted and "cost" in res.decision and "missing" in res.decision
    assert store.records("harness_edit")[-1]["cost"]["passed"] is False


def test_ledger_records_per_task_flips_regressions_and_unpredicted(store, tmp_path):
    score = lambda a, t, s: _gain(a, t, s, regress="g2")  # noqa: E731
    _gen(store, tmp_path, score=score)
    rec = store.records("harness_edit")[-1]
    assert rec["regressed"] == ["g2"] and rec["regressed_unpredicted"] == ["g2"]
    assert rec["flipped"] == ["e0", "e1", "e2", "g0", "g1", "g3", "h0", "h1"]
    assert rec["per_task"]["g2"]["split"] == "gate" and rec["per_task"]["g2"]["mean_diff"] < 0
    assert rec["per_task"]["e0"]["split"] == "evolve" and rec["per_task"]["e0"]["n"] == 2
    # predicted at risk -> no longer "unpredicted"
    edit = json.loads(ONE_EDIT)
    edit["edits"][0]["tasks_at_risk"] = ["g2"]
    other = EvidenceStore(tmp_path / "store2")  # a fresh lineage: the novelty gate would refuse the same change
    _gen(other, tmp_path, score=score, proposal=json.dumps(edit))
    assert other.records("harness_edit")[-1]["regressed_unpredicted"] == []
    other.close()


def test_generation_counters_record_yield_and_prune(store, tmp_path):
    res, _ = _gen(store, tmp_path, proposal=json.dumps(PROPOSALS), cfg=EvolveConfig(seeds=(0, 1), n_edits=4,
                                                                                    race_rounds=1))
    c = res.counters
    assert c["proposed"] == 4 and c["rejected_static"] == 2 and c["raced"] == 2
    assert c["pruned"] == 1 and c["gated"] == 1 and c["accepted"] == 1 and c["yield"] == pytest.approx(0.25)
    assert store.records("generation_stats")[-1] == {"parent": res.parent_hash, **c}


def test_novelty_gate_refuses_a_re_proposed_rejected_edit(store, tmp_path):
    cost = lambda arm, t, s: _tokens(300 if arm == "cand" else 100)  # noqa: E731
    first, ev = _gen(store, tmp_path, cost=cost)
    assert not first.accepted  # rejected (cost)
    calls = ev.calls
    second, _ = _gen(store, tmp_path, cost=cost, evaluator=ev)
    statuses = [c["status"] for c in second.candidates]
    assert statuses == ["rejected_novelty"] and ev.calls == calls  # refused before any evaluation
    assert second.candidates[0]["signature"] == edit_signature(Edit.model_validate(first.candidates[-1]["edit"]))
    # a statically rejected edit is refused on re-proposal too
    leak = json.dumps({"edits": [PROPOSALS["edits"][3]]})
    _gen(store, tmp_path, proposal=leak, evaluator=ev)
    third, _ = _gen(store, tmp_path, proposal=leak, evaluator=ev)
    assert [c["status"] for c in third.candidates] == ["rejected_novelty"]


def test_edit_signature_is_normalised():
    a = Edit(type=EditType.PARAMS, rationale="one", ops=[EditOp(op="replace", path="/a", value={"x": 1, "y": 2}),
                                                         EditOp(op="replace", path="/b", value=3)])
    b = Edit(type=EditType.PARAMS, rationale="other words", ops=[EditOp(op="replace", path="/b", value=3),
                                                                 EditOp(op="replace", path="/a",
                                                                        value={"y": 2, "x": 1})])
    assert edit_signature(a) == edit_signature(b)
    c = b.model_copy(update={"type": EditType.HOP})
    assert edit_signature(c) != edit_signature(a)
    d = Edit(type=EditType.PARAMS, rationale="", ops=[EditOp(op="replace", path="/b", value=4),
                                                      EditOp(op="replace", path="/a", value={"y": 2, "x": 1})])
    assert edit_signature(d) != edit_signature(a)


def test_proposer_may_decline_with_a_reason(store, tmp_path):
    res, _ = _gen(store, tmp_path, proposal=json.dumps({"edits": [], "decline": "no trace supports a change"}))
    assert not res.accepted and "declined" in res.decision and "no trace supports" in res.decision
    assert [c["status"] for c in res.candidates] == ["declined"]
    lin = store.records("lineage")[-1]
    assert lin["status"] == "declined" and lin["reason"] == "no trace supports a change"
    assert res.counters["declined"] == 1 and res.counters["proposed"] == 0


def test_proposer_prompt_never_shows_gate_details(store, tmp_path):
    score = lambda a, t, s: _gain(a, t, s, regress="h1")  # noqa: E731
    _, ev = _gen(store, tmp_path, score=score)
    assert store.records("harness_edit")[-1]["lofo"]  # the ledger holds the gate details...
    proposer = MockBackend([json.dumps({"edits": [], "decline": "x"})])
    evolve_generation(seed_spec(), _tasks(tmp_path), evaluator=ev, proposer=proposer, store=store,
                      cfg=EvolveConfig(seeds=(0, 1), n_edits=1, race_rounds=1))
    prompt = proposer.requests[0].messages[0].content
    past = prompt.split("PAST EDITS")[1]
    assert "control_flow" in past  # ...the proposer still learns the edit and its aggregate outcome
    for leak in ('"lofo"', "actual_gate_delta", "ood_drop", '"h1"', '"g0"', "family:H", "held-out family H"):
        assert leak not in past, leak
