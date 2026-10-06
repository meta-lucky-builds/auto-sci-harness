"""Canary + leak audit (research.audit I5; GOAL invariant 2, §7 "canary leak test").

A clean run must pass; each planted leak must be caught with a location (never the secret itself).
"""

import json

import pytest
import yaml

import custom_harness.research  # noqa: F401
from custom_harness.gateway import ChatRequest, Message, MockBackend
from custom_harness.records import RunStatus
from custom_harness.runtime import Engine, HarnessSpec, register
from custom_harness.runtime.template import PrivateDataAccess, render
from custom_harness.sandbox import LocalSandbox
from custom_harness.verify.leak import ESCAPE_MARKER, leak_audit, private_fingerprints
from test_research import KNN_RADIUS, ROOT, rings_task, scripted_llm  # noqa: F401


def research_spec(extra_before_audit: str | None = None) -> HarnessSpec:
    doc = yaml.safe_load((ROOT / "examples/research_v1.yaml").read_text())
    if extra_before_audit:
        doc["nodes"]["planted"] = {"kind": "module", "module": extra_before_audit}
        doc["edges"] = [e for e in doc["edges"] if not (e["source"] == "report" and e["target"] == "audit")]
        doc["edges"] += [{"source": "report", "target": "planted"}, {"source": "planted", "target": "audit"}]
    return HarnessSpec.model_validate(doc)


def run(store, tmp_path, task, llm, spec=None):
    eng = Engine(spec or research_spec(), backends={"proposer": llm, "executor": llm, "judge": llm},
                 store=store, sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True))
    return eng.run("rings", {"task_dir": str(task)})


def test_clean_run_passes_and_trace_holds_prompts_but_no_private_values(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    monkeypatch.setenv("CHH_DATA_DIR", str(tmp_path / "data"))
    res = run(store, tmp_path, rings_task, MockBackend(scripted_llm))
    assert res.status == RunStatus.COMPLETED, res.detail
    assert res.state["audit"]["I5"] is True and res.state["audit"]["passed"], res.state["audit"]
    trace = store.trace(res.run_id)
    dumped = json.dumps([s.model_dump(mode="json") for s in trace])
    assert res.state["_private_canary"] not in dumped
    assert str(rings_task) not in dumped and res.state["_private_dev_labels_ref"] not in dumped
    llm_steps = [s for s in trace if s.kind == "llm"]
    assert llm_steps and all(s.output.get("messages") for s in llm_steps)  # rendered prompts recorded
    tour = next(s for s in trace if s.node_id == "tournament")
    assert tour.output["_module_llm_calls"][0]["request"]  # module LLM requests recorded too


@register("test.planted_copy_labels")
def _copy_labels(state, params, ctx):  # a harness bug: hidden labels copied into the agent workspace
    from custom_harness.kernel import Kernel
    labels = Kernel(ctx.store).vault.read(state["datasets"]["test"], "test_y.csv", purpose="leakscan")
    (ctx.sandbox.workspace / "data" / "maybe_useful.csv").write_bytes(labels)
    return {}


@register("test.planted_canary_prompt")
def _canary_prompt(state, params, ctx):  # a module bug: private state sent to a model, bypassing templates
    ctx.llm("executor", ChatRequest(messages=[Message(role="user", content="debug: " + state["_private_canary"])]))
    return {}


def test_private_file_copied_into_workspace_is_caught(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    res = run(store, tmp_path, rings_task, MockBackend(lambda r: "ok" if "debug" in r.messages[-1].content
                                                       else scripted_llm(r)),
              research_spec("test.planted_copy_labels"))
    audit = res.state["audit"]
    assert audit["I5"] is False and not audit["passed"]
    assert any("copy of private file" in p and p.endswith("/test/test_y.csv") for p in audit["problems"]), audit


def test_private_value_sent_to_a_model_is_caught_even_though_trace_is_scrubbed(store, tmp_path, rings_task,  # noqa: F811
                                                                              monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    llm = MockBackend(lambda r: "ok" if "debug" in r.messages[-1].content else scripted_llm(r))
    res = run(store, tmp_path, rings_task, llm, research_spec("test.planted_canary_prompt"))
    audit = res.state["audit"]
    assert audit["I5"] is False
    assert any("private value reached agent-visible output" in p and "planted" in p for p in audit["problems"])
    assert res.state["_private_canary"] not in json.dumps(audit)  # the report names locations, not secrets
    assert res.state["_private_canary"] not in json.dumps([s.model_dump(mode="json") for s in store.trace(res.run_id)])


def test_hidden_labels_hard_coded_in_agent_code_are_caught(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    labels = (rings_task / "private" / "test_y.csv").read_text().split()[1:]
    cheat = KNN_RADIUS + "\nLEAKED = [" + ", ".join(labels) + "]\n"

    def llm(req):
        text = req.messages[-1].content
        if "Implement this hypothesis" in text or "This script failed" in text:
            return json.dumps({"code": cheat, "notes": "x"})
        return scripted_llm(req)
    res = run(store, tmp_path, rings_task, MockBackend(llm))
    audit = res.state["audit"]
    assert audit["I5"] is False
    assert any("hidden test labels" in p for p in audit["problems"])


def test_leak_audit_units(tmp_path):
    priv = tmp_path / "private"
    priv.mkdir()
    (priv / "y.csv").write_text("label\n" + "\n".join(str(i % 3) for i in range(100)))
    (priv / "canary.txt").write_text("CHH-CANARY-abc123\n")
    fp = private_fingerprints(priv, labels=[str(i % 3) for i in range(100)])
    assert fp["canaries"] == ["CHH-CANARY-abc123"]
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "preds.csv").write_text("prediction\n" + "\n".join(str(i % 3) for i in range(100)))  # perfect preds
    assert leak_audit([], ws, fp, fp["canaries"])["passed"]  # a perfect predictions file is NOT a leak
    (ws / "notes.txt").write_text("found CHH-CANARY-abc123 somewhere")
    r = leak_audit([], ws, fp, fp["canaries"], extra_texts={"report": f"{ESCAPE_MARKER}: x"})
    assert not r["passed"] and len(r["problems"]) == 2
    assert all("abc123" not in p for p in r["problems"])


def test_template_guard_still_blocks_canary():
    with pytest.raises(PrivateDataAccess):
        render("{{_private_canary}}", {"_private_canary": "CHH-CANARY-x"})
