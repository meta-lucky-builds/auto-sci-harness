"""The evolution Evaluator under the per-completion race (control/evalrace.py, GOAL §5 L1).

The first tests show what was unsafe (each fails on 4bf1a3d, before the single-writer change): a unit asked for by
two threads at once ran twice; writers of the same blob collided on one temp file (PermissionError on Windows); a new
Evaluator after a crash re-ran and re-charged every finished unit. The rest show the wiring: per-completion racing
over a real Evaluator decides exactly as the batch race (slots 1 and 3, with infrastructure outages), evaluates in the
batch order with one slot, never runs a unit twice, and commits every run on one thread."""

import hashlib
import threading
import time

import pytest

from custom_harness.budget import Budget, persistent_budget
from custom_harness.errors import InfraError
from custom_harness.evolve import Evaluator, EvolveConfig, SuiteTask, race
from custom_harness.gateway import ChatRequest, Message, MockBackend
from custom_harness.records import Cost
from custom_harness.runtime import HarnessSpec, ModuleNode, register
from custom_harness.sandbox import LocalSandbox
from custom_harness.store.blobs import BlobStore

RUNS: list[tuple] = []  # (effect, task_dir name, seed, thread id) of every harness run
_lock = threading.Lock()
BARRIER: dict[str, threading.Barrier | None] = {"b": None}


def _u(*parts) -> float:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


@register("test.evalrace.scored")
def _scored(state, params, ctx):
    """A run whose score is a deterministic function of (effect, task, seed): 0.5 x clip(dev improvement)."""
    task = state["input"]["task_dir"].replace("\\", "/").rsplit("/", 1)[-1]
    seed, effect = state["_seed"], params["effect"]
    with _lock:
        RUNS.append((effect, task, seed, threading.get_ident()))
    b = BARRIER["b"]
    if b is not None:
        try:
            b.wait()
        except threading.BrokenBarrierError:
            pass
    if params.get("sleep"):
        time.sleep(params["sleep"] * _u("sleep", effect, task, seed))
    ctx.llm("executor", ChatRequest(messages=[Message(role="user", content="spend some tokens here")]))
    noise = (_u(task, seed) - 0.5) + 0.6 * (_u(task, seed, effect) - 0.5)
    return {"audit": {"passed": True}, "best": {"dev_score": 1.0 + effect + 0.5 * noise}, "baseline_dev": 1.0}


def spec(effect: float, sleep: float = 0.0) -> HarnessSpec:
    return HarnessSpec(id="scored", entry="a",
                       nodes={"a": ModuleNode(module="test.evalrace.scored",
                                              params={"effect": effect, "sleep": sleep})},
                       edges=[])


@pytest.fixture(autouse=True)
def _reset():
    RUNS.clear()
    BARRIER["b"] = None
    yield
    BARRIER["b"] = None


def tasks(tmp_path, n=4):
    out = []
    for i in range(n):
        (tmp_path / f"t{i}").mkdir(parents=True, exist_ok=True)
        out.append(SuiteTask(f"t{i}", tmp_path / f"t{i}", "evolve", family="A"))
    return out


class CountingBudget(Budget):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.charges: list[Cost] = []

    def charge(self, cost):
        self.charges.append(cost)
        super().charge(cost)


def evaluator(store, tmp_path, suite, *, factory=None, **kw):
    llm = MockBackend(lambda req: "ok fine")
    factory = factory or (lambda key: LocalSandbox(tmp_path / "sb" / key, i_understand_no_isolation=True))
    return Evaluator({"executor": llm}, store, factory, suite, **kw)


# ---------------------------------------------------------------- what was unsafe (fail on the old code)
def test_one_unit_asked_for_by_two_threads_runs_once_and_is_charged_once(store, tmp_path):
    """Old code (seen on 4bf1a3d): both threads missed the cache and both started the harness (Evaluator.calls == 2,
    two runs of the module); the second run then crashed with PermissionError on the shared blob temp file (see the
    blob test below), else it would have been charged a second time. Now the Evaluator has one writer thread; a
    second one is refused before any run."""
    suite = tasks(tmp_path, 1)
    budget = CountingBudget(limits={})
    ev = evaluator(store, tmp_path, suite, campaign_budget=budget)
    BARRIER["b"] = threading.Barrier(2, timeout=1.5)  # holds a run until a second run joins it (or 1.5 s pass)
    errors: list[BaseException] = []

    def ask():
        try:
            ev.outcome(spec(0.1), ("t0", 0))
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=ask) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(RUNS) == 1, f"the unit ran {len(RUNS)} times"
    assert len(budget.charges) == 1 and ev.calls == 1
    assert len(errors) == 1 and "single writer" in str(errors[0])


def test_writers_of_the_same_blob_do_not_collide(tmp_path):
    """Old code: every writer of a blob used `<digest>.tmp`. Two concurrent runs storing identical bytes (the same
    spec version, workspace snapshot or checkpoint) collided on it: on Windows the second open fails with
    PermissionError (seen in the two-thread test above on the old code). Many rounds of 6 simultaneous writers."""
    bs = BlobStore(tmp_path / "blobs")
    errors: list[BaseException] = []
    for i in range(60):
        data = f"identical bytes from concurrent runs, round {i}".encode() * 200
        go = threading.Barrier(6, timeout=10)

        def put(data=data, go=go):
            try:
                go.wait()
                bs.put(data)
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=put) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        assert bs.get(hashlib.sha256(data).hexdigest()) == data
    assert errors == [], errors[:3]
    assert not [p for p in (tmp_path / "blobs").rglob("*.tmp")]  # no temp file left behind


def test_resume_after_a_crash_neither_re_runs_nor_re_charges_finished_units(store, tmp_path):
    """Old code (the control half below): a new Evaluator after a crash had an empty cache, so every finished unit
    ran again and was charged again. With a durable key the finished units come back from the store."""
    suite = tasks(tmp_path, 2)
    units = [("t0", 0), ("t1", 0), ("t0", 1)]
    b1 = persistent_budget(store, "camp", {"tokens": None})
    ev1 = evaluator(store, tmp_path, suite, campaign_budget=b1, durable_key="camp-1")
    first = [ev1.outcome(spec(0.2), u) for u in units]
    spent = b1.spent.prompt_tokens + b1.spent.completion_tokens
    assert spent > 0 and len(RUNS) == 3
    # the process dies here; a new one opens the same store
    b2 = persistent_budget(store, "camp", {"tokens": None})
    ev2 = evaluator(store, tmp_path, suite, campaign_budget=b2, durable_key="camp-1")
    again = [ev2.outcome(spec(0.2), u) for u in units]
    assert ev2.calls == 0 and len(RUNS) == 3
    assert [(o.score, o.run_id) for o in again] == [(o.score, o.run_id) for o in first]
    assert b2.spent.prompt_tokens + b2.spent.completion_tokens == spent
    # control: without a durable key (the old behaviour) the same resume re-runs and re-charges
    b3 = persistent_budget(store, "camp", {"tokens": None})
    ev3 = evaluator(store, tmp_path, suite, campaign_budget=b3)
    for u in units:
        ev3.outcome(spec(0.2), u)
    assert ev3.calls == 3 and b3.spent.prompt_tokens + b3.spent.completion_tokens == 2 * spent


def test_a_crash_between_charge_and_record_charges_each_real_run_once(store, tmp_path, monkeypatch):
    """The durable record is written AFTER the charge: a crash in between re-runs the unit on resume. That second run
    really happened, so it is charged too; no run is charged twice and none is left uncharged."""
    suite = tasks(tmp_path, 1)
    b1 = persistent_budget(store, "camp", {"tokens": None})
    ev1 = evaluator(store, tmp_path, suite, campaign_budget=b1, durable_key="k")
    real = store.append_record

    def crash(kind, body):
        if kind == "evolve_unit":
            raise KeyboardInterrupt("killed between the charge and the unit record")
        return real(kind, body)

    monkeypatch.setattr(store, "append_record", crash)
    with pytest.raises(KeyboardInterrupt):
        ev1.outcome(spec(0.0), ("t0", 0))
    monkeypatch.setattr(store, "append_record", real)
    one_run = b1.spent.prompt_tokens + b1.spent.completion_tokens
    b2 = persistent_budget(store, "camp", {"tokens": None})
    ev2 = evaluator(store, tmp_path, suite, campaign_budget=b2, durable_key="k")
    ev2.outcome(spec(0.0), ("t0", 0))
    assert len(RUNS) == 2 and ev2.calls == 1
    assert b2.spent.prompt_tokens + b2.spent.completion_tokens == 2 * one_run  # two real runs, each charged once


# ---------------------------------------------------------------- the wiring
def _race_world(tmp_path, store, effects, *, slots, allocator, factory=None, sleep=0.0, rounds=2, n_tasks=3,
                seeds=(0, 1, 2)):
    from custom_harness.control.evalrace import race_evaluator
    suite = tasks(tmp_path, n_tasks)
    ev = evaluator(store, tmp_path, suite, factory=factory)
    units = [(t.task_id, s) for t in suite for s in seeds]
    parent = spec(0.0)
    parent_scores = {u: ev.outcome(parent, u).score for u in units}
    cands = {spec(e, sleep).spec_hash(): spec(e, sleep) for e in effects}
    RUNS.clear()
    if allocator == "batch":
        rr = race(list(cands), units, lambda h, u: ev.score(cands[h], u), parent_scores, rounds=rounds, keep=1)
        return rr, ev, None
    rr, _alloc, ex = race_evaluator(cands, units, ev, parent_scores, slots=slots, rounds=rounds, keep=1, timeout_s=60)
    return rr, ev, ex


def _same(a, b):
    return (a.survivors == b.survivors and a.stats == b.stats and a.evaluations == b.evaluations
            and a.evaluations_fixed_k == b.evaluations_fixed_k)


@pytest.mark.parametrize("effects", [(0.3, -0.2, 0.0), (0.05, 0.1, -0.4, 0.2, 0.0)])
def test_per_completion_over_the_evaluator_decides_as_the_batch_race(tmp_path, effects):
    from custom_harness.kernel import Kernel
    from custom_harness.store import EvidenceStore
    results = {}
    for name, alloc, slots in (("batch", "batch", 1), ("pc1", "per_completion", 1), ("pc3", "per_completion", 3)):
        st = EvidenceStore(tmp_path / f"store-{name}")
        Kernel(st).approve_adapter("ml", approver="pytest")
        rr, ev, ex = _race_world(tmp_path / name, st, effects, slots=slots, allocator=alloc, sleep=0.02)
        runs = list(RUNS)
        results[name] = (rr, ev, ex, runs)
        st.close()
    batch = results["batch"][0]
    for name in ("pc1", "pc3"):
        rr, ev, ex, runs = results[name]
        assert _same(rr, batch), name
        keys = [(e, t, s) for e, t, s, _ in runs]
        assert len(keys) == len(set(keys)) == rr.evaluations  # every needed unit ran exactly once
    # one slot: the harness runs happen in exactly the batch race's order
    assert [r[:3] for r in results["pc1"][3]] == [r[:3] for r in results["batch"][3]]
    # three slots really overlapped, and every commit happened on the writer thread
    ev3, runs3 = results["pc3"][1], results["pc3"][3]
    assert len({th for *_, th in runs3}) > 1
    assert ev3._writer == threading.get_ident()


def test_outages_change_no_race_decision_and_are_excluded_not_scored(tmp_path):
    """M0 under the allocator: sandbox creation fails for some attempts (Docker down). Units whose retry succeeds
    count normally; units that stay down are excluded (score None), never scored 0. The per-completion race over
    the flaky environment decides exactly as the batch race over the same flaky environment, and both slots."""
    from custom_harness.kernel import Kernel
    from custom_harness.store import EvidenceStore

    def flaky(root):
        seen: dict[str, int] = {}

        def factory(key):
            seen[key] = seen.get(key, 0) + 1
            task_seed = key.split("-", 1)[1]
            if task_seed == "t1-2" or (seen[key] == 1 and _u("down", task_seed) < 0.3):
                raise InfraError("Cannot connect to the Docker daemon", transient=True)
            return LocalSandbox(root / key, i_understand_no_isolation=True)
        return factory

    effects = (0.3, -0.2, 0.1)
    results = {}
    for name, alloc, slots in (("batch", "batch", 1), ("pc1", "per_completion", 1), ("pc3", "per_completion", 3)):
        st = EvidenceStore(tmp_path / f"store-{name}")
        Kernel(st).approve_adapter("ml", approver="pytest")
        rr, ev, _ = _race_world(tmp_path / name, st, effects, slots=slots, allocator=alloc,
                                factory=flaky(tmp_path / name / "sb"))
        results[name] = (rr, ev.infra_failures, {k: o.score for k, o in ev.cache.items() if k[1:] == ("t1", 2)},
                         dict(ev.cache))
        st.close()
    batch, infra_b, t12, cache = results["batch"]
    assert infra_b > 0 and t12 and all(s is None for s in t12.values())  # outages happened; t1-2 is excluded
    for name in ("pc1", "pc3"):
        rr, infra, t12x, _ = results[name]
        assert _same(rr, batch), name
        assert infra == infra_b and all(s is None for s in t12x.values())
    for h, st in batch.stats.items():  # every excluded unit was dropped from the pair, not scored 0
        down = sum(1 for (hh, t, s), o in cache.items() if hh == h and o.score is None)
        assert st["excluded"] == down and st["n_units"] + down == sum(1 for k in cache if k[0] == h)
    assert any(st["excluded"] for st in batch.stats.values())


def test_concurrent_race_needs_the_owner_ack_and_unknown_allocators_are_refused():
    from custom_harness.evolve.loop import check_race_config
    check_race_config(EvolveConfig())
    check_race_config(EvolveConfig(race_allocator="per_completion"))
    check_race_config(EvolveConfig(race_allocator="per_completion", race_slots=3, race_concurrency_ack=True))
    for bad in (EvolveConfig(race_allocator="per_completion", race_slots=3), EvolveConfig(race_slots=2),
                EvolveConfig(race_allocator="voodoo"), EvolveConfig(race_allocator="per_completion", race_slots=0)):
        with pytest.raises(ValueError):
            check_race_config(bad)


def test_a_failure_mid_race_still_commits_every_run_in_flight(store, tmp_path):
    """A run raising (not an infra failure) propagates unchanged, as in the sequential Evaluator; the runs still in
    flight are waited for and committed (charged once, cached), and nothing new starts."""
    from custom_harness.control.evalrace import race_evaluator

    @register("test.evalrace.boom")
    def _boom(state, params, ctx):
        raise SystemExit("engine-level crash")  # not caught by the engine's node error handling

    suite = tasks(tmp_path, 3)
    budget = CountingBudget(limits={})
    ev = evaluator(store, tmp_path, suite, campaign_budget=budget)
    units = [(t.task_id, 0) for t in suite]
    parent_scores = {u: ev.outcome(spec(0.0), u).score for u in units}
    boom = HarnessSpec(id="boom", entry="a", nodes={"a": ModuleNode(module="test.evalrace.boom")}, edges=[])
    cands = {boom.spec_hash(): boom, spec(0.1, 0.05).spec_hash(): spec(0.1, 0.05)}
    n_before = len(budget.charges)
    with pytest.raises(SystemExit):
        race_evaluator(cands, units, ev, parent_scores, slots=3, rounds=1, timeout_s=30)
    good = [k for k in ev.cache if k[0] == spec(0.1, 0.05).spec_hash()]
    assert len(budget.charges) - n_before == len(good)  # every committed run charged once, none lost
    assert not ev._in_flight
