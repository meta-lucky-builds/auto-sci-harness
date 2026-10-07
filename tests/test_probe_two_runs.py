"""Probe (temporary): two research runs on the same task in one store -- does run 1's audit leak-scan read poison
run 2's claim at K2?"""

import yaml

import custom_harness.research  # noqa: F401
from custom_harness.gateway import MockBackend
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from test_research import SPEC, rings_task, scripted_llm  # noqa: F401


def test_probe(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    out = []
    for i in range(2):
        llm = MockBackend(scripted_llm)
        res = Engine(HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text())),
                     backends=dict.fromkeys(("proposer", "executor", "judge", "replicator"), llm), store=store,
                     sandbox=LocalSandbox(tmp_path / f"ws{i}", i_understand_no_isolation=True)).run(
            "rings", {"task_dir": str(rings_task)}, seed=i)
        c = res.state["certificate"]
        out.append((c["label"], c["failed"]))
    print("PROBE", out)
    assert out[1][0] != "REJECTED" or "K2" not in out[1][1], out
