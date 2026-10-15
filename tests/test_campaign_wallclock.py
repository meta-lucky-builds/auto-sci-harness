"""Campaign-level wall-clock budget (GOAL §5 L1: "a wall-clock unit ... campaign-level"), persisted across crash and
fork. Two units are tested (docs/campaign_wallclock.md):
- wall_s: SUMMED STEP TIME (each member run's measured step durations). The first tests characterise what it already
  does across runs, a fork and a hard kill.
- elapsed_s: CALENDAR TIME since the campaign was opened, from the open time persisted in the campaign_open record.
  One clock per campaign; a crash, a new process or a fork never resets it; a clock that went backwards and a
  campaign without a recorded open time fail closed."""

import math
import os
import subprocess
import sys
import time
from itertools import pairwise
from pathlib import Path

import pytest

import _campaign_crash as C
import _campaign_wallclock_crash as W
import _crash_helpers as H
from custom_harness.budget import CAMPAIGN_OPEN, BudgetExceeded, Campaign, CampaignClockError, CampaignError
from custom_harness.records import RunStatus
from custom_harness.runtime import Edge, Engine, HarnessSpec, ModuleNode, register
from custom_harness.store import EvidenceStore

HERE = Path(__file__).resolve().parent


class Clock:
    """A fake wall clock (seconds since the epoch) that only moves when a test (or a tick step) moves it."""

    def __init__(self, now: float):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


_CLOCKS: dict[str, Clock] = {}


@register("wallclock.tick")
def _tick(state, params, ctx):
    """A step that takes `by` seconds of (fake) calendar time."""
    _CLOCKS[params["clock"]].now += params["by"]
    return {"ticked": params["by"]}


@register("wallclock.sleep")
def _sleep(state, params, ctx):
    """A step that takes `s` seconds of real time (measured into the step's wall_s by the engine)."""
    time.sleep(params["s"])
    return {"slept": params["s"]}


def _chain(spec_id: str, module: str, params: dict, n: int) -> HarnessSpec:
    ids = [f"s{i}" for i in range(n)]
    return HarnessSpec(id=spec_id, entry=ids[0], nodes={i: ModuleNode(module=module, params=params) for i in ids},
                       edges=[Edge(source=a, target=b) for a, b in pairwise(ids)])


def _ticks(name: str, clock: Clock, n: int, by: float = 10.0) -> HarnessSpec:
    _CLOCKS[name] = clock
    return _chain(f"tick-{name}", "wallclock.tick", {"clock": name, "by": by}, n)


def _engine(spec: HarnessSpec, store: EvidenceStore, camp: Campaign) -> Engine:
    return Engine(spec, backends=H.backends(), store=store, campaign=camp)


def _trace_wall(store: EvidenceStore, run_id: str) -> float:
    return sum(s.cost.wall_s for s in store.trace(run_id))


# ----------------------------------------------------------------------- wall_s (summed step time): characterisation
def test_wall_s_campaign_cap_is_summed_step_time_charged_across_runs_and_processes(tmp_path):
    store = EvidenceStore(tmp_path / "s")
    one = _engine(_chain("sl", "wallclock.sleep", {"s": 0.6}, 1), store,
                  Campaign(store, "w", {"wall_s": 0.5})).run("t", {})
    # one step: checked before it at 0 s < 0.5, so it runs and the run completes; its ~0.6 s then reaches the cap
    assert one.status == RunStatus.COMPLETED
    camp = Campaign(store, "w")  # a new campaign object, as a new process would build
    assert camp.spent.wall_s == pytest.approx(_trace_wall(store, one.run_id)) and camp.spent.wall_s >= 0.5
    assert camp.remaining("wall_s") == pytest.approx(0.5 - camp.spent.wall_s)
    two = _engine(_chain("sl", "wallclock.sleep", {"s": 0.6}, 1), store, camp).run("t", {})
    assert two.status == RunStatus.BUDGET_EXCEEDED and "campaign w wall_s" in two.detail
    assert store.trace(two.run_id) == []  # refused before its first step
    store.close()


def test_wall_s_of_a_fork_counts_the_parents_prefix_once(tmp_path):
    store = EvidenceStore(tmp_path / "s")
    spec = _chain("sl", "wallclock.sleep", {"s": 0.1}, 2)
    camp = Campaign(store, "wf", {"wall_s": 3600.0})
    res = _engine(spec, store, camp).run("t", {})
    forked = _engine(spec, store, Campaign(store, "wf")).fork(res.run_id, step_index=0)
    assert forked.status == RunStatus.COMPLETED and camp.members() == [res.run_id, forked.run_id]
    assert [s.node_id for s in store.trace(forked.run_id)] == ["s1"]  # the fork's trace holds only its own step
    assert camp.spent.wall_s == pytest.approx(_trace_wall(store, res.run_id) + _trace_wall(store, forked.run_id))
    store.close()


# ----------------------------------------------------------------------- elapsed_s (calendar time since open)
def test_open_time_is_persisted_and_reopening_never_resets_the_clock(tmp_path):
    clk = Clock(1_000.0)
    store = EvidenceStore(tmp_path / "s")
    camp = Campaign(store, "e", {"elapsed_s": 100.0, "tokens": None}, clock=clk)
    assert camp.opened_at == 1_000.0
    [rec] = store.records_where(CAMPAIGN_OPEN, "campaign_id", "e")
    assert rec["opened_at"] == 1_000.0 and rec["limits"] == {"elapsed_s": 100.0, "tokens": None}
    clk.now = 1_004.0
    assert camp.elapsed() == 4.0 and camp.remaining("elapsed_s") == 96.0
    assert camp.remaining("tokens") is None
    store.close()

    store = EvidenceStore(tmp_path / "s")  # a new process: the open time comes from the store, not from "now"
    later = Clock(1_060.0)
    again = Campaign(store, "e", clock=later)
    assert again.opened_at == 1_000.0 and again.elapsed() == 60.0 and again.remaining("elapsed_s") == 40.0
    assert Campaign(store, "e", {"elapsed_s": 100.0, "tokens": None}, clock=later).opened_at == 1_000.0  # same caps
    with pytest.raises(CampaignError, match="caps are fixed"):  # a longer clock is a new cap: refused
        Campaign(store, "e", {"elapsed_s": 1e9, "tokens": None}, clock=later)
    assert len(store.records_where(CAMPAIGN_OPEN, "campaign_id", "e")) == 1  # re-opening wrote no second open
    later.now = 1_100.0
    with pytest.raises(BudgetExceeded, match="campaign e elapsed_s"):
        again.check()
    with pytest.raises(ValueError, match="unknown budget units"):
        Campaign(store, "e2", {"elapsed_seconds": 1.0}, clock=later)
    store.close()


def test_elapsed_cap_refuses_before_the_next_step_and_every_process_sees_the_same_clock(tmp_path):
    clk = Clock(5_000.0)
    store = EvidenceStore(tmp_path / "s")
    camp = Campaign(store, "e", {"elapsed_s": 15.0}, clock=clk)
    res = _engine(_ticks("e", clk, 3), store, camp).run("t", {})  # each step takes 10 s of calendar time
    # checked at 0 s (ran), at 10 s (ran), at 20 s >= 15: refused before the third step
    assert res.status == RunStatus.BUDGET_EXCEEDED and "campaign e elapsed_s" in res.detail
    assert [s.node_id for s in store.trace(res.run_id)] == ["s0", "s1"]
    store.close()

    store = EvidenceStore(tmp_path / "s")  # a second process, its own clock object reading the same time
    other = Campaign(store, "e", clock=Clock(clk.now))
    assert other.elapsed() == 20.0 and other.remaining("elapsed_s") == -5.0
    second = _engine(_ticks("e", clk, 3), store, other).run("t2", {})
    assert second.status == RunStatus.BUDGET_EXCEEDED and "campaign e elapsed_s" in second.detail
    assert store.trace(second.run_id) == [] and other.members() == [res.run_id, second.run_id]
    store.close()


def test_a_fork_joins_the_same_campaign_clock(tmp_path):
    clk = Clock(10_000.0)
    store = EvidenceStore(tmp_path / "s")
    camp = Campaign(store, "ef", {"elapsed_s": 100.0}, clock=clk)
    spec = _ticks("ef", clk, 2)
    res = _engine(spec, store, camp).run("t", {})
    assert res.status == RunStatus.COMPLETED and clk.now == 10_020.0
    other = Campaign(store, "ef", clock=clk)
    forked = _engine(spec, store, other).fork(res.run_id, step_index=0)
    assert forked.status == RunStatus.COMPLETED and camp.members() == [res.run_id, forked.run_id]
    # one clock: the fork neither restarted it nor added its own (no second campaign_open, same open time)
    assert other.opened_at == camp.opened_at == 10_000.0
    assert len(store.records_where(CAMPAIGN_OPEN, "campaign_id", "ef")) == 1
    assert camp.elapsed() == other.elapsed() == 30.0  # wall time of the parent, the fork and the gap, counted once
    clk.now = 10_100.0
    late = _engine(spec, store, Campaign(store, "ef", clock=clk)).fork(res.run_id, step_index=0)
    assert late.status == RunStatus.BUDGET_EXCEEDED and "campaign ef elapsed_s" in late.detail
    assert store.trace(late.run_id) == [] and clk.now == 10_100.0  # refused before the fork's first step
    store.close()


def test_a_clock_that_went_backwards_fails_closed(tmp_path):
    clk = Clock(2_000.0)
    store = EvidenceStore(tmp_path / "s")
    camp = Campaign(store, "b", {"elapsed_s": 100.0}, clock=clk)
    back = Campaign(store, "b", clock=Clock(1_999.0))  # e.g. a new process on a machine whose clock is behind
    for read in (back.check, back.elapsed, lambda: back.remaining("elapsed_s")):
        with pytest.raises(CampaignClockError, match="earlier than the campaign's recorded open time"):
            read()
    assert issubclass(CampaignClockError, BudgetExceeded)  # so the engine stops before the step, as for any cap
    res = _engine(_ticks("b", clk, 1), store, back).run("t", {})
    assert res.status == RunStatus.BUDGET_EXCEEDED and "earlier than the campaign's recorded open time" in res.detail
    assert store.trace(res.run_id) == []

    clk.now = 2_050.0
    camp.check()
    clk.now = 2_040.0  # backwards, but still after the open time: refused rather than undercounted
    with pytest.raises(CampaignClockError, match="went backwards"):
        camp.check()
    with pytest.raises(CampaignClockError, match="no finite time"):
        Campaign(store, "b", clock=lambda: math.nan).check()
    with pytest.raises(CampaignError, match="no finite time"):
        Campaign(store, "b-nan", {"elapsed_s": 1.0}, clock=lambda: math.inf)
    assert store.records_where(CAMPAIGN_OPEN, "campaign_id", "b-nan") == []  # nothing recorded on that refusal
    store.close()


def test_a_campaign_without_a_recorded_open_time_fails_closed_for_elapsed_and_works_for_the_rest(tmp_path):
    store = EvidenceStore(tmp_path / "s")
    # a campaign opened before elapsed_s existed: its campaign_open record has no opened_at
    store.append_record(CAMPAIGN_OPEN, {"campaign_id": "old", "limits": {"tokens": 10_000}})
    old = Campaign(store, "old")
    assert old.opened_at is None and old.remaining("elapsed_s") is None  # no elapsed cap: the other units work
    old.check()
    res = _engine(_ticks("old", Clock(0.0), 2), store, old).run("t", {})
    assert res.status == RunStatus.COMPLETED
    with pytest.raises(CampaignError, match="no recorded open time"):
        old.elapsed()  # never treated as 0
    with pytest.raises(CampaignError, match="caps are fixed"):  # cannot bolt an elapsed cap onto it either
        Campaign(store, "old", {"tokens": 10_000, "elapsed_s": 60.0})

    # an elapsed cap without a usable open time (missing or malformed): refused, never read as 0 elapsed
    for i, bad in enumerate([None, "yesterday", True, math.nan, math.inf, [1.0]]):
        body = {"campaign_id": f"bad{i}", "limits": {"elapsed_s": 60.0}}
        if bad is not None:
            body["opened_at"] = bad
        store.append_record(CAMPAIGN_OPEN, body)
        with pytest.raises(CampaignError, match="no usable recorded open time"):
            Campaign(store, f"bad{i}", clock=Clock(0.0))
    store.close()


class _RacingStore:
    """Another process opens the same campaign between our read (no campaign_open yet) and our append."""

    def __init__(self, inner: EvidenceStore, rival: dict):
        self._inner, self._rival, self._raced = inner, rival, False

    def records_where(self, kind, key, value):
        if kind == CAMPAIGN_OPEN and not self._raced:
            self._raced = True
            found = self._inner.records_where(kind, key, value)
            self._inner.append_record(CAMPAIGN_OPEN, self._rival)
            return found
        return self._inner.records_where(kind, key, value)

    def __getattr__(self, item):
        return getattr(self._inner, item)


def test_two_processes_opening_one_campaign_both_follow_the_first_open_record(tmp_path):
    store = EvidenceStore(tmp_path / "s")
    rival = {"campaign_id": "r", "limits": {"tokens": 1}, "opened_at": 500.0}
    with pytest.raises(CampaignError, match="caps are fixed"):  # we lost the race with other caps: refused
        Campaign(_RacingStore(store, rival), "r", {"tokens": 5})
    rival2 = {"campaign_id": "r2", "limits": {"elapsed_s": 60.0}, "opened_at": 500.0}
    camp = Campaign(_RacingStore(store, rival2), "r2", {"elapsed_s": 60.0}, clock=Clock(1_000.0))
    assert camp.opened_at == 500.0  # the first record's clock, as every other reader of the store sees it
    with pytest.raises(BudgetExceeded, match="campaign r2 elapsed_s"):
        camp.check()
    store.close()


# ----------------------------------------------------------------------- hard kill, then resume
def test_hard_killed_campaign_keeps_its_clock_and_charges_each_steps_wall_time_once(tmp_path):
    store_dir, ws, marker = tmp_path / "store", tmp_path / "ws", tmp_path / "marker"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(HERE), os.environ.get("PYTHONPATH", "")])}
    t_start = time.time()
    proc = subprocess.Popen([sys.executable, str(HERE / "_campaign_wallclock_crash.py"), str(store_dir), str(ws),
                             str(marker)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 60
        while not marker.exists():
            assert proc.poll() is None, proc.stderr.read().decode()
            assert time.time() < deadline, "child never reached the in-flight step"
            time.sleep(0.05)
        t_in_flight = time.time()
        time.sleep(0.5)
    finally:
        proc.kill()  # hard kill: no cleanup code runs in the child
        proc.wait()
    t_kill = time.time()

    store = EvidenceStore(store_dir)
    camp = Campaign(store, W.CAMPAIGN_ID)  # a new process; real clock
    assert camp.limits == W.CAPS
    opened = camp.opened_at
    assert opened is not None and t_start <= opened <= t_in_flight  # the child recorded the open time
    run1, run2 = camp.members()
    assert store.run_status(run2)[0] == RunStatus.RUNNING
    assert [s.node_id for s in store.trace(run2)] == ["a", "b"]
    at_kill = camp.elapsed()
    assert at_kill >= t_kill - t_in_flight >= 0.5  # the time the killed step was in flight is calendar time
    # ... but it is not in wall_s (summed step time): the killed step has no trace row, its journal wall_s is the
    # backend-reported 0, so wall_s at the kill is exactly the finished steps' measured durations
    assert camp.spent.wall_s == pytest.approx(_trace_wall(store, run1) + _trace_wall(store, run2))
    # the persisted open time bites after the crash: a clock at the cap refuses, a clock before the open fails closed
    with pytest.raises(BudgetExceeded, match=f"campaign {W.CAMPAIGN_ID} elapsed_s"):
        Campaign(store, W.CAMPAIGN_ID, clock=lambda: opened + W.CAPS["elapsed_s"]).check()
    with pytest.raises(CampaignClockError, match="earlier than the campaign's recorded open time"):
        Campaign(store, W.CAMPAIGN_ID, clock=lambda: opened - 1.0).check()

    eng = C.second_engine(store, str(ws), str(marker), Campaign(store, W.CAMPAIGN_ID))
    assert eng.resume(run2).status == RunStatus.COMPLETED
    after = Campaign(store, W.CAMPAIGN_ID)
    assert after.members() == [run1, run2]
    assert after.opened_at == opened  # not reset by the crash or the resume
    assert len(store.records_where(CAMPAIGN_OPEN, "campaign_id", W.CAMPAIGN_ID)) == 1
    t_read = time.time()
    assert after.elapsed() >= t_read - opened and after.elapsed() > at_kill  # still counting from the original open
    # wall_s after the resume: each trace row once. Step c has an 'interrupted' row (wall_s = journalled 0) plus the
    # re-executed row; the journal entry for c is replaced by the interrupted row, not added to it
    trace2 = store.trace(run2)
    assert [s.node_id for s in trace2] == ["a", "b", "c", "c", "d"]
    assert trace2[2].output.get("interrupted") is True and trace2[2].cost.wall_s == 0.0
    assert after.spent.wall_s == pytest.approx(_trace_wall(store, run1) + _trace_wall(store, run2))
    journal_wall = sum(e["cost"]["wall_s"] for e in store.records_where("cost_journal", "run_id", run2))
    assert journal_wall == 0.0  # so the equality above leaves no room for a double charge of the journal
    store.close()
