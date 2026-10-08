"""Campaign-level budgets (GOAL §5 L1): one budget for a whole campaign, persisted across crash and fork, charged
across runs. Done-when: a hard-killed campaign resumes with the same total spend and no double charge."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import _campaign_crash as C
import _crash_helpers as H
from custom_harness.budget import Campaign, CampaignError
from custom_harness.records import Cost, RunStatus
from custom_harness.runtime import Edge, Engine, HarnessSpec, LLMNode
from custom_harness.store import EvidenceStore

HERE = Path(__file__).resolve().parent


def _tok(c: Cost) -> int:
    return c.prompt_tokens + c.completion_tokens


def _trace_tokens(store, run_id, nodes=None) -> int:
    return sum(_tok(s.cost) for s in store.trace(run_id) if nodes is None or s.node_id in nodes)


def test_hard_killed_campaign_resumes_with_the_same_total_and_no_double_charge(tmp_path):
    store_dir, ws, marker = tmp_path / "store", tmp_path / "ws", tmp_path / "marker"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(HERE), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.Popen([sys.executable, str(HERE / "_campaign_crash.py"), str(store_dir), str(ws), str(marker)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 60
        while not marker.exists():
            assert proc.poll() is None, proc.stderr.read().decode()
            assert time.time() < deadline, "child never reached the in-flight step"
            time.sleep(0.05)
        time.sleep(0.2)
    finally:
        proc.kill()  # hard kill: no cleanup code runs in the child
        proc.wait()

    store = EvidenceStore(store_dir)
    camp = Campaign(store, "camp")  # a new process re-opens the campaign by id; its caps come from the store
    assert camp.limits == C.CAPS
    run1, run2 = camp.members()
    assert store.run_status(run1)[0] == RunStatus.COMPLETED
    assert store.run_status(run2)[0] == RunStatus.RUNNING  # nothing could mark it
    assert [s.node_id for s in store.trace(run2)] == ["a", "b"]
    journal = [e for e in store.records_where("cost_journal", "run_id", run2) if e["step"] == 2]
    assert len(journal) == 1  # the attack really happened: the in-flight model call exists only in the journal

    # reference: the same campaign in a clean store, never killed
    clean = EvidenceStore(tmp_path / "clean")
    ccamp = Campaign(clean, "camp", C.CAPS)
    c1 = C.first_engine(clean, ccamp).run("t1", {})
    c2 = C.second_engine(clean, str(tmp_path / "ws2"), str(marker), ccamp).run("t2", {"q": 1})
    assert c1.status == c2.status == RunStatus.COMPLETED
    clean_total = _tok(ccamp.spent)
    in_flight = _trace_tokens(clean, c2.run_id, {"c"})  # the one call step c makes
    assert in_flight > 0

    at_kill = _tok(camp.spent)
    assert at_kill == _trace_tokens(clean, c1.run_id) + _trace_tokens(clean, c2.run_id, {"a", "b", "c"})
    assert _tok(Campaign(store, "camp").spent) == at_kill  # re-opening again: same total (nothing is a counter)

    eng = C.second_engine(store, str(ws), str(marker), Campaign(store, "camp"))
    assert eng.resume(run2).status == RunStatus.COMPLETED
    after = Campaign(store, "camp")
    assert after.members() == [run1, run2]  # the resume did not add a member
    # each step charged once: the interrupted step's journal spend is replaced by its trace row, not added to it
    assert _tok(after.spent) == _trace_tokens(store, run1) + _trace_tokens(store, run2)
    # versus the clean campaign: exactly the one call the kill interrupted is spent again
    assert _tok(after.spent) == clean_total + in_flight
    store.close()
    clean.close()


def _two_step() -> HarnessSpec:
    return HarnessSpec(id="f", entry="a", nodes={"a": LLMNode(prompt="one two", output_key="x"),
                                                "b": LLMNode(prompt="three four", output_key="y")},
                       edges=[Edge(source="a", target="b")])


def test_fork_inside_a_campaign_is_charged_once_per_step(tmp_path):
    store = EvidenceStore(tmp_path / "s")
    camp = Campaign(store, "fk", {"tokens": 10_000})
    res = Engine(_two_step(), backends=H.backends(), store=store, campaign=camp).run("t", {})
    eng2 = Engine(_two_step(), backends=H.backends(), store=store, campaign=Campaign(store, "fk"))
    forked = eng2.fork(res.run_id, step_index=0)
    assert forked.status == RunStatus.COMPLETED and camp.members() == [res.run_id, forked.run_id]
    own = _trace_tokens(store, forked.run_id)
    # the fork's per-run budget inherits the parent's prefix (so a fork cannot reset a run cap) ...
    assert _tok(eng2.budget.spent) == _trace_tokens(store, res.run_id, {"a"}) + own
    # ... but the campaign counts that prefix once, in the parent run
    assert _tok(camp.spent) == _trace_tokens(store, res.run_id) + own
    store.close()


def test_campaign_cap_is_charged_across_runs_and_processes(tmp_path):
    store = EvidenceStore(tmp_path / "s")
    one = Engine(_two_step(), backends=H.backends(), store=store,
                 campaign=Campaign(store, "cap", {"tokens": 10_000, "usd": None})).run("t", {})
    used = _trace_tokens(store, one.run_id)
    store.close()
    store = EvidenceStore(tmp_path / "s")
    with pytest.raises(CampaignError, match="caps are fixed"):
        Campaign(store, "cap", {"tokens": 1e9})
    with pytest.raises(CampaignError, match="does not exist"):
        Campaign(store, "nope")
    tight = EvidenceStore(tmp_path / "t")
    Engine(_two_step(), backends=H.backends(), store=tight,
           campaign=Campaign(tight, "tight", {"tokens": used})).run("t", {})
    # a second run in the same campaign, a new process: refused before its first step (per-run budget is unlimited)
    second = Engine(_two_step(), backends=H.backends(), store=tight, campaign=Campaign(tight, "tight")).run("t", {})
    assert second.status == RunStatus.BUDGET_EXCEEDED and "campaign tight tokens" in second.detail
    assert tight.trace(second.run_id) == []
    camp = Campaign(tight, "tight")
    assert camp.charge_external("proposer-call-1", Cost(prompt_tokens=5)) is True
    assert camp.charge_external("proposer-call-1", Cost(prompt_tokens=5)) is False  # idempotent by key
    assert _tok(camp.spent) == used + 5
    store.close()
    tight.close()
