"""L6.9 branch and route (GOAL §5): branches from per-cluster gate verdict BITS (ledgered releases), a router that
routes only on released bits, a fail-closed select/materialize seam, and the one-shot sealed test `finalize_routed`.
No model calls: scripted outcomes (each scripted run reads its gate target through the Kernel vault, so the default
target attribution and the release ledger are the real ones)."""

from __future__ import annotations

import gc
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from custom_harness.evolve import Evaluator, EvolveConfig, RunOutcome, SuiteTask, evolve_generation
from custom_harness.evolve.branches import (
    BRANCH_KIND,
    BranchConfig,
    BranchRefused,
    cluster_note,
    lofo_note,
    propose_branch,
)
from custom_harness.evolve.finalize import FinalizeRefused, finalize
from custom_harness.evolve.router import (
    EmbargoViolation,
    GateEvidenceView,
    Router,
    RouteRefused,
    build_router,
    finalize_routed,
)
from custom_harness.gateway import MockBackend
from custom_harness.kernel import DatasetRole, Kernel
from custom_harness.kerneld.releases import Releases, released
from custom_harness.records import Cost
from custom_harness.runtime import HarnessSpec
from custom_harness.sandbox import LocalSandbox
from custom_harness.store import EvidenceStore

ROOT = Path(__file__).resolve().parents[1]
TINY = {"id": "tiny", "entry": "a", "nodes": {"a": {"kind": "llm", "prompt": "hi", "output_key": "o"}}}
SEEDS = (0, 1)


def _spec(name: str) -> HarnessSpec:
    return HarnessSpec.model_validate(TINY | {"id": name})


TRUNK, CA, CB = _spec("trunk"), _spec("branch-a"), _spec("branch-b")
NAMES = {TRUNK.spec_hash(): "T", CA.spec_hash(): "CA", CB.spec_hash(): "CB"}


def tid(task_id: str) -> str:
    """A test target id that does not contain the task id (real ids are content hashes)."""
    return "tgt-" + hashlib.sha256(task_id.encode()).hexdigest()[:10]


def scripted(name: str, family: str, task: str, seed: int) -> float:
    """Trunk 0.5 everywhere; CA 0.9 on family A and 0.1 on B; CB the mirror image (jitter differs per arm). Pooled,
    every harness scores about 0.5: no single harness is globally better, each branch wins one cluster."""
    k = sum(map(ord, task)) + 3 * seed
    if name == "T":
        return 0.5 + 0.01 * (k % 5)
    good = {"CA": "A", "CB": "B"}[name]
    return (0.9 if family == good else 0.1) + 0.01 * ((k + 2) % 7)


class Script:
    """Scripted outcomes. Each (spec, unit) 'run' reads its task's held-out target through the Kernel vault, as a
    real run does, so `private_reads(run_id=...)` attributes the target to the run."""

    def __init__(self, store, tasks, names=NAMES, score=scripted):
        self.store, self.names, self.score = store, names, score
        self.tasks = {t.task_id: t for t in tasks}
        self.vault = Kernel(store).vault
        for t in tasks:
            if t.split == "gate":
                self.vault.register_dataset(tid(t.task_id), DatasetRole.TEST, public={"x.csv": b"x\n1\n"},
                                            private={"y.csv": f"y\n{t.task_id}\n".encode()})
        self.cache: dict = {}

    def outcome(self, spec, unit):
        key = (spec.spec_hash(), *unit)
        if key not in self.cache:
            t = self.tasks[unit[0]]
            name = self.names.get(spec.spec_hash(), "?")
            rid = f"run-{name}-{unit[0]}-{unit[1]}"
            if t.split == "gate":
                self.vault.read(tid(t.task_id), "y.csv", purpose="leakscan", run_id=rid)
            self.cache[key] = RunOutcome(unit[0], rid, unit[1], self.score(name, t.family, unit[0], unit[1]), True,
                                         "completed", Cost(prompt_tokens=100), [])
        return self.cache[key]


def _gate_tasks(tmp_path) -> list[SuiteTask]:
    return ([SuiteTask(f"gA{i}", tmp_path, "gate", family="A") for i in range(3)]
            + [SuiteTask(f"gB{i}", tmp_path, "gate", family="B") for i in range(3)])


def _branches(store, tasks, releases=None):
    sc = Script(store, tasks)
    rel = releases or Releases.live(store)
    recs = [propose_branch(TRUNK, c, tasks, seeds=SEEDS, outcome=sc.outcome, store=store, releases=rel,
                           globally_accepted=False) for c in (CA, CB)]
    return recs, sc


def _numbers(obj, path=""):
    """(path, value) of every non-bool number inside obj."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _numbers(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _numbers(v, f"{path}[{i}]")
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        yield path, obj


# ---------------------------------------------------------------------------------------------- branches
def test_a_cluster_winner_that_does_not_win_globally_becomes_a_branch(store, tmp_path):
    tasks = _gate_tasks(tmp_path)
    (ra, rb), _ = _branches(store, tasks)
    assert ra["won"] == ["A"] and rb["won"] == ["B"]
    assert ra["clusters"]["B"] == {"n_tasks": 3, "tested": True, "wins": False,
                                   "targets": sorted(tid(f"gB{i}") for i in range(3)),
                                   "note": cluster_note(ra["branch_id"], "B")}
    # every tested cluster bit is a ledgered verdict-bit release on each target its runs read (2 branches x 2 x 3)
    # plus each branch's leave-one-family-out bit on every held-out family's target (A and B are both held out
    # here: 2 branches x 6 targets; invariant 16, branches.py)
    rel = released(store)
    assert len(rel) == 12 + 12 and {(b["channel"], b["alphabet"]) for b in rel} == {("evolution_score",
                                                                                     "verdict_bit")}
    assert {b["note"] for b in rel} == ({cluster_note(r["branch_id"], c) for r in (ra, rb) for c in "AB"}
                                        | {lofo_note(r["branch_id"]) for r in (ra, rb)})
    assert ra["lofo"] == {"passed": True, "targets": sorted(tid(f"g{f}{i}") for f in "AB" for i in range(3)),
                          "note": lofo_note(ra["branch_id"])}
    # aggregate only: the stored record holds counts and a timestamp, never a mean, CI bound or score
    for rec in store.records(BRANCH_KIND):
        assert {p for p, _ in _numbers(rec)} <= {".at", ".clusters.A.n_tasks", ".clusters.B.n_tasks"}
        assert not any(t in json.dumps(rec) for t in ("gA0", "gB1"))  # no gate task id either (only target ids)


def test_a_global_winner_or_a_loser_makes_no_branch_and_a_global_winner_releases_nothing(store, tmp_path):
    tasks = _gate_tasks(tmp_path)
    sc = Script(store, tasks)
    rel = Releases.live(store)
    assert propose_branch(TRUNK, CA, tasks, seeds=SEEDS, outcome=sc.outcome, store=store, releases=rel,
                          globally_accepted=True) is None
    assert released(store) == [] and sc.cache == {}  # nothing computed, nothing released
    loser = Script(store, tasks, score=lambda n, f, t, s: 0.5 if n == "T" else 0.4)
    assert propose_branch(TRUNK, CA, tasks, seeds=SEEDS, outcome=loser.outcome, store=store, releases=rel,
                          globally_accepted=False) is None
    assert store.records(BRANCH_KIND) == [] and len(released(store)) == 6  # the losing bits are still releases


def test_one_task_clusters_and_unattributed_runs_are_never_tested(store, tmp_path):
    tasks = [SuiteTask("gA0", tmp_path, "gate", family="A"), SuiteTask("gB0", tmp_path, "gate", family="B"),
             SuiteTask("gB1", tmp_path, "gate", family="B")]
    sc = Script(store, tasks, score=lambda n, f, t, s: 0.5 if n == "T" else 0.9 + 0.01 * s)
    rec = propose_branch(TRUNK, CA, tasks, seeds=SEEDS, outcome=sc.outcome, store=store,
                         releases=Releases.live(store), globally_accepted=False)
    assert rec["clusters"]["A"] == {"n_tasks": 1, "tested": False, "wins": False, "why": "too_few_tasks"}
    assert rec["won"] == ["B"]  # the one-task cluster's bit would be a per-task gate value: never computed
    # runs that read no registered target: their bit cannot be ledgered, so it is never computed (fail closed)
    nothing = propose_branch(TRUNK, CB, tasks, seeds=SEEDS, store=store, releases=Releases.live(store),
                             globally_accepted=False,
                             outcome=lambda sp, u: RunOutcome(u[0], "", u[1], 0.9 if sp is CB else 0.1, True,
                                                              "completed", Cost(prompt_tokens=1), []))
    assert nothing is None


def test_branching_refuses_sealed_tasks_and_private_cluster_keys(store, tmp_path):
    tasks = _gate_tasks(tmp_path) + [SuiteTask("s0", tmp_path, "sealed", family="A")]
    with pytest.raises(BranchRefused, match="sealed"):
        propose_branch(TRUNK, CA, tasks, seeds=SEEDS, outcome=lambda *a: None, store=store,
                       releases=Releases.live(store), globally_accepted=False)
    with pytest.raises(BranchRefused, match="public task attribute"):
        BranchConfig(cluster_key="task_id")


# ---------------------------------------------------------------------------------------------- the router
def test_router_routes_each_cluster_to_its_released_winner(store, tmp_path):
    tasks = _gate_tasks(tmp_path)
    _branches(store, tasks)
    r = build_router(store, TRUNK)
    assert dict(r.routes) == {"A": CA.spec_hash(), "B": CB.spec_hash()}
    pick = {f: r.route(SuiteTask(f"new-{f}", tmp_path, "sealed", family=f)).spec_hash() for f in "ABC"}
    assert pick == {"A": CA.spec_hash(), "B": CB.spec_hash(), "C": TRUNK.spec_hash()}  # unrouted -> trunk
    # branches made against another trunk are stale for this one: no routes
    assert build_router(store, _spec("other-trunk")).routes == ()


def test_select_and_materialize_fail_closed(store, tmp_path):
    tasks = _gate_tasks(tmp_path)
    _branches(store, tasks)
    r = build_router(store, TRUNK)
    with pytest.raises(RouteRefused, match="no family"):
        r.select(SuiteTask("x", tmp_path, "sealed", family=""))
    with pytest.raises(RouteRefused, match="neither the trunk nor a routed branch"):
        r.materialize(_spec("stranger").spec_hash())
    forged = Router(r.trunk, r.cluster_key, r.routes,
                    tuple((h, json.dumps(TINY | {"id": "swapped"})) if h == CA.spec_hash() else (h, d)
                          for h, d in r.specs))
    with pytest.raises(RouteRefused, match="re-hash"):
        forged.route(SuiteTask("a", tmp_path, "sealed", family="A"))


def test_two_winners_on_one_cluster_are_refused(store, tmp_path):
    tasks = _gate_tasks(tmp_path)
    ca2 = _spec("branch-a2")
    names = NAMES | {ca2.spec_hash(): "CA"}
    sc = Script(store, tasks, names=names)
    for c in (CA, ca2):
        propose_branch(TRUNK, c, tasks, seeds=SEEDS, outcome=sc.outcome, store=store, releases=Releases.live(store),
                       globally_accepted=False)
    with pytest.raises(RouteRefused, match="won by 2 branches"):
        build_router(store, TRUNK)


# ---------------------------------------------------------------------------------------------- attacks
def test_attack_router_reading_per_task_gate_values_is_refused(store, tmp_path):
    """A route policy that reads per-task gate values straight from the store (the loop's host-only `harness_edit`
    per_task attribution) and routes on them. The attack must really obtain those values; the router must refuse
    the table it produces, because no released bit backs a per-task route."""
    tasks = _gate_tasks(tmp_path)
    _branches(store, tasks)
    store.append_record("harness_edit", {"candidate": CB.spec_hash(), "per_task": {
        "gA0": {"split": "gate", "n": 2, "mean_diff": 0.31}, "gA1": {"split": "gate", "n": 2, "mean_diff": -0.2}}})
    stolen: dict = {}

    def per_task_policy(view):
        for rec in store.records("harness_edit"):
            for t, r in rec.get("per_task", {}).items():
                if r.get("split") == "gate":
                    stolen[t] = r["mean_diff"]
        return {t: CB.spec_hash() for t, d in stolen.items() if d > 0}  # route gA0's 'kind' of task to CB

    with pytest.raises(RouteRefused, match="not backed by a released winning verdict bit"):
        build_router(store, TRUNK, policy=per_task_policy)
    assert stolen == {"gA0": 0.31, "gA1": -0.2}  # the attack happened: per-task gate values were read

    def cluster_policy(view):  # same stolen value, expressed as a cluster route: A -> CB (CB's A bit was False)
        return {"A": CB.spec_hash()} if stolen["gA0"] > 0 else {}

    with pytest.raises(RouteRefused, match="not backed by a released winning verdict bit"):
        build_router(store, TRUNK, policy=cluster_policy)


def test_attack_policy_probing_the_evidence_view_finds_only_bits(store, tmp_path):
    tasks = _gate_tasks(tmp_path)
    _branches(store, tasks)
    seen = []

    def probing_policy(view):
        seen.append(view)
        for name in ("per_task", "scores", "_store", "gate_mean"):
            with pytest.raises(EmbargoViolation):
                getattr(view, name)
        with pytest.raises(EmbargoViolation):
            view.store = store  # an attempt to attach a handle
        return {}

    r = build_router(store, TRUNK, policy=probing_policy)
    view = seen[0]
    assert view.attempts == ["per_task", "scores", "_store", "gate_mean"] and r.routes == ()
    # introspection: everything the view references is a hash, a cluster name or a bit
    leaves = []
    for k, v in view._bits.items():
        leaves += [*k, v]
    assert {type(x) for x in leaves} == {str, bool}
    assert not any(isinstance(x, float) for x in gc.get_referents(view._bits))
    assert not any("gA" in x or "gB" in x for x in leaves if isinstance(x, str))
    with pytest.raises(EmbargoViolation):
        GateEvidenceView({(CA.spec_hash(), "A"): 0.31})  # a continuous value cannot even enter the view


def test_attack_records_that_smuggle_values_or_skip_the_ledger_are_refused(store, tmp_path):
    tasks = _gate_tasks(tmp_path)
    (ra, _), _ = _branches(store, tasks)
    assert build_router(store, TRUNK).routes  # positive control
    # the store is append-only: each poisoned record is filed under its own trunk
    bad_trunk = _spec("trunk2")
    smuggled = json.loads(json.dumps(ra)) | {"trunk": bad_trunk.spec_hash(), "per_task": {"gA0": 0.31}}
    store.append_record(BRANCH_KIND, smuggled)
    with pytest.raises(RouteRefused, match="whitelist"):
        build_router(store, bad_trunk)
    t3 = _spec("trunk3")
    mean_in_cluster = json.loads(json.dumps(ra)) | {"trunk": t3.spec_hash()}
    mean_in_cluster["clusters"]["A"]["mean_diff"] = 0.4
    store.append_record(BRANCH_KIND, mean_in_cluster)
    with pytest.raises(RouteRefused, match="whitelist"):
        build_router(store, t3)
    # a well-formed record whose bit was never released (forged note): refused by the ledger re-check
    t4 = _spec("trunk4")
    unreleased = json.loads(json.dumps(ra)) | {"trunk": t4.spec_hash()}
    unreleased["clusters"]["A"]["note"] = "branch:forged:a"
    store.append_record(BRANCH_KIND, unreleased)
    with pytest.raises(RouteRefused, match="not in the release ledger"):
        build_router(store, t4)


# ---------------------------------------------------------------------------------------------- loop hook
def test_the_generation_loop_creates_a_branch_when_a_cluster_wins_but_the_gate_rejects(store, tmp_path):
    from test_evolve import seed_spec
    from test_lofo import ONE_EDIT

    tasks = [SuiteTask(f"e{i}", tmp_path, "evolve", family="A") for i in range(3)]
    tasks += [SuiteTask(f"gA{i}", tmp_path, "gate", family="A") for i in range(3)]
    tasks += [SuiteTask(f"h{i}", tmp_path, "gate", family="H") for i in range(2)]
    parent = seed_spec()

    def score(name, family, task, seed):
        j = 0.001 * (sum(map(ord, task)) % 7 + 3 * (seed % 1000))
        return 0.0 if name == "T" else (-0.5 if family == "H" else 1.0) + j

    class Ev(Evaluator):
        def __init__(self, st):
            super().__init__({}, st, lambda k: None, tasks)
            self.script = Script(st, tasks, names={parent.spec_hash(): "T"}, score=score)

        def outcome(self, spec, unit):
            key = (spec.spec_hash(), *unit)
            if key not in self.cache:
                self.calls += 1
                self.cache[key] = self.script.outcome(spec, unit)
            return self.cache[key]

    def run(st, branching):
        return evolve_generation(parent, tasks, evaluator=Ev(st), proposer=MockBackend([ONE_EDIT]), store=st,
                                 cfg=EvolveConfig(seeds=SEEDS, n_edits=1, race_rounds=1), branching=branching)

    res = run(store, BranchConfig())
    assert not res.accepted and res.candidates[-1]["rejected_on"] == "held_out_gate", res.decision
    assert res.branch is not None and res.branch["won"] == ["A"]
    assert res.branch["clusters"]["H"]["wins"] is False
    r = build_router(store, parent)
    assert dict(r.routes) == {"A": res.branch["spec_hash"]}
    # default (no branching) on a fresh store: the same rejected generation, and no branch, no cluster release
    st2 = EvidenceStore(tmp_path / "store2")
    Kernel(st2).approve_adapter("ml", approver="pytest")
    res2 = run(st2, None)
    assert res2.decision == res.decision and res2.branch is None and st2.records(BRANCH_KIND) == []
    assert not [b for b in released(st2) if b["note"].startswith("branch")]
    st2.close()


# ---------------------------------------------------------------------------------------------- sealed: finalize only
def _rings(tmp_path: Path, name: str, seed: int) -> Path:
    out = tmp_path / "tasks" / name
    subprocess.run([sys.executable, str(ROOT / "examples/tasks/make_rings_task.py"), str(out), "--n-train", "60",
                    "--n-test", "30", "--seed", str(seed)], check=True, capture_output=True)
    return out


def _sealed(tmp_path):
    return [SuiteTask(f"s{f}{i}", _rings(tmp_path, f"s{f}{i}", 10 * i + ord(f)), "sealed", family=f)
            for f in "AB" for i in range(2)]


def test_router_beats_the_single_best_harness_on_both_clusters_of_the_sealed_split(store, tmp_path):
    tasks = _gate_tasks(tmp_path)
    _branches(store, tasks)
    router = build_router(store, TRUNK)
    sealed = _sealed(tmp_path)
    seeds = (0, 1, 2)
    calls = []

    def runner(spec, task, seed):
        calls.append((NAMES[spec.spec_hash()], task.task_id, seed))
        return RunOutcome(task.task_id, f"r{len(calls)}", seed, scripted(NAMES[spec.spec_hash()], task.family,
                                                                         task.task_id, seed), True, "completed",
                          Cost(), [])

    with pytest.raises(FinalizeRefused, match="approver"):
        finalize_routed(router, sealed, approver=" ", store=store, runner=runner)
    assert calls == []
    out = finalize_routed(router, sealed, approver="alice", store=store, runner=runner, seeds=seeds)
    # every single harness, scored on the same sealed units (from the script); the best one by pooled mean
    def mean(xs):
        return sum(xs) / len(xs)

    single = {n: {c: mean([scripted(n, c, u["task_id"], u["seed"]) for u in out["units"] if u["cluster"] == c])
                  for c in "AB"} for n in ("T", "CA", "CB")}
    routed = {c: mean([u["score"] for u in out["units"] if u["cluster"] == c]) for c in "AB"}
    best = max(single, key=lambda n: mean(list(single[n].values())))
    assert mean(list(routed.values())) > mean(list(single[best].values())) + 0.3  # pooled over the clusters
    for c in "AB":
        assert routed[c] >= max(single[n][c] for n in single) - 1e-12  # never worse than any harness on a cluster
        assert routed[c] > single[best][c] or routed[c] > single["T"][c] + 0.3
        assert out["per_cluster"][c]["lo"] > 0  # paired vs the trunk (the gate's single best), per cluster
    assert sum(routed[c] > single[best][c] + 0.3 for c in "AB") >= 1  # strictly better where `best` is weak
    assert {u["route"] for u in out["units"] if u["cluster"] == "A"} == {CA.spec_hash()}
    assert out["paired"]["lo"] > 0 and out["paired"]["n"] == 12
    n = len(calls)
    with pytest.raises(FinalizeRefused, match="already finalized"):
        finalize_routed(router, sealed, approver="bob", store=store, runner=runner, seeds=seeds)
    with pytest.raises(FinalizeRefused, match="already finalized"):  # the one-shot is shared with plain finalize
        finalize(TRUNK, sealed, approver="bob", backends={}, store=store, sandbox_factory=lambda k: None)
    assert len(calls) == n


def test_finalize_routed_runs_the_real_harness_per_route_once(store, tmp_path):
    """Integration: the default runner is run_harness with the sealed capability; each sealed task runs its
    routed spec (and the trunk as the paired baseline)."""
    tasks = _gate_tasks(tmp_path)
    _branches(store, tasks)
    router = build_router(store, TRUNK)
    sealed = _sealed(tmp_path)
    llm = MockBackend(lambda r: "{}")
    factory = lambda k: LocalSandbox(tmp_path / "sb" / k, i_understand_no_isolation=True)  # noqa: E731
    out = finalize_routed(router, sealed, approver="alice", store=store, backends={"executor": llm},
                          sandbox_factory=factory)
    ran = {(u["task_id"], store.get_run(u["run_id"]).harness_hash, store.get_run(u["baseline_run_id"]).harness_hash)
           for u in out["units"]}
    assert ran == {(t.task_id, (CA if t.family == "A" else CB).spec_hash(), TRUNK.spec_hash()) for t in sealed}
    assert len(llm.requests) == 2 * len(sealed)
