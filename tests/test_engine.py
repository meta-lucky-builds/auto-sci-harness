import json
import sys

import pytest

from custom_harness.budget import Budget
from custom_harness.gateway import MockBackend
from custom_harness.records import RunStatus
from custom_harness.runtime import (END, Condition, Edge, Engine, HarnessSpec, LLMNode, ModuleNode,
                                    ToolNode, register)
from custom_harness.runtime.template import render
from custom_harness.sandbox import LocalSandbox

SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"]}


def one_llm_spec(**kw):
    return HarnessSpec(id="t", entry="ask",
                       nodes={"ask": LLMNode(prompt="Q: {{input.q}}", output_key="ans", **kw)})


def test_contract_success_records_trace(store):
    spec = one_llm_spec(output_schema=SCHEMA)
    eng = Engine(spec, backends={"executor": MockBackend(['```json\n{"answer": 4}\n```'])}, store=store)
    res = eng.run("task1", {"q": "2+2"})
    assert res.status == RunStatus.COMPLETED
    assert res.state["ans"] == {"answer": 4}
    trace = store.trace(res.run_id)
    assert len(trace) == 1 and trace[0].provenance.backend == "mock"
    assert store.run_status(res.run_id)[0] == RunStatus.COMPLETED


def test_contract_retry_feeds_error_back(store):
    backend = MockBackend(["not json", '{"answer": "four"}', '{"answer": 4}'])
    eng = Engine(one_llm_spec(output_schema=SCHEMA, retries=2), backends={"executor": backend}, store=store)
    res = eng.run("t", {"q": "2+2"})
    assert res.state["ans"] == {"answer": 4}
    assert len(backend.requests) == 3
    assert "violated the required JSON contract" in backend.requests[-1].messages[-1].content


def test_contract_failure_without_error_edge_fails_run_and_keeps_cost(store):
    eng = Engine(one_llm_spec(output_schema=SCHEMA, retries=1), backends={"executor": MockBackend(["nope"])},
                 store=store)
    res = eng.run("t", {"q": "x"})
    assert res.status == RunStatus.FAILED
    step = store.trace(res.run_id)[0]
    assert step.error.startswith("ContractError")
    assert step.cost.prompt_tokens > 0  # failed attempts are still accounted


def test_error_edge_routes_to_recovery(store):
    spec = HarnessSpec(
        id="t", entry="ask",
        nodes={"ask": LLMNode(prompt="{{input.q}}", output_key="ans", output_schema=SCHEMA),
               "fallback": LLMNode(prompt="fallback", output_key="fb")},
        edges=[Edge(source="ask", target="fallback", when=Condition(key="_last_error", op="truthy"))])
    eng = Engine(spec, backends={"executor": MockBackend(["bad", "recovered"])}, store=store)
    res = eng.run("t", {"q": "x"})
    assert res.status == RunStatus.COMPLETED and res.state["fb"] == "recovered"


@register("test.counter")
def _counter(state, params, ctx):
    return {"n": state.get("n", 0) + params["step"]}


def test_conditional_loop_and_step_limit(store):
    spec = HarnessSpec(id="loop", entry="inc",
                       nodes={"inc": ModuleNode(module="test.counter", params={"step": 1})},
                       edges=[Edge(source="inc", target=END, when=Condition(key="n", op="gt", value=2)),
                              Edge(source="inc", target="inc")])
    res = Engine(spec, backends={}, store=store).run("t", {})
    assert res.status == RunStatus.COMPLETED and res.state["n"] == 3

    capped = spec.model_copy(update={"max_steps": 2})
    res = Engine(capped, backends={}, store=store).run("t", {})
    assert res.status == RunStatus.STEP_LIMIT


def test_budget_exceeded_stops_run(store):
    spec = HarnessSpec(id="b", entry="a",
                       nodes={"a": LLMNode(prompt="hello world", output_key="x")},
                       edges=[Edge(source="a", target="a")])
    eng = Engine(spec, backends={"executor": MockBackend(["one two three"])}, store=store,
                 budget=Budget(limits={"tokens": 10}))
    res = eng.run("t", {})
    assert res.status == RunStatus.BUDGET_EXCEEDED


def test_tool_snapshot_and_exact_fork(store, tmp_path):
    sb = LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)
    write = [sys.executable, "-c", "import pathlib; pathlib.Path('f.txt').write_text('{{input.v}}')"]
    spec = HarnessSpec(id="fs", entry="w1",
                       nodes={"w1": ToolNode(command=write, output_key="r1"),
                              "w2": ToolNode(command=[sys.executable, "-c",
                                                      "import pathlib; pathlib.Path('f.txt').write_text('second')"],
                                             output_key="r2")},
                       edges=[Edge(source="w1", target="w2")])
    eng = Engine(spec, backends={}, store=store, sandbox=sb)
    res = eng.run("t", {"v": "first"})
    assert res.status == RunStatus.COMPLETED
    assert (sb.workspace / "f.txt").read_text() == "second"

    forked = eng.fork(res.run_id, step_index=0)  # restore state after w1, re-run w2
    assert forked.status == RunStatus.COMPLETED
    assert forked.state["_forked_from"]["run_id"] == res.run_id
    assert len(store.trace(forked.run_id)) == 1


def test_fork_restores_workspace_bytes(store, tmp_path):
    sb = LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)
    spec = HarnessSpec(id="fs2", entry="w1", nodes={"w1": ToolNode(
        command=[sys.executable, "-c", "import pathlib; pathlib.Path('a/b.bin').parent.mkdir(exist_ok=True);"
                 "pathlib.Path('a/b.bin').write_bytes(bytes(range(256)))"], output_key="r")})
    eng = Engine(spec, backends={}, store=store, sandbox=sb)
    res = eng.run("t", {})
    (sb.workspace / "a" / "b.bin").write_bytes(b"corrupted later")
    (sb.workspace / "junk.txt").write_text("junk")
    eng.fork(res.run_id, 0)
    assert (sb.workspace / "a" / "b.bin").read_bytes() == bytes(range(256))
    assert not (sb.workspace / "junk.txt").exists()


def test_local_sandbox_requires_ack_and_scrubs_env(tmp_path, monkeypatch):
    with pytest.raises(PermissionError):
        LocalSandbox(tmp_path / "x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secretsecretsecretsecret")
    sb = LocalSandbox(tmp_path / "x", i_understand_no_isolation=True)
    r = sb.exec([sys.executable, "-c", "import os; print(os.environ.get('OPENAI_API_KEY'))"])
    assert r.stdout.strip() == "None"
    r = sb.exec([sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=0.5)
    assert r.timed_out


def test_spec_validation_and_hash_stability():
    with pytest.raises(ValueError):
        HarnessSpec(id="bad", entry="missing", nodes={})
    with pytest.raises(ValueError):
        HarnessSpec(id="bad", entry="a", nodes={"a": LLMNode(prompt="p", output_key="o")},
                    edges=[Edge(source="a", target="nowhere")])
    a = one_llm_spec()
    b = HarnessSpec.model_validate(json.loads(a.model_dump_json()))
    assert a.spec_hash() == b.spec_hash()


def test_template_truncation_keeps_head_and_tail():
    out = render("{{x}}", {"x": "A" * 50 + "B" * 50}, max_chars=20)
    assert out.startswith("A" * 10) and out.endswith("B" * 10) and "omitted" in out
