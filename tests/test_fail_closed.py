"""Fail-closed fuzzing of every parser and gate that decides something (GOAL §7, invariant 7), plus the
remaining M0-applicable §7 checks: injected fake numbers are blocked; evolve/gate splits cannot overlap.

"Fail closed" here means: arbitrary/garbage input never produces an acceptance, an edit, a ranking,
a credit or a validated value; it produces a rejection or a recorded failure.
"""

import json
import math
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import custom_harness.research  # noqa: F401
from custom_harness.evolve import EvolveConfig, Evaluator, evolve_generation, gate, score_state
from custom_harness.evolve.edits import Edit
from custom_harness.evolve.propose import propose_edits
from custom_harness.evolve.selection import PairedSample
from custom_harness.gateway import MockBackend
from custom_harness.records import Evidence, EvidenceKind, RunStatus
from custom_harness.records import Claim
from custom_harness.research.modules import tournament
from custom_harness.runtime import Engine, HarnessSpec, LLMNode, register
from custom_harness.runtime.modules import ModuleContext
from custom_harness.sandbox import LocalSandbox
from custom_harness.tiers import Tier
from test_evolve import seed_spec
from test_research import ROOT, rings_task, scripted_llm  # noqa: F401

FUZZ = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True, allow_infinity=True) | st.text(max_size=20),
    lambda kids: st.lists(kids, max_size=4) | st.dictionaries(st.text(max_size=10), kids, max_size=4),
    max_leaves=20)
garbage_text = st.one_of(st.text(max_size=300), json_values.map(lambda v: json.dumps(v, default=str)))


# ---------------------------------------------------------------- the acceptance gate
@FUZZ
@given(st.lists(st.floats(allow_nan=True, allow_infinity=True), max_size=12),
       st.lists(st.floats(allow_nan=True, allow_infinity=True), max_size=12),
       st.integers(0, 6), st.integers(0, 6))
def test_gate_accepts_only_when_every_rule_holds(ev, gt, ev_excl, gt_excl):
    evs = PairedSample([("e", i) for i in range(len(ev) + ev_excl)], ev, [("x", i) for i in range(ev_excl)])
    gts = PairedSample([("g", i) for i in range(len(gt) + gt_excl)], gt, [("y", i) for i in range(gt_excl)])
    d = gate(evs, gts, n_boot=200)
    if d.accepted:
        assert all(math.isfinite(x) for x in ev + gt)
        assert len(ev) >= 2 and len(gt) >= 2
        assert evs.excluded_frac <= 0.25 and gts.excluded_frac <= 0.25
        assert d.evolve["lo"] > 0 and d.gate["mean_diff"] > 0 and d.gate["lo"] >= 0


# ---------------------------------------------------------------- the run score
@FUZZ
@given(json_values, st.sampled_from(list(RunStatus)))
def test_score_state_never_raises_and_never_credits_garbage(state, status):
    state = state if isinstance(state, dict) else {"junk": state}
    score, verified = score_state(state, status)
    if status == RunStatus.INFRA_FAILED:
        assert score is None and verified is False
        return
    assert isinstance(score, float) and math.isfinite(score) and -0.5 <= score <= 1.5
    audit = state.get("audit")
    if status != RunStatus.COMPLETED or not isinstance(audit, dict) or audit.get("passed") is not True:
        assert score == 0.0 and verified is False
    if verified:
        assert state["verdict"]["verified"] is True


# ---------------------------------------------------------------- the edit proposer parser
@FUZZ
@given(garbage_text)
def test_proposer_garbage_never_becomes_an_edit(text):
    edits, problems, _ = propose_edits(MockBackend([text]), seed_spec(), {"clusters": []}, [], n_edits=3,
                                       protected=("setup", "verify", "audit"))
    for e in edits:
        assert isinstance(e, Edit)
        Edit.model_validate(e.model_dump())  # only fully valid typed edits come out


# ---------------------------------------------------------------- engine output contracts
SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]}


@FUZZ
@given(garbage_text)
def test_contract_only_admits_schema_valid_values(tmp_path_factory, text):
    from custom_harness.store import EvidenceStore
    import jsonschema
    store = EvidenceStore(tmp_path_factory.mktemp("s"))
    spec = HarnessSpec(id="c", entry="a", nodes={"a": LLMNode(prompt="q", output_key="v", output_schema=SCHEMA)})
    res = Engine(spec, backends={"executor": MockBackend([text])}, store=store).run("t", {})
    if res.status == RunStatus.COMPLETED:
        jsonschema.validate(res.state["v"], SCHEMA)
    else:
        assert res.status == RunStatus.FAILED and "ContractError" in res.detail
    store.close()


# ---------------------------------------------------------------- judge ranking parser
@FUZZ
@given(st.lists(garbage_text, min_size=3, max_size=3))
def test_tournament_never_guesses_a_ranking(tmp_path_factory, responses):
    from custom_harness.store import EvidenceStore
    store = EvidenceStore(tmp_path_factory.mktemp("s"))
    ctx = ModuleContext(run_id="r", store=store, backends={"judge": MockBackend(responses)},
                        budget=__import__("custom_harness.budget", fromlist=["Budget"]).Budget(), sandbox=None)
    hyps = [{"id": "a", "idea": "x"}, {"id": "b", "idea": "y"}, {"id": "c", "idea": "z"}]
    state = {"hypotheses": {"hypotheses": hyps}, "task": {"description": "d", "metric": "accuracy"},
             "profile": {}, "_seed": 0}
    valid = []
    for r in responses:
        try:
            ranking = json.loads(r.strip())["ranking"]
            if isinstance(ranking, list) and sorted(map(str, ranking)) == ["a", "b", "c"] and \
                    all(isinstance(x, str) for x in ranking):
                valid.append(ranking)
        except Exception:
            pass
    try:
        out = tournament(state, {"rounds": 3}, ctx)
    except RuntimeError as e:
        assert "no valid rankings" in str(e) and not valid
    except (TypeError, AttributeError):
        pytest.fail("tournament crashed on a malformed judge response instead of rejecting the round")
    else:
        assert out["ranking"]["valid_rounds"] == len(valid) >= 1
    store.close()


# ---------------------------------------------------------------- §7: fake number injected -> blocked
@register("test.planted_fake_number")
def _fake_number(state, params, ctx):
    """A fabricated result: a metric evidence record whose reported score was never computed."""
    real = ctx.store.get_claim(state["claims"][0])
    ev = ctx.store.get_evidence(next(e for e in real.evidence_ids
                                     if ctx.store.get_evidence(e).kind == EvidenceKind.METRIC))
    payload = ctx.store.blobs.get_json(ev.payload_ref)
    payload["score"] = payload["score"] + 0.123  # the fabricated number
    fake = Evidence(kind=EvidenceKind.METRIC, tier=Tier.T1_EXECUTABLE, verifier_id=ev.verifier_id,
                    produced_by_run=ctx.run_id, payload_ref=ctx.store.blobs.put_json(payload), summary="fake")
    ctx.store.add_evidence(fake)
    claim = Claim(text="fabricated improvement", evidence_ids=(fake.id,))
    ctx.store.add_claim(claim)
    return {"claims": list(state["claims"]) + [claim.id]}


def test_injected_fake_number_is_blocked_by_the_audit(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    doc = yaml.safe_load((ROOT / "examples/research_v0.yaml").read_text())
    doc["nodes"]["planted"] = {"kind": "module", "module": "test.planted_fake_number"}
    doc["edges"] = [e for e in doc["edges"] if not (e["source"] == "report" and e["target"] == "audit")]
    doc["edges"] += [{"source": "report", "target": "planted"}, {"source": "planted", "target": "audit"}]
    llm = MockBackend(scripted_llm)
    res = Engine(HarnessSpec.model_validate(doc), backends={"proposer": llm, "executor": llm, "judge": llm},
                 store=store, sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(rings_task)})
    audit = res.state["audit"]
    assert audit["I4"] is False and not audit["passed"]
    assert score_state(res.state, res.status) == (0.0, False)  # no credit for a fabricated result


# ---------------------------------------------------------------- §7: evolve/gate split overlap
def test_evolve_and_gate_splits_cannot_share_a_task(store, tmp_path):
    from custom_harness.evolve import SuiteTask
    tasks = [SuiteTask("t1", tmp_path, "evolve"), SuiteTask("t1", tmp_path, "gate")]
    ev = Evaluator({}, store, lambda k: LocalSandbox(tmp_path / k, i_understand_no_isolation=True), tasks)
    with pytest.raises(ValueError, match="both the evolve and the gate split"):
        evolve_generation(seed_spec(), tasks, evaluator=ev, proposer=MockBackend(["{}"]), store=store,
                          cfg=EvolveConfig())
