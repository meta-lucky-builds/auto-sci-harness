"""GOAL §5 L6 steps 2, 6, 7 at the loop level (any proposer): typed edits only, the prediction screen (covert channel
through rationale / predicted effect), one mechanism per candidate, and the smoke run before racing.

These tests drive `evolve_generation` through its public API only, so on the code before these rules they fail on
their assertions (the attack is carried out and goes through), not on a missing import.
"""

from __future__ import annotations

import json

from custom_harness.evolve import Evaluator, EvolveConfig, RunOutcome, evolve_generation
from custom_harness.gateway import MockBackend
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore
from test_evolve import PROPOSALS, declared_seed_spec, executor, seed_spec, suite  # noqa: F401  (suite is a fixture)
from test_evolve_evidence import CostTable, _gain, _tasks, _tokens

CANARY = "CHH-CANARY-" + "5a" * 16


def _item(path: str, value, *, rationale: str = "allow fix attempts", **kw) -> dict:
    return {"type": "control_flow", "ops": [{"op": "replace", "path": path, "value_json": json.dumps(value)}],
            "rationale": rationale, "evidence": kw.pop("evidence", ["C0"]),
            "failure_clusters_fixed": kw.pop("clusters", ["C0"]), "expected_delta": kw.pop("delta", 1.0),
            "tasks_at_risk": kw.pop("at_risk", []), **kw}


def _run(store, tmp_path, proposal: dict | str, *, evaluator=None, cfg=None, parent=None):
    parent = parent or seed_spec()
    tasks = _tasks(tmp_path)
    ev = evaluator or CostTable(store, tasks, parent.spec_hash(), _gain, lambda a, t, s: _tokens(100))
    text = proposal if isinstance(proposal, str) else json.dumps(proposal)
    res = evolve_generation(parent, tasks, evaluator=ev, proposer=MockBackend([text]), store=store,
                            cfg=cfg or EvolveConfig(seeds=(0, 1), n_edits=6, race_rounds=1))
    return res, ev


def _statuses(store) -> list[str]:
    return [r["status"] for r in store.records("lineage")]


# ================================================================ typed edits only
def test_non_typed_edits_are_malformed_and_never_raced(store, tmp_path):
    good = PROPOSALS["edits"][0]
    attacks = [
        {**good, "patch": "--- a/spec.yaml\n+++ b/spec.yaml\n-max_fixes: 0\n+max_fixes: 9"},  # free-form patch rider
        {**good, "type": "freeform"},                                                         # untyped edit type
        {**good, "ops": [{"op": "move", "path": "/nodes/run/params/max_fixes", "value_json": "2"}]},
        {**good, "ops": [{"op": "replace", "path": "/nodes/run/params/max_fixes", "value_json": "2",
                          "from": "/nodes/claim/params/seeds"}]},                              # op with extra field
    ]
    res, _ = _run(store, tmp_path, {"edits": attacks})
    assert any("patch" in json.dumps(a) for a in attacks)  # the attack was sent
    assert res.counters["malformed"] == 4 and res.counters["raced"] == 0, res.counters
    assert not res.accepted


# ================================================================ prediction screen (covert channel)
def test_exfiltration_through_rationale_evidence_ops_and_prediction_is_refused_or_harmless(store, tmp_path):
    edits = [
        _item("/nodes/run/params/max_fixes", 2, rationale="regressions expected on g2 (held-out)"),
        _item("/nodes/run/params/max_fixes", 2, evidence=[CANARY]),
        _item("/nodes/fix/system", "Remember task g3 when fixing."),
        _item("/nodes/run/params/max_fixes", 2, clusters=["C0", "x:aGVsZC1vdXQ="], delta=0.123456789012,
              at_risk=["e1", "free text: gate mean was -0.31"]),
    ]
    res, _ = _run(store, tmp_path, {"edits": edits})
    lineage = store.records("lineage")
    st = _statuses(store)
    assert st[:3] == ["rejected_static"] * 3, st
    for r in lineage[:3]:
        assert "content withheld" in r["reason"]
        assert r["edit"]["rationale"] == "<withheld>"
    blob = json.dumps(lineage)
    for secret in ("g2 (held-out)", CANARY, "task g3", "aGVsZC1vdXQ=", "gate mean was", "0.123456789"):
        assert secret not in blob, secret
    # the fourth edit survives, sanitised: diagnosis ids only, campaign task ids only, a rounded bounded delta
    rec = store.records("harness_edit")[-1]
    assert rec["clusters_claimed"] == ["C0"] and rec["tasks_at_risk"] == ["e1"]
    assert rec["predicted_delta"] == 0.123
    assert res.accepted
    later = [r for r in lineage if r.get("screen")]
    assert later and any("dropped" in n for n in later[0]["screen"])


def test_unbounded_delta_is_clipped_and_non_finite_text_never_stored(store, tmp_path):
    _run(store, tmp_path, {"edits": [_item("/nodes/run/params/max_fixes", 2, delta=1e9,
                                         rationale="x" * 5000)]})
    rec = store.records("harness_edit")[-1]
    assert rec["predicted_delta"] == 2.0 and len(rec["rationale"]) == 2000


def test_decline_text_naming_held_out_data_is_withheld(store, tmp_path):
    _run(store, tmp_path, {"edits": [], "decline": "nothing to do; h1 and g0 look hard"})
    rec = next(r for r in store.records("lineage") if r["status"] == "declined")
    assert "g0" not in rec["reason"] and "withheld" in rec["reason"]


def test_a_screened_edit_does_not_poison_the_novelty_gate(store, tmp_path):
    _run(store, tmp_path, {"edits": [_item("/nodes/run/params/max_fixes", 2, rationale="see g1")]})
    assert _statuses(store) == ["rejected_static"]
    res, _ = _run(store, tmp_path, {"edits": [_item("/nodes/run/params/max_fixes", 2)]})
    assert res.accepted and "rejected_novelty" not in _statuses(store)


# ================================================================ one mechanism per candidate
def test_mechanisms_are_defined_on_the_effect_of_the_ops():
    from custom_harness.evolve.edits import Edit, mechanisms
    spec = seed_spec()
    idx = next(i for i, e in enumerate(spec.edges) if e.source == "fix" and e.when is None)

    def m(*ops):
        return mechanisms(Edit.model_validate({"type": "control_flow", "rationale": "r", "ops": list(ops)}), spec)

    one = {"op": "replace", "path": "/nodes/run/params/max_fixes", "value": 2}
    assert m(one) == [["run"]]
    assert m(one, {"op": "replace", "path": "/nodes/run/params/max_improves", "value": 3}) == [["run"]]
    assert m(one, {"op": "replace", "path": "/nodes/next/params/max_improves", "value": 3}) == [["next"], ["run"]]
    assert m(one, {"op": "replace", "path": "/max_steps", "value": 9}) == [["run"], ["spec:max_steps"]]
    # inserting a step after `fix`: the new node, its wiring and fix's routing are one mechanism
    insert = [{"op": "add", "path": "/nodes/lint", "value": {"kind": "llm", "role": "executor", "prompt": "p",
                                                               "output_key": "lint_notes"}},
              {"op": "replace", "path": f"/edges/{idx}", "value": {"source": "fix", "target": "lint", "when": None}},
              {"op": "add", "path": "/edges/-", "value": {"source": "lint", "target": "run", "when": None}}]
    assert m(*insert) == [["fix", "lint"]]
    assert len(m(*insert, one)) == 2
    # re-routing one node's edge is that node's mechanism
    assert m({"op": "replace", "path": f"/edges/{idx}/target", "value": "next"}) == [["fix"]]
    assert m({"op": "replace", "path": "/nodes/nope/x", "value": 1}) is None  # unappliable: left to apply_edit


def test_multi_mechanism_bundle_is_refused_and_single_ones_pass(store, tmp_path):
    two = {"type": "control_flow", "rationale": "two things", "evidence": [], "failure_clusters_fixed": [],
           "expected_delta": 1.0, "tasks_at_risk": [],
           "ops": [{"op": "replace", "path": "/nodes/run/params/max_fixes", "value_json": "2"},
                   {"op": "replace", "path": "/nodes/next/params/max_improves", "value_json": "3"}]}
    res, _ = _run(store, tmp_path, {"edits": [two]})
    rec = store.records("lineage")[0]
    assert rec["status"] == "rejected_static" and "one mechanism per candidate" in rec["reason"], rec
    assert res.counters["raced"] == 0 and not res.accepted
    other = EvidenceStore(tmp_path / "s2")
    res2, _ = _run(other, tmp_path, {"edits": [_item("/nodes/run/params/max_fixes", 2)]})
    other.close()
    assert res2.accepted


# ================================================================ smoke run
class SmokeTable(CostTable):
    """CostTable whose candidates with run.max_improves == 7 break (status `status`) on every unit."""

    def __init__(self, *a, status: str = "failed", **kw):
        super().__init__(*a, **kw)
        self.status, self.by_spec = status, {}

    def outcome(self, spec, unit):
        key = (spec.spec_hash(), unit[0], unit[1])
        if key not in self.cache and spec.nodes["run"].params.get("max_improves") == 7:
            self.calls += 1
            self.by_spec[spec.spec_hash()] = self.by_spec.get(spec.spec_hash(), 0) + 1
            self.cache[key] = RunOutcome(unit[0], "", unit[1], None if self.status == "infra_failed" else 0.0,
                                         False, self.status, _tokens(100), [f"run: {self.status}: boom"])
        return super().outcome(spec, unit)


BAD = _item("/nodes/run/params/max_improves", 7, rationale="more improves")


def _smoke_eval(store, tmp_path, status="failed"):
    return SmokeTable(store, _tasks(tmp_path), seed_spec().spec_hash(), _gain, lambda a, t, s: _tokens(100),
                      status=status)


def test_candidate_failing_smoke_is_rejected_cheaply_and_ledgered(store, tmp_path):
    ev = _smoke_eval(store, tmp_path)
    res, _ = _run(store, tmp_path, {"edits": [BAD, _item("/nodes/run/params/max_fixes", 2)]}, evaluator=ev)
    rec = next(r for r in store.records("lineage") if r["edit"]["rationale"] == "more improves")
    assert rec["status"] == "rejected_smoke", rec["status"]
    assert "failed" in rec["reason"] and rec["infra"] is False and rec["smoke"]["task"] == "e0"
    assert ev.by_spec == {rec["candidate"]: 1}  # one run, then dropped: never raced, re-measured or gated
    assert res.counters["rejected_smoke"] == 1 and res.counters["raced"] == 1
    assert res.accepted  # the healthy candidate still goes through race and gate
    assert store.records("generation_stats")[-1]["rejected_smoke"] == 1
    # the novelty gate refuses the broken change next time
    _run(store, tmp_path, {"edits": [BAD]}, evaluator=ev)
    assert _statuses(store)[-1] == "rejected_novelty"


def test_infra_smoke_failure_is_rejected_but_not_novelty_blocked(store, tmp_path):
    ev = _smoke_eval(store, tmp_path, status="infra_failed")
    _run(store, tmp_path, {"edits": [BAD]}, evaluator=ev)
    rec = store.records("lineage")[-1]
    assert rec["status"] == "rejected_smoke" and rec["infra"] is True
    _run(store, tmp_path, {"edits": [BAD]}, evaluator=ev)
    assert _statuses(store)[-1] == "rejected_smoke"  # tried again (an outage says nothing about the edit)


def test_smoke_runs_one_task_through_the_real_loop(store, tmp_path, suite, monkeypatch):  # noqa: F811
    """Real Engine, real research modules, scripted models: a candidate whose step limit cuts every run short ends
    STEP_LIMIT on its smoke task (the parent completes) and is rejected after exactly one run."""
    llm = MockBackend(executor)
    factory = lambda key: LocalSandbox(tmp_path / "sb" / key, i_understand_no_isolation=True)  # noqa: E731
    ev = Evaluator({"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store, factory, suite)
    cfg = EvolveConfig(n_edits=1, race_rounds=1)
    parent = declared_seed_spec(cfg)
    edit = {"type": "budget_policy", "rationale": "shorter runs", "evidence": [], "failure_clusters_fixed": [],
            "expected_delta": 0.1, "tasks_at_risk": [],
            "ops": [{"op": "replace", "path": "/max_steps", "value_json": "4"}]}
    res = evolve_generation(parent, suite, evaluator=ev, proposer=MockBackend([json.dumps({"edits": [edit]})]),
                            store=store, cfg=cfg)
    rec = store.records("lineage")[-1]
    assert rec["status"] == "rejected_smoke" and rec["smoke"]["status"] == "step_limit", rec
    cand_runs = [k for k in ev.cache if k[0] == rec["candidate"]]
    assert cand_runs == [(rec["candidate"], "rings0", 0)]
    assert not res.accepted and res.counters["raced"] == 0




def test_typed_edit_fields_match_the_schema():
    from custom_harness.evolve.propose import _ITEM_KEYS, _OP_KEYS, EDIT_SCHEMA
    item = EDIT_SCHEMA["properties"]["edits"]["items"]
    assert set(item["properties"]) == _ITEM_KEYS and set(item["properties"]["ops"]["items"]["properties"]) == _OP_KEYS
