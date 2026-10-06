"""Resume after a hard kill; persisted budget; wall-clock unit (GOAL L0/L1, CR P0-4, P0-6).

Done-when (GOAL L0): kill -9 mid-run, then resume, completes with an identical trace prefix and no
double-charged cost.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import _crash_helpers as H
from custom_harness.errors import InfraError
from custom_harness.gateway import MockBackend
from custom_harness.records import Cost, Run, RunStatus
from custom_harness.runtime import Edge, Engine, HarnessSpec, LLMNode, ModuleNode, register
from custom_harness.runtime.engine import RetryPolicy
from custom_harness.store import EvidenceStore

HERE = Path(__file__).resolve().parent


def _tokens(c: Cost) -> int:
    return c.prompt_tokens + c.completion_tokens


def test_hard_kill_then_resume_is_exact_and_not_double_charged(tmp_path):
    store_dir, ws, marker = tmp_path / "store", tmp_path / "ws", tmp_path / "marker"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(HERE), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.Popen([sys.executable, str(HERE / "_crash_helpers.py"), str(store_dir), str(ws), str(marker)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 60
        while not marker.exists():
            assert proc.poll() is None, proc.stderr.read().decode()
            assert time.time() < deadline, "child never reached the in-flight step"
            time.sleep(0.05)
        time.sleep(0.2)
    finally:
        proc.kill()  # TerminateProcess on Windows / SIGKILL on POSIX: no cleanup code runs
        proc.wait()

    store = EvidenceStore(store_dir)
    run = store.list_runs(1)[0]
    status, _ = store.run_status(run.id)
    assert status == RunStatus.RUNNING  # a hard kill leaves it RUNNING (nothing could mark it)
    prefix = [s.model_dump() for s in store.trace(run.id)]
    assert [s["node_id"] for s in prefix] == ["a", "b"]  # c was in flight

    (ws / "f.txt").write_text("corrupted after the crash")
    (ws / "junk.txt").write_text("junk")

    eng = Engine(H.spec(str(marker)), backends=H.backends(), store=store,
                 sandbox=H.LocalSandbox(ws, i_understand_no_isolation=True))
    res = eng.resume(run.id)
    assert res.status == RunStatus.COMPLETED
    trace = store.trace(run.id)
    assert [s.model_dump() for s in trace[:2]] == prefix  # identical prefix
    # the killed step is recorded explicitly with the spend recovered from the cost journal, then re-run
    assert [s.index for s in trace] == [0, 1, 2, 3, 4]
    assert [s.node_id for s in trace] == ["a", "b", "c", "c", "d"]
    assert trace[2].output["interrupted"] is True and trace[2].error.startswith("interrupted")
    lost_call = trace[2].cost
    assert _tokens(lost_call) > 0  # the mid-step model call made before the kill is NOT lost
    assert res.state["seen"] == "step-a" and res.state["junk_present"] is False  # workspace restored exactly

    recorded = sum((s.cost for s in trace), Cost())
    assert _tokens(eng.budget.spent) == _tokens(recorded)  # nothing charged twice, nothing lost
    clean_store = EvidenceStore(tmp_path / "clean")
    clean = Engine(H.spec(str(marker)), backends=H.backends(), store=clean_store,
                   sandbox=H.LocalSandbox(tmp_path / "ws2", i_understand_no_isolation=True)).run("t", {"q": 1})
    # exactly one extra model call compared with a run that never crashed: the one the kill interrupted
    assert _tokens(clean.cost) + _tokens(lost_call) == _tokens(recorded)
    store.close()
    clean_store.close()


def test_resume_refuses_live_runs_finished_runs_and_other_specs(store, tmp_path):
    eng = H.engine(str(tmp_path / "s"), str(tmp_path / "ws"), str(tmp_path / "m"))
    run = Run(harness_id="crashy", harness_hash=eng.spec_hash, task_id="t",
              inputs_ref=eng.store.blobs.put_json({"input": {}, "_seed": 0}))
    eng.store.add_run(run)
    eng._started(run.id)  # owned by THIS (alive) process
    with pytest.raises(RuntimeError, match="may still be running"):
        eng.resume(run.id)
    (tmp_path / "m").write_text("go")
    assert eng.resume(run.id, force=True).status == RunStatus.COMPLETED  # from the very first step
    with pytest.raises(ValueError, match="not resumable"):
        eng.resume(run.id)  # completed

    other = Engine(HarnessSpec(id="other", entry="x", nodes={"x": LLMNode(prompt="p", output_key="o")}),
                   backends=H.backends(), store=eng.store)
    with pytest.raises(ValueError, match="exact harness spec"):
        other.resume(run.id)


@register("test.policy_fail")
def _policy_fail(state, params, ctx):
    raise RuntimeError("the harness's own bug")


def test_genuine_harness_failure_is_not_resumable(store):
    spec = HarnessSpec(id="pf", entry="a", nodes={"a": ModuleNode(module="test.policy_fail")})
    eng = Engine(spec, backends={}, store=store)
    res = eng.run("t", {})
    assert res.status == RunStatus.FAILED
    with pytest.raises(ValueError, match="not resumable"):
        eng.resume(res.run_id)


def test_fork_inherits_spend_so_budgets_cannot_be_reset(store):
    spec = HarnessSpec(id="f", entry="a", nodes={"a": LLMNode(prompt="one two", output_key="x"),
                                                "b": LLMNode(prompt="three four", output_key="y")},
                       edges=[Edge(source="a", target="b")])
    eng = Engine(spec, backends={"executor": MockBackend(["alpha beta gamma"])}, store=store)
    res = eng.run("t", {})
    first = store.trace(res.run_id)[0].cost
    eng2 = Engine(spec, backends={"executor": MockBackend(["alpha beta gamma"])}, store=store)
    forked = eng2.fork(res.run_id, step_index=0)
    assert forked.status == RunStatus.COMPLETED
    assert _tokens(eng2.budget.spent) == _tokens(first) + _tokens(store.trace(forked.run_id)[0].cost)


@register("test.sleepy")
def _sleepy(state, params, ctx):
    time.sleep(params["s"])
    return {}


_flaky = {"n": 0}


@register("test.slow_outage")
def _slow_outage(state, params, ctx):
    _flaky["n"] += 1
    if _flaky["n"] == 1:
        time.sleep(0.4)
        raise InfraError("provider outage", transient=True)
    return {}


def test_wall_clock_is_charged_but_outage_time_is_not(store):
    spec = HarnessSpec(id="w", entry="a", nodes={"a": ModuleNode(module="test.sleepy", params={"s": 0.25}),
                                                "b": ModuleNode(module="test.slow_outage")},
                       edges=[Edge(source="a", target="b")])
    eng = Engine(spec, backends={}, store=store, retry=RetryPolicy(max_attempts=2, backoff_s=0.0))
    res = eng.run("t", {})
    assert res.status == RunStatus.COMPLETED
    a, b = store.trace(res.run_id)
    assert a.cost.wall_s >= 0.25
    assert b.cost.wall_s < 0.2  # the 0.4 s spent inside the outage is not charged
    assert abs(eng.budget.spent.wall_s - (a.cost.wall_s + b.cost.wall_s)) < 1e-9


def test_campaign_budget_persists_across_processes_and_stops_the_campaign(tmp_path):
    from custom_harness.budget import BudgetExceeded, persistent_budget
    from custom_harness.evolve import Evaluator, SuiteTask

    s1 = EvidenceStore(tmp_path / "s")
    b1 = persistent_budget(s1, "campaign-1", {"tokens": 100})
    b1.charge(Cost(prompt_tokens=60))
    s1.close()
    s2 = EvidenceStore(tmp_path / "s")  # a new process
    b2 = persistent_budget(s2, "campaign-1", {"tokens": 100})
    assert _tokens(b2.spent) == 60
    assert _tokens(persistent_budget(s2, "other", {"tokens": 100}).spent) == 0  # ids are separate
    with pytest.raises(BudgetExceeded):
        b2.charge(Cost(prompt_tokens=50))
    b3 = persistent_budget(s2, "campaign-1", {"tokens": 100})
    assert _tokens(b3.spent) == 110  # the charge that crossed the cap was still recorded
    ev = Evaluator({}, s2, lambda k: None, [SuiteTask("t", tmp_path, "evolve")], campaign_budget=b3)
    with pytest.raises(BudgetExceeded):
        ev.outcome(HarnessSpec(id="x", entry="a", nodes={"a": LLMNode(prompt="p", output_key="o")}), ("t", 0))
    assert ev.calls == 0  # nothing ran once the campaign was over its cap
    s2.close()
