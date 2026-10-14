"""MapNode fan-out (GOAL §5 L0 Runtime): per-branch persisted outputs, deterministic ids, declared reducers; resume
after a crash mid-fan-out re-runs only unfinished branches with no double charge; protected-node and reserved-key
rules hold inside branches."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

import _map_crash_helpers as H
from custom_harness.budget import Budget
from custom_harness.errors import InfraError
from custom_harness.gateway import Backend, ChatRequest, ChatResponse, MockBackend
from custom_harness.records import AccessMode, Cost, RunStatus, StepKind
from custom_harness.runtime import (
    ApprovalNode,
    Edge,
    Engine,
    HarnessSpec,
    LLMNode,
    MapNode,
    ModuleNode,
    UndeclaredReducer,
    register,
)
from custom_harness.runtime.engine import RetryPolicy
from custom_harness.store import EvidenceStore

HERE = Path(__file__).resolve().parent


def _tokens(c: Cost) -> int:
    return c.prompt_tokens + c.completion_tokens


def _units(c: Cost) -> tuple:
    return (c.prompt_tokens, c.completion_tokens, round(c.usd, 9), c.quota_units, c.gpu_hours)


def _echo():
    return {"executor": MockBackend(lambda req: req.messages[-1].content.upper())}


def _map_spec(items_key="input.items", reducer="collect", body=None, **kw):
    nodes = {"m": MapNode(items_key=items_key, body="b", output_key="out", reducer=reducer, **kw),
             "b": body or LLMNode(prompt="say {{item}}", output_key="said")}
    return HarnessSpec(id="map", entry="m", nodes=nodes)


# ------------------------------------------------------------------------------------------- basics
def test_fan_out_runs_each_item_in_order_with_deterministic_persisted_branches(store):
    eng = Engine(_map_spec(), backends=_echo(), store=store)
    res = eng.run("t", {"items": ["a", "b", "c"]})
    assert res.status == RunStatus.COMPLETED
    assert res.state["out"] == ["SAY A", "SAY B", "SAY C"]
    assert "item" not in res.state and "said" not in res.state  # branch state never reaches run state
    [row] = store.trace(res.run_id)
    assert row.kind == StepKind.CONTROL and row.output["n_items"] == 3
    recs = store.records_where("map_branch", "run_id", res.run_id)
    assert [r["index"] for r in recs] == [0, 1, 2]
    assert [r["branch_id"] for r in recs] == [b["branch_id"] for b in row.output["branches"]]
    assert len({r["branch_id"] for r in recs}) == 3
    for r in recs:  # persisted output is the branch output
        assert store.blobs.get_json(r["output_ref"])["value"] == "SAY " + "abc"[r["index"]].upper()
    # deterministic: the id is a function of (instance, node, position, item) only
    from custom_harness.runtime.engine import _branch_id, approval_item_hash
    inst = row.output["instance"]
    assert inst == f"{res.run_id}:0"
    assert recs[1]["branch_id"] == _branch_id(inst, "m", 1, approval_item_hash("b"))
    # the step's cost is the sum of its branches' costs
    assert _tokens(row.cost) == sum(_tokens(Cost.model_validate(r["cost"])) for r in recs) > 0


@pytest.mark.parametrize("reducer,outputs,expected", [
    ("concat", [[1], [2, 3]], [1, 2, 3]), ("sum", [1, 2.5], 3.5), ("merge", [{"a": 1}, {"b": 2}], {"a": 1, "b": 2})])
def test_declared_reducers_combine(store, reducer, outputs, expected):
    import json
    eng = Engine(_map_spec(reducer=reducer, body=LLMNode(prompt="{{item}}", output_key="v",
                                                         output_schema={})), backends={
        "executor": MockBackend(lambda req: req.messages[-1].content)}, store=store)
    res = eng.run("t", {"items": [json.dumps(o) for o in outputs]})
    assert res.status == RunStatus.COMPLETED and res.state["out"] == expected


def test_reducer_type_errors_fail_the_step(store):
    eng = Engine(_map_spec(reducer="sum"), backends=_echo(), store=store)
    res = eng.run("t", {"items": ["a"]})
    assert res.status == RunStatus.FAILED and "ReducerError" in res.detail
    eng2 = Engine(_map_spec(reducer="merge", body=LLMNode(prompt="{{item}}", output_key="v", output_schema={})),
                  backends={"executor": MockBackend(lambda req: req.messages[-1].content)}, store=store)
    res2 = eng2.run("t", {"items": ['{"k": 1}', '{"k": 2}']})
    assert res2.status == RunStatus.FAILED and "conflicts" in res2.detail


def test_undeclared_reducer_is_refused_by_spec_and_by_engine(store):
    with pytest.raises(ValidationError, match="not declared"):
        _map_spec(reducer="mean")
    # a spec built without validation is refused when the engine is built, before any run
    good = _map_spec()
    bad_map = MapNode.model_construct(kind="map", items_key="input.items", body="b", item_key="item",
                                      output_key="out", reducer="last_wins", max_items=64)
    bad = HarnessSpec.model_construct(**{**dict(good), "nodes": {**good.nodes, "m": bad_map}})
    with pytest.raises(UndeclaredReducer):
        Engine(bad, backends=_echo(), store=store)
    assert store.list_runs(10) == []


def test_too_many_items_fails_never_truncates(store):
    eng = Engine(_map_spec(max_items=2), backends=_echo(), store=store)
    res = eng.run("t", {"items": ["a", "b", "c"]})
    assert res.status == RunStatus.FAILED and "max_items" in res.detail
    assert store.records_where("map_branch", "run_id", res.run_id) == []


def test_budget_is_checked_before_every_branch(store):
    spec = _map_spec()
    spec = spec.model_copy(update={"budget": spec.budget.model_copy(update={"tokens": 5})})
    eng = Engine(spec, backends=_echo(), store=store)
    res = eng.run("t", {"items": ["a", "b", "c", "d"]})
    assert res.status == RunStatus.BUDGET_EXCEEDED
    ran = store.records_where("map_branch", "run_id", res.run_id)
    assert 0 < len(ran) < 4  # it started, and stopped launching branches once the cap was reached


# ------------------------------------------------------------------------- protected / reserved rules
def test_body_must_be_reachable_only_through_maps():
    nodes = {"setup": LLMNode(prompt="p", output_key="s"),
             "m": MapNode(items_key="input.items", body="setup", output_key="out", reducer="collect")}
    with pytest.raises(ValidationError, match="reachable only through maps"):  # a wired (e.g. protected) node
        HarnessSpec(id="x", entry="m", nodes=nodes, edges=[Edge(source="m", target="setup")])
    with pytest.raises(ValidationError, match="reachable only through maps"):  # the entry
        HarnessSpec(id="x", entry="setup", nodes=nodes)


@pytest.mark.parametrize("body", [ApprovalNode(item_key="out"),
                                  MapNode(items_key="input.items", body="m", output_key="o2", reducer="collect")])
def test_approval_and_map_nodes_cannot_be_branch_bodies(body):
    with pytest.raises(ValidationError, match="only llm, tool or module"):
        HarnessSpec(id="x", entry="m", nodes={
            "m": MapNode(items_key="input.items", body="b", output_key="out", reducer="collect"), "b": body})


@pytest.mark.parametrize("field,value", [("output_key", "_private_y"), ("output_key", "review_approval"),
                                         ("output_key", "gate_decision"), ("output_key", "input"),
                                         ("output_key", "model_policy"), ("item_key", "_private_x"),
                                         ("item_key", "a.b"), ("items_key", "_private_labels"),
                                         ("items_key", "_last_error.x")])
def test_reserved_keys_are_refused_on_map_nodes(field, value):
    kw = {"items_key": "input.items", "body": "b", "output_key": "out", "reducer": "collect", field: value}
    with pytest.raises(ValidationError):
        HarnessSpec(id="x", entry="m", nodes={"m": MapNode(**kw), "b": LLMNode(prompt="p", output_key="v")})


_attempted: list[str] = []


@register("test.map_reserved_writer")
def _reserved_writer(state, params, ctx):
    _attempted.append(params["key"])
    return {params["key"]: "forged", "fine": 1}


@pytest.mark.parametrize("key", ["_private_answer", "review_approval", "claim_decision", "input", "model_policy"])
def test_branch_writing_reserved_state_is_refused(store, key):
    _attempted.clear()
    eng = Engine(_map_spec(body=ModuleNode(module="test.map_reserved_writer", params={"key": key})), backends={},
                 store=store)
    res = eng.run("t", {"items": ["a", "b"]})
    assert _attempted == [key]  # the attack happened: the branch really returned the reserved key
    assert res.status == RunStatus.FAILED and "reserved state key" in res.detail
    assert res.state.get(key) != "forged" and "out" not in res.state


@register("test.map_mutator")
def _mutator(state, params, ctx):
    state["input"]["shared"]["n"] += 1  # mutating its state copy must not leak into the run or the next branch
    return {"n": state["input"]["shared"]["n"]}


def test_branches_run_on_isolated_state_copies(store):
    eng = Engine(_map_spec(body=ModuleNode(module="test.map_mutator")), backends={}, store=store)
    res = eng.run("t", {"items": [1, 2, 3], "shared": {"n": 0}})
    assert res.status == RunStatus.COMPLETED, res.detail
    assert res.state["out"] == [{"n": 1}, {"n": 1}, {"n": 1}]
    assert res.state["input"]["shared"] == {"n": 0}


# ------------------------------------------------------------------------- taint and replayed output
class _Rec:
    def __init__(self):
        self.calls = []

    def emit(self, value, **kw):
        self.calls.append((value, kw))


def test_item_taint_reaches_branch_prompts_and_the_map_output(store):
    eng = Engine(_map_spec(), backends=_echo(), store=store)
    eng.taint["input"] = {"target-1"}
    eng.releases = _Rec()
    res = eng.run("t", {"items": ["a", "b"]})
    assert res.status == RunStatus.COMPLETED
    assert [kw["targets"] for _, kw in eng.releases.calls] == [{"target-1"}, {"target-1"}]  # one per branch prompt
    assert eng.taint["out"] >= {"target-1"}


class _Replayed(Backend):
    name, provider, mode, training_on_outputs = "cassette", "mock", AccessMode.MOCK, "allowed"

    def complete(self, req: ChatRequest) -> ChatResponse:
        return ChatResponse(text="r", cost=Cost(prompt_tokens=1),
                            provenance=self.provenance("m").model_copy(update={"replayed": True}), replayed=True)


def test_replayed_branch_output_marks_the_map_step(store):
    res = Engine(_map_spec(), backends={"executor": _Replayed()}, store=store).run("t", {"items": ["a"]})
    [row] = store.trace(res.run_id)
    assert row.output["replayed"] is True and row.provenance.replayed is True


# ------------------------------------------------------------------------- crash, infra failure, resume
def _kill_mid_fan_out(tmp_path, when):
    store_dir, marker = tmp_path / "store", tmp_path / "marker"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(HERE), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.Popen([sys.executable, str(HERE / "_map_crash_helpers.py"), str(store_dir), str(marker), when],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 60
        while not marker.exists():
            assert proc.poll() is None, proc.stderr.read().decode()
            assert time.time() < deadline, "child never reached the in-flight branch"
            time.sleep(0.05)
        time.sleep(0.3)
    finally:
        proc.kill()  # TerminateProcess / SIGKILL: no cleanup runs
        proc.wait()
    return EvidenceStore(store_dir), marker


def _clean(tmp_path, when):
    st = EvidenceStore(tmp_path / f"clean-{when}")
    marker = tmp_path / f"clean-marker-{when}"
    marker.write_text("never block")
    res = H.engine(str(tmp_path / f"clean-{when}"), str(marker), when).run("t", {"items": H.ITEMS})
    return st, res


@pytest.mark.parametrize("when", ["before", "after"])
def test_kill9_mid_fan_out_then_resume_reruns_only_unfinished_branches(tmp_path, when):
    store, marker = _kill_mid_fan_out(tmp_path, when)
    run = store.list_runs(1)[0]
    assert store.run_status(run.id)[0] == RunStatus.RUNNING
    prefix = [s.model_dump() for s in store.trace(run.id)]
    assert [s["node_id"] for s in prefix] == ["a"]  # the map step was in flight
    before = store.records_where("map_branch", "run_id", run.id)
    assert [r["index"] for r in before] == [0, 1]  # two branches finished and were persisted before the kill

    eng = Engine(H.spec(str(marker), when), backends=H.backends(), store=store)
    res = eng.resume(run.id)
    assert res.status == RunStatus.COMPLETED
    trace = store.trace(run.id)
    assert [s.model_dump() for s in trace[:1]] == prefix  # identical trace prefix
    assert [s.node_id for s in trace] == ["a", "m", "d"]  # no interrupted row: the map recovers per branch
    m = trace[1].output
    assert m["reused_branches"] == 2
    assert [b["reused"] for b in m["branches"]] == [True, True, False, False]
    assert [b["branch_id"] for b in m["branches"][:2]] == [r["branch_id"] for r in before]  # same ids after resume
    # only unfinished branches re-ran: the resumed process made calls for x2, x3 and node d only
    asked = [r.messages[-1].content for r in eng.backends["executor"].requests]
    assert asked[:2] == ["work on x2", "work on x3"] and len(asked) == 3
    assert res.state["results"] == [{"item": i, "answer": f"done: work on {i}"} for i in H.ITEMS]

    clean_store, clean = _clean(tmp_path, when)
    recorded = sum((s.cost for s in trace), Cost())
    assert _units(eng.budget.spent) == _units(recorded)  # nothing charged twice
    if when == "before":  # killed between branches: exactly the cost of a run that never crashed
        assert m["recovered_lost_calls"] == 0
        assert _units(recorded) == _units(clean.cost)
    else:  # killed after branch x2's model call: that call is charged once (it was spent), never twice
        assert m["recovered_lost_calls"] == 1
        lost = Cost.model_validate(next(e for e in store.records_where("cost_journal", "run_id", run.id)
                                        if e.get("nonce") not in {r["nonce"] for r in
                                                                  store.records_where("map_branch", "run_id", run.id)}
                                        )["cost"])
        assert _tokens(recorded) == _tokens(clean.cost) + _tokens(lost) and _tokens(lost) > 0
    store.close()
    clean_store.close()


_outage = {"down": True, "calls": []}


@register("test.map_flaky")
def _flaky(state, params, ctx):
    _outage["calls"].append(state["item"])
    resp = ctx.llm("executor", ChatRequest(messages=[__import__("custom_harness.gateway", fromlist=["Message"])
                                                     .Message(role="user", content=f"q {state['item']}")]))
    if state["item"] == "c" and _outage["down"]:
        raise InfraError("docker daemon unavailable", transient=True)
    return {"a": resp.text}


def test_infra_failure_mid_fan_out_resumes_unfinished_branches_without_double_charge(store, tmp_path):
    _outage.update(down=True, calls=[])
    spec = _map_spec(body=ModuleNode(module="test.map_flaky"))
    eng = Engine(spec, backends=_echo(), store=store, retry=RetryPolicy(max_attempts=1))
    res = eng.run("t", {"items": ["a", "b", "c", "d"]})
    assert res.status == RunStatus.INFRA_FAILED
    assert _outage["calls"] == ["a", "b", "c"]
    _outage.update(down=False, calls=[])
    eng2 = Engine(spec, backends=_echo(), store=store, retry=RetryPolicy(max_attempts=1))
    res2 = eng2.resume(res.run_id)
    assert res2.status == RunStatus.COMPLETED
    assert _outage["calls"] == ["c", "d"]  # finished branches a, b were reused, not re-run
    trace = store.trace(res.run_id)
    assert [s.node_id for s in trace] == ["m", "m"]
    assert trace[1].output["reused_branches"] == 2 and trace[1].output["instance"] == trace[0].output["instance"]
    # charged once: the infra row charged a, b and the failed c; the resumed row only c (again) and d
    a_b_cost = sum((Cost.model_validate(b["cost"]) for b in trace[0].output["branches"][:2]), Cost())
    assert _tokens(trace[1].cost) == _tokens(sum((Cost.model_validate(b["cost"])
                                                  for b in trace[1].output["branches"][2:]), Cost()))
    assert _tokens(eng2.budget.spent) == _tokens(trace[0].cost) + _tokens(trace[1].cost)
    assert _tokens(a_b_cost) > 0
    _outage.update(down=False, calls=[])
    clean = Engine(spec, backends=_echo(), store=EvidenceStore(tmp_path / "c2"),
                   retry=RetryPolicy(max_attempts=1)).run("t", {"items": ["a", "b", "c", "d"]})
    # exactly one extra model call compared with a clean run: the call of the infra-failed attempt of branch c
    failed_c = Cost.model_validate(trace[0].output["branches"][2]["cost"])
    assert _tokens(eng2.budget.spent) == _tokens(clean.cost) + _tokens(failed_c)


def test_budget_exceeded_mid_fan_out_with_a_shared_budget(store):
    eng = Engine(_map_spec(), backends=_echo(), store=store, budget=Budget(limits={"tokens": 3}))
    res = eng.run("t", {"items": ["a", "b"]})
    assert res.status == RunStatus.BUDGET_EXCEEDED


def test_llm_body_cannot_fan_out_over_tool_output_even_via_an_evolved_edit():
    from custom_harness.evolve.edits import Edit, EditOp, EditRejected, apply_edit
    from custom_harness.runtime import ToolNode
    spec = HarnessSpec(id="s", entry="t", nodes={"t": ToolNode(command=["ls"], output_key="tool_out"),
                                                 "w": LLMNode(prompt="hi", output_key="essay")},
                       edges=[Edge(source="t", target="w")])
    edit = Edit(type="hop", rationale="x", ops=[
        EditOp(op="add", path="/nodes/m", value={"kind": "map", "items_key": "tool_out.stdout", "body": "b",
                                                 "output_key": "mo", "reducer": "collect"}),
        EditOp(op="add", path="/nodes/b", value={"kind": "llm", "prompt": "{{item}}", "output_key": "bo"}),
        EditOp(op="replace", path="/edges/0/target", value="m"),
        EditOp(op="add", path="/edges/-", value={"source": "m", "target": "w"})])
    with pytest.raises(EditRejected, match="tool output"):  # tool output would reach a model prompt as {{item}}
        apply_edit(spec, edit, protected_nodes=(), terminal_required=())
