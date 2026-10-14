"""Per-completion racing allocator (GOAL §5 L1): wakes on each completion, re-reads the store, refills slots with no
batch barrier, terminates when drained, and decides EXACTLY as the batch race on the same inputs."""

import math
import random
import threading
import time

import pytest

from custom_harness.control.allocator import (
    AllocatorError,
    MemoryUnitStore,
    RaceAllocator,
    race_per_completion,
)
from custom_harness.evolve.selection import race


class SimExecutor:
    """Deterministic simulator: holds submitted work; each pump() completes ONE pending item chosen by a seeded RNG
    (any completion order is possible, so the allocator cannot rely on submission order)."""

    def __init__(self, table, seed=0, hold=()):
        self.table, self.rng, self.hold = table, random.Random(seed), set(hold)
        self.pending: list = []
        self.completed: list = []

    def submit(self, c, u, report, fail):
        self.pending.append((c, u, report, fail))

    def finish(self, key):
        i = next(i for i, p in enumerate(self.pending) if (p[0], p[1]) == key)
        c, u, report, _ = self.pending.pop(i)
        self.completed.append((c, u))
        report(c, u, self.table[(c, u)])

    def pump(self):
        free = [p for p in self.pending if (p[0], p[1]) not in self.hold]
        if free:
            p = self.rng.choice(free)
            self.finish((p[0], p[1]))


def _world(seed):
    rng = random.Random(seed)
    n_c, n_t, n_s = rng.randint(1, 6), rng.randint(1, 5), rng.randint(1, 3)
    cands = [f"c{i}" for i in range(n_c)]
    units = [(f"t{t}", s) for t in range(n_t) for s in range(n_s)]
    rng.shuffle(units)
    discrete = rng.random() < 0.4  # ties
    def draw(bias):
        if rng.random() < 0.1:
            return None  # infra exclusion
        return float(rng.randint(0, 3)) if discrete else rng.gauss(bias, 1.0)
    parent = {u: draw(0.0) for u in units}
    table = {(c, u): draw(rng.uniform(-1.5, 1.5)) for c in cands for u in units}
    return cands, units, parent, table, rng.randint(1, 4), rng.randint(1, 3)


def _same(a, b):
    def norm(x):
        return {k: tuple("nan" if isinstance(v, float) and math.isnan(v) else v for v in d.values())
                for k, d in x.stats.items()}
    return (a.survivors == b.survivors and norm(a) == norm(b) and a.evaluations == b.evaluations
            and a.evaluations_fixed_k == b.evaluations_fixed_k)


@pytest.mark.parametrize("seed", range(300))
def test_decisions_are_identical_to_the_batch_race(seed):
    cands, units, parent, table, rounds, keep = _world(seed)
    batch = race(cands, units, lambda c, u: table[(c, u)], parent, rounds=rounds, keep=keep, n_boot=10)
    for slots in (1, 3):
        ex = SimExecutor(table, seed=seed * 7 + slots)
        alloc = RaceAllocator(cands, units, parent, store=MemoryUnitStore(), executor=ex, slots=slots,
                              rounds=rounds, keep=keep, n_boot=10)
        got = alloc.run(timeout_s=5)
        assert _same(got, batch), (seed, slots, got, batch)
        assert ex.pending == [] and len(ex.completed) == batch.evaluations  # drained, nothing evaluated twice


def test_units_fewer_than_rounds_matches_the_batch_race():
    """race() treats only the LAST chunk index as the final round, even when it is empty."""
    cands, units = ["a", "b", "c"], [("t0", 0), ("t1", 0)]
    parent = {u: 0.0 for u in units}
    table = {("a", units[0]): 1.0, ("a", units[1]): -3.0, ("b", units[0]): -1.0, ("b", units[1]): 0.5,
             ("c", units[0]): 0.2, ("c", units[1]): 0.1}
    batch = race(cands, units, lambda c, u: table[(c, u)], parent, rounds=3, keep=1, n_boot=10)
    got = RaceAllocator(cands, units, parent, store=MemoryUnitStore(), executor=SimExecutor(table), slots=2,
                        rounds=3, keep=1, n_boot=10).run(timeout_s=5)
    assert _same(got, batch)


def test_no_batch_barrier_every_free_slot_is_refilled_at_once():
    cands = ["a", "b"]
    units = [(f"t{i}", 0) for i in range(6)]
    parent = {u: 0.0 for u in units}
    table = {(c, u): 1.0 for c in cands for u in units}
    ex = SimExecutor(table, seed=3)
    alloc = RaceAllocator(cands, units, parent, store=MemoryUnitStore(), executor=ex, slots=3, rounds=1, n_boot=10)
    alloc.run(timeout_s=5)
    assert len(alloc.idle_log) >= 4
    for in_flight, waiting in alloc.idle_log:  # a slot is idle only when no needed unit is waiting
        assert in_flight == 3 or waiting == 0, alloc.idle_log


def test_wakes_on_each_completion_and_terminates_when_drained():
    cands, units = ["a"], [("t0", 0), ("t1", 0), ("t2", 0)]
    parent = {u: 0.0 for u in units}
    table = {("a", u): 1.0 for u in units}
    ex = SimExecutor(table)
    alloc = RaceAllocator(cands, units, parent, store=MemoryUnitStore(), executor=ex, slots=3, rounds=1, n_boot=10)
    res = alloc.run(timeout_s=5)
    assert res.survivors == ["a"] and ex.pending == []
    assert alloc.wakes == 1 + 3  # the initial read, then one wake per finished unit (no polling, no batch)


def test_a_unit_finishing_during_analysis_is_not_lost():
    """Unit u1 finishes while the allocator is analysing the wake caused by u0 (after its store read). Nothing else
    will ever finish, so if that completion were lost the allocator would wait forever."""
    cands, units = ["a"], [("t0", 0), ("t1", 0)]
    parent = {u: 0.0 for u in units}
    table = {("a", u): 1.0 for u in units}
    ex = SimExecutor(table, hold={("a", ("t1", 0))})  # pump() never completes u1
    fired = []

    def during_analysis(alloc):
        if ("a", ("t0", 0)) in alloc.store.finished() and not fired:
            fired.append(True)
            ex.finish(("a", ("t1", 0)))  # lands AFTER this iteration's store read

    alloc = RaceAllocator(cands, units, parent, store=MemoryUnitStore(), executor=ex, slots=2, rounds=1, n_boot=10,
                          on_analysis=during_analysis)
    res = alloc.run(timeout_s=2)
    assert fired, "the mid-analysis completion did not happen: the test is not testing anything"
    assert res.survivors == ["a"] and res.stats["a"]["n_units"] == 2


def test_the_store_is_the_source_of_truth_recorded_results_are_not_re_evaluated():
    """Results already in the store (e.g. left by an interrupted allocator) are used, never re-run."""
    cands, units = ["a", "b"], [("t0", 0), ("t1", 0), ("t2", 0), ("t3", 0)]
    parent = {u: 0.0 for u in units}
    table = {(c, u): (1.0 if c == "a" else -1.0) + 0.1 * i for c in cands for i, u in enumerate(units)}
    pre = {k: table[k] for k in list(table)[:3]}
    ex = SimExecutor(table)
    got = RaceAllocator(cands, units, parent, store=MemoryUnitStore(pre), executor=ex, slots=2, rounds=2,
                        n_boot=10).run(timeout_s=5)
    batch = race(cands, units, lambda c, u: table[(c, u)], parent, rounds=2, n_boot=10)
    assert _same(got, batch) and not set(pre) & set(ex.completed)


def test_a_failed_evaluation_fails_closed():
    class Boom(SimExecutor):
        def pump(self):
            c, u, _, fail = self.pending.pop()
            fail(c, u, RuntimeError("worker crashed"))
    units = [("t0", 0)]
    with pytest.raises(AllocatorError, match="worker crashed"):
        RaceAllocator(["a"], units, {units[0]: 0.0}, store=MemoryUnitStore(), executor=Boom({}), slots=1,
                      rounds=1, n_boot=10).run(timeout_s=2)


def test_parent_must_cover_every_unit_as_in_the_batch_race():
    with pytest.raises(ValueError, match="parent"):
        RaceAllocator(["a"], [("t0", 0)], {}, store=MemoryUnitStore(), executor=SimExecutor({}), slots=1).run()


def test_threaded_drop_in_matches_the_batch_race_under_random_timing():
    cands, units, parent, table, rounds, keep = _world(11)
    rng = random.Random(5)
    delays = {k: rng.uniform(0, 0.01) for k in table}
    lock, active, peak = threading.Lock(), [0], [0]

    def evaluate(c, u):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(delays[(c, u)])
        with lock:
            active[0] -= 1
        return table[(c, u)]

    batch = race(cands, units, lambda c, u: table[(c, u)], parent, rounds=rounds, keep=keep, n_boot=10)
    got = race_per_completion(cands, units, evaluate, parent, slots=3, rounds=rounds, keep=keep, n_boot=10,
                              timeout_s=10)
    assert _same(got, batch) and peak[0] <= 3
