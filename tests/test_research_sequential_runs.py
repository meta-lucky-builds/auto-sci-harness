"""K2-TWO-RUN regression: honest research runs on one task in one store must not poison each other at K2.

Run 1's audit reads the test labels with purpose="leakscan" (no claim) to build its leak fingerprints. Before the
fix, K2 counted any early private read without a registered claim as peeking, so every claim registered on that task
AFTER another run's audit was REJECTED by K2 (verified: `[CERTIFIED-INTERNAL, REJECTED(K2)]`). Integrity reads are now
listed as K2 observations (accountability), not as peeking (trust boundary: docs/kernel.md).
"""

import yaml

import custom_harness.research  # noqa: F401  registers modules
from custom_harness.gateway import MockBackend
from custom_harness.kernel import Kernel
from custom_harness.runtime import Engine, HarnessSpec
from custom_harness.sandbox import LocalSandbox
from test_research import SPEC, rings_task, scripted_llm  # noqa: F401


def test_sequential_runs_on_one_task_do_not_poison_each_other_at_k2(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")  # trusted test code only
    out = []
    for i in range(2):
        llm = MockBackend(scripted_llm)
        res = Engine(HarnessSpec.model_validate(yaml.safe_load(SPEC.read_text())),
                     backends=dict.fromkeys(("proposer", "executor", "judge", "replicator"), llm), store=store,
                     sandbox=LocalSandbox(tmp_path / f"ws{i}", i_understand_no_isolation=True)).run(
            "rings", {"task_dir": str(rings_task)}, seed=i)
        out.append(res.state["certificate"])
    assert [c["label"] for c in out] == ["CERTIFIED-INTERNAL", "CERTIFIED-INTERNAL"], out
    # the scenario really happened: run 1's leak-scan read precedes run 2's pre-registration, and run 2's
    # certificate lists it as an integrity observation instead of ignoring it
    k2 = Kernel(store).certificate(out[1]["hash"]).checks["K2"]
    assert k2.status == "pass"
    assert any("integrity read(s) (leakscan) before pre-registration" in o for o in k2.observations), k2.observations
