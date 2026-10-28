"""L6.9 branch and route vs invariant 16 (GOAL §10): routing to a branch never bypasses the leave-one-family-out /
leave-one-domain-out gate, and the router never reads sealed data (invariant 6: the sealed split is touched only by
`finalize_routed`). No model calls: scripted outcomes (test_router.Script reads each gate target through the Kernel
vault, so the release ledger and the target attribution are the real ones)."""

from __future__ import annotations

import ast
import builtins
import json
import os
from pathlib import Path

import pytest

from custom_harness.evolve import Evaluator, EvolveConfig, SuiteTask, evolve_generation
from custom_harness.evolve.branches import BRANCH_KIND, BranchConfig, BranchRefused, lofo_note, propose_branch
from custom_harness.evolve.router import RouteRefused, build_router
from custom_harness.gateway import MockBackend
from custom_harness.kerneld.releases import Releases, released
from test_router import CA, NAMES, SEEDS, TRUNK, Script, _branches, _gate_tasks, tid

ROOT = Path(__file__).resolve().parents[1]


def _suite(tmp_path) -> list[SuiteTask]:
    """Two domains. Family E (domain ml) is evolved; H (domain ml) is a HELD-OUT family inside domain ml; B (domain
    bio) is the held-out domain."""
    return ([SuiteTask(f"e{i}", tmp_path, "evolve", family="E", domain="ml") for i in range(2)]
            + [SuiteTask(f"gE{i}", tmp_path, "gate", family="E", domain="ml") for i in range(4)]
            + [SuiteTask(f"gH{i}", tmp_path, "gate", family="H", domain="ml") for i in range(2)]
            + [SuiteTask(f"gB{i}", tmp_path, "gate", family="B", domain="bio") for i in range(3)])


def _score(h_delta: float):
    """Trunk 0.5. The candidate is +1.0 on family E, `h_delta` on the held-out family H (both domain ml), and -0.1
    on domain bio (so it never wins bio). Small jitter so intervals are proper."""
    def score(name, family, task, seed):
        j = 0.01 * ((sum(map(ord, task)) + 3 * seed) % 5)
        if name == "T":
            return 0.5 + j
        return 0.5 + {"E": 1.0, "H": h_delta, "B": -0.1}[family] + 0.01 * ((sum(map(ord, task)) + seed) % 3)
    return score


def _domain_branch(store, tasks, h_delta, **kw):
    sc = Script(store, tasks, score=_score(h_delta))
    return propose_branch(TRUNK, CA, tasks, seeds=SEEDS, outcome=sc.outcome, store=store,
                          releases=Releases.live(store), globally_accepted=False,
                          cfg=BranchConfig(cluster_key="domain"), **kw)


def test_a_domain_win_that_hides_a_held_out_family_regression_never_becomes_a_route(store, tmp_path):
    """The candidate wins domain ml pooled (E dominates) while regressing on the held-out family H inside ml. A
    route ml -> candidate would apply it to H: exactly what the leave-one-family-out gate forbids (invariant 16)."""
    tasks = _suite(tmp_path)
    assert _domain_branch(store, tasks, h_delta=-0.2) is None
    assert store.records(BRANCH_KIND) == []
    assert build_router(store, TRUNK, cluster_key="domain").routes == ()
    # the LOFO verdict was computed and released as a bit (it reached the ledger like any gate bit), and it was 0
    notes = {b["note"] for b in released(store)}
    assert any(n.startswith("branch_lofo:") for n in notes)


def test_the_same_domain_win_without_the_regression_is_routed_and_carries_its_lofo_bit(store, tmp_path):
    tasks = _suite(tmp_path)
    rec = _domain_branch(store, tasks, h_delta=0.1)  # positive control: H improves too
    assert rec is not None and rec["won"] == ["ml"]
    lofo = rec["lofo"]
    assert lofo["passed"] is True
    assert lofo["note"] == lofo_note(TRUNK.spec_hash(), CA.spec_hash(), {"ml": CA.spec_hash()}, True)
    # the bit is ledgered on every target attributed to the held-out groups' runs (H, and domain bio = family B)
    assert {b["dataset_id"] for b in released(store) if b["note"] == lofo["note"]} == set(lofo["targets"])
    held_out = {tid(f"gH{i}") for i in range(2)} | {tid(f"gB{i}") for i in range(3)}
    assert held_out <= set(lofo["targets"])
    # ... and on nothing else: Script registers each gate run before its read, so attributed_read_log attributes the
    # reads to their own runs (a read naming an unknown run would be attributed to every run, rule of bd1acbb, and
    # the gE targets would appear here too)
    assert sorted(lofo["targets"]) == sorted(held_out)
    assert dict(build_router(store, TRUNK, cluster_key="domain").routes) == {"ml": CA.spec_hash()}


def test_lofo_margin_is_never_looser_than_the_loops(store, tmp_path):
    tasks = _suite(tmp_path)
    # H regresses by 0.05; a branch eps of 0.5 would hide it, but the loop's eps (0.0) wins: no branch
    assert _domain_branch(store, tasks, h_delta=-0.05, lofo_eps=0.0) is None
    with pytest.raises(BranchRefused, match="non_regression_eps"):
        BranchConfig(non_regression_eps=-1.0)


def test_a_campaign_without_a_held_out_family_cannot_branch(store, tmp_path):
    tasks = [SuiteTask(f"e{i}", tmp_path, "evolve", family="A") for i in range(2)] + \
        [SuiteTask(f"g{i}", tmp_path, "gate", family="A") for i in range(3)]
    sc = Script(store, tasks, score=lambda n, f, t, s: 0.5 if n == "T" else 0.9 + 0.01 * s)
    with pytest.raises(BranchRefused, match="LOFO/LODO"):
        propose_branch(TRUNK, CA, tasks, seeds=SEEDS, outcome=sc.outcome, store=store,
                       releases=Releases.live(store), globally_accepted=False)


def test_router_refuses_records_without_a_released_passing_lofo_bit(store, tmp_path):
    """Each forged record sits on the REAL trunk, next to the honest ones, in a fresh store: the router refuses it
    for its own reason."""
    from custom_harness.kernel import Kernel
    from custom_harness.store import EvidenceStore
    tasks = _gate_tasks(tmp_path)
    _branches(store, tasks)
    assert build_router(store, TRUNK).routes  # positive control
    cases = (
        (lambda r: r.pop("lofo"), "whitelist"),                                     # old format: no LOFO at all
        (lambda r: r["lofo"].update(passed=False), "leave-one-family/domain-out"),  # a failing bit
        (lambda r: r["lofo"].update(note="branch_lofo:forged"), "leave-one-family/domain-out"),  # another note
        (lambda r: r["lofo"].update(routes={}), "leave-one-family/domain-out"),     # a table without its own route
        (lambda r: r["lofo"].update(targets=[*r["lofo"]["targets"], "tgt-never-released"]),
         "LOFO/LODO bit not in the release ledger"),                                # note right, release missing
    )
    for i, (mutate, why) in enumerate(cases):
        st = EvidenceStore(tmp_path / f"st{i}")
        Kernel(st).approve_adapter("ml", approver="pytest")
        (good, _), _ = _branches(st, tasks)
        bad = json.loads(json.dumps(good))
        mutate(bad)
        st.append_record(BRANCH_KIND, bad)
        with pytest.raises(RouteRefused, match=why):
            build_router(st, TRUNK)
        st.close()


def test_the_generation_loop_does_not_branch_around_a_held_out_family(store, tmp_path):
    """Through evolve_generation: the candidate is rejected globally (held-out family H regresses), wins domain ml,
    and still gets no branch, because its route would carry the regression to H."""
    from test_evolve import seed_spec
    from test_lofo import ONE_EDIT

    tasks = _suite(tmp_path)
    parent = seed_spec()
    score = _score(-0.2)

    class Ev(Evaluator):
        def __init__(self, st):
            super().__init__({}, st, lambda k: None, tasks)
            self.script = Script(st, tasks, names={parent.spec_hash(): "T"},
                                 score=lambda n, f, t, s: score("T" if n == "T" else "C", f, t, s % 1000))

        def outcome(self, spec, unit):
            key = (spec.spec_hash(), *unit)
            if key not in self.cache:
                self.calls += 1
                self.cache[key] = self.script.outcome(spec, unit)
            return self.cache[key]

    res = evolve_generation(parent, tasks, evaluator=Ev(store), proposer=MockBackend([ONE_EDIT]), store=store,
                            cfg=EvolveConfig(seeds=SEEDS, n_edits=1, race_rounds=1),
                            branching=BranchConfig(cluster_key="domain"))
    assert not res.accepted and res.candidates[-1]["rejected_on"] == "held_out_gate", res.decision
    assert res.branch is None and store.records(BRANCH_KIND) == []
    assert build_router(store, parent, cluster_key="domain").routes == ()


# ---------------------------------------------------------------------------------------------- sealed data
def test_the_router_never_reads_sealed_data(store, tmp_path, monkeypatch):
    """Build a router and route sealed tasks while every file read under a sealed task directory, every Kernel
    vault read and every held-out target lookup is trapped. Routing uses the public descriptor only."""
    tasks = _gate_tasks(tmp_path)
    _branches(store, tasks)
    sealed_root = tmp_path / "sealed"
    sealed = []
    for f in "ABC":
        d = sealed_root / f"s{f}"
        (d / "private").mkdir(parents=True)
        (d / "private" / "labels.csv").write_text("y\n1\n", encoding="utf-8")
        sealed.append(SuiteTask(f"s{f}", d, "sealed", family=f))
    touched: list[str] = []
    real_open, real_os_open = builtins.open, os.open

    def guard(path):
        if isinstance(path, str | os.PathLike) and str(sealed_root) in os.path.abspath(os.fspath(path)):
            touched.append(str(path))

    def trap_open(file, *a, **k):
        guard(file)
        return real_open(file, *a, **k)

    def trap_os_open(path, *a, **k):
        guard(path)
        return real_os_open(path, *a, **k)

    import custom_harness.evolve.multiplicity as mult
    import custom_harness.kernel as kernel_mod
    vault_reads: list = []
    vault_cls = type(kernel_mod.Kernel(store).vault)
    real_read = vault_cls.read

    def trap_vault_read(self, *a, **k):
        vault_reads.append(a)
        return real_read(self, *a, **k)

    monkeypatch.setattr(builtins, "open", trap_open)
    monkeypatch.setattr(os, "open", trap_os_open)
    monkeypatch.setattr(vault_cls, "read", trap_vault_read)
    monkeypatch.setattr(mult, "held_out_target_id", lambda d: touched.append(f"target:{d}") or "x")
    monkeypatch.setattr("custom_harness.evolve.router.held_out_target_id",
                        lambda d: touched.append(f"target:{d}") or "x")
    router = build_router(store, TRUNK)
    picks = {t.task_id: NAMES[router.route(t).spec_hash()] for t in sealed}
    assert picks == {"sA": "CA", "sB": "CB", "sC": "T"}
    assert touched == [] and vault_reads == []


def test_sealed_access_in_router_py_is_confined_to_finalize_routed():
    """Static: the sealed-run capability and the sealed target lookup appear in router.py only inside the one-shot
    human command `finalize_routed` (and the import line)."""
    tree = ast.parse((ROOT / "src/custom_harness/evolve/router.py").read_text(encoding="utf-8"))
    sensitive = {"_SEALED_ACCESS", "held_out_target_id", "run_harness"}
    owners: dict[str, set[str]] = {}

    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, ast.FunctionDef | ast.ClassDef):
                name = child.name if owner == "<module>" else owner
            if isinstance(child, ast.Name) and child.id in sensitive:
                owners.setdefault(child.id, set()).add(owner)
            visit(child, name)

    visit(tree, "<module>")
    assert owners and all(o == {"finalize_routed"} for o in owners.values()), owners
