"""The evidence embargo (evolve/embargo.py, docs/evolve_embargo.md): evolve-split runs touch no held-out data, the
family bound is known before the campaign, and only Kernel verdicts on the gate can accept an edit."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import custom_harness.research  # noqa: F401
from custom_harness.evolve import Evaluator, EvolveConfig, SuiteTask, evolve_generation
from custom_harness.evolve import embargo as E
from custom_harness.evolve import evaluate as V
from custom_harness.evolve.loop import check_evidence_config
from custom_harness.evolve.multiplicity import held_out_target_id
from custom_harness.evolve.selection import PairedSample
from custom_harness.gateway import MockBackend
from custom_harness.kerneld.releases import released
from custom_harness.kerneld.scope import ScopedKernel
from custom_harness.records import RunStatus
from custom_harness.runtime import HarnessSpec
from custom_harness.runtime.spec import END, ModuleNode
from custom_harness.sandbox import LocalSandbox
from test_evolve import PROPOSALS, executor, seed_spec

ROOT = Path(__file__).resolve().parents[1]


def example(name: str) -> HarnessSpec:
    return HarnessSpec.model_validate(yaml.safe_load((ROOT / "examples" / name).read_text(encoding="utf-8")))


def out(score, verified):
    return SimpleNamespace(score=score, verified=verified)


def modules(spec: HarnessSpec) -> set[str]:
    return {n.module for n in spec.nodes.values() if isinstance(n, ModuleNode)}


# ---------------------------------------------------------------- the dev view
@pytest.mark.parametrize("name", ["research_v1.yaml", "research_v2.yaml"])
def test_dev_view_has_no_step_that_touches_held_out_data(name):
    spec = example(name)
    assert modules(spec) & E.TRUTH_MODULES  # the attack surface exists in the full spec
    view = E.dev_view(spec)
    assert not modules(view) & E.TRUTH_MODULES
    assert E.AUDIT_MODULE not in modules(view)
    assert "replicate" not in view.nodes
    assert view.entry == spec.entry


@pytest.mark.parametrize("name", ["research_v1.yaml", "research_v2.yaml"])
def test_dev_view_ends_at_the_report_and_keeps_the_search(name):
    spec = example(name)
    view = E.dev_view(spec)
    report = next(nid for nid, n in view.nodes.items() if isinstance(n, ModuleNode) and n.module == E.REPORT_MODULE)
    assert [e.target for e in view.outgoing(report)] == [END]
    assert view.nodes[report].params["figure"] is False
    assert {"run", "implement", "fix"} <= set(view.nodes)  # the part the proposer can actually edit
    assert all(e.target == END or e.target in view.nodes for e in view.edges)


def test_dev_view_of_a_spec_without_claims_is_the_spec():
    spec = HarnessSpec.model_validate({"id": "s", "entry": "a", "nodes": {"a": {"kind": "module", "module": "x"}}})
    assert E.dev_view(spec) is spec


def test_dev_view_refuses_two_reports():
    doc = example("research_v2.yaml").model_dump(mode="json")
    doc["nodes"]["report2"] = {"kind": "module", "module": E.REPORT_MODULE}
    with pytest.raises(E.EmbargoRefused, match="exactly one"):
        E.dev_view(HarnessSpec.model_validate(doc))


# ---------------------------------------------------------------- the gate view
def test_gate_view_turns_replication_off_and_only_raises_the_declaration():
    doc = example("research_v2.yaml").model_dump(mode="json")
    doc["nodes"]["claim"]["params"]["n_comparisons"] = 500
    spec = HarnessSpec.model_validate(doc)
    raised = E.gate_view(example("research_v2.yaml"), 16)
    assert raised.nodes["claim"].params["n_comparisons"] == 16
    assert raised.nodes["certify"].params["replicate"] is False
    assert E.gate_view(spec, 16).nodes["claim"].params["n_comparisons"] == 500  # never lowered


# ---------------------------------------------------------------- the bound
def test_bound_counts_two_arms_once_and_one_bit_per_generation():
    # 6 gate tasks, 1 claim node, 1 seed, 1 infra retry: 12 claims per arm per generation
    assert E.gate_family_bound(generations=3, n_gate_tasks=6, n_claim_nodes=1, n_seeds=1, infra_retries=1,
                               base=0, reach=1) == 2 * 12 + 12 * 2 + 12 * 4
    assert E.gate_family_bound(generations=1, n_gate_tasks=6, n_claim_nodes=1, n_seeds=1, infra_retries=1,
                               base=5, reach=8) == 5 + 2 * 12 * 8


@pytest.mark.parametrize(("level", "need"), [(0.1, 4), (0.05, 5), (0.0167, 6), (0.5, 1)])
def test_min_gate_tasks(level, need):
    assert E.min_gate_tasks(level) == need
    assert 2.0 ** -need <= level


def test_a_gate_that_can_never_accept_is_refused_before_any_run(store):
    tasks = [SimpleNamespace(task_dir=Path("unused"))] * 3
    with pytest.raises(E.EmbargoRefused, match="can never reach"):
        E.plan_campaign(seed_spec(), store, tasks, generations=1, n_seeds=1, infra_retries=1, gate_alpha=0.1)


def test_a_resumed_campaign_keeps_its_plan_and_its_generation_limit(store):
    first = E.resolve_plan(store, "c", 0, 3, lambda: E.EmbargoPlan(96, 3, 0.033))
    def never():
        raise AssertionError("a resume must not recompute the plan")
    assert E.resolve_plan(store, "c", 2, 1, never) == first
    with pytest.raises(E.EmbargoRefused, match="planned for 3"):
        E.resolve_plan(store, "c", 2, 2, never)


# ---------------------------------------------------------------- evidence
def test_task_level_gives_one_difference_per_task():
    s = PairedSample([("a", 0), ("a", 1), ("b", 0), ("c", 0)], [1.0, 3.0, 5.0], [("c", 0)])
    t = E.task_level(s)
    assert t.units == [("a", 0), ("b", 0)]
    assert t.diffs == [2.0, 5.0]


def test_evolve_evidence_needs_a_gain_across_tasks():
    s = PairedSample([("a", 0), ("b", 0), ("c", 0)], [0.5, 0.5, 0.5], [])
    assert E.evolve_evidence(s, require_ci=True, max_excluded_frac=0.25)["passed"]
    flat = PairedSample([("a", 0), ("b", 0)], [0.0, 0.0], [])
    assert not E.evolve_evidence(flat, require_ci=True, max_excluded_frac=0.25)["passed"]
    lost = PairedSample([("a", 0), ("b", 0)], [1.0], [("b", 0)])
    assert E.evolve_evidence(lost, require_ci=True, max_excluded_frac=0.25)["infra_aborted"]


def test_verdict_flips_compare_certificates_per_task():
    arms = {"cand": {("a", 0): out(1.5, True), ("b", 0): out(0.2, False), ("c", 0): out(None, False)},
            "par": {("a", 0): out(0.1, False), ("b", 0): out(1.0, True), ("c", 0): out(1.0, True)}}
    f = E.verdict_flips(list(arms["cand"]), lambda u: arms["cand"][u], lambda u: arms["par"][u])
    assert (f.won, f.lost, f.tied, f.excluded) == (("a",), ("b",), (), ("c",))
    assert f.excluded_units == 1


def test_a_dev_gain_with_no_new_certificate_is_never_accepted():
    """The audit's case: an edit that raises attempts lifts the dev score on every unit and earns nothing held out."""
    units = [(f"t{i}", 0) for i in range(8)]
    f = E.verdict_flips(units, lambda u: out(0.9, False), lambda u: out(0.1, False))
    d = E.flips_gate(f, level=0.1, max_excluded_frac=0.25)
    assert not d["passed"]
    assert "no certificate" in d["reason"]


@pytest.mark.parametrize(("won", "lost", "passes"), [(4, 0, True), (3, 0, False), (5, 1, False), (7, 1, True)])
def test_sign_test_at_level_one_tenth(won, lost, passes):
    f = E.Flips(tuple(f"w{i}" for i in range(won)), tuple(f"l{i}" for i in range(lost)), units=won + lost)
    assert E.flips_gate(f, level=0.1, max_excluded_frac=0.25)["passed"] is passes


def test_infrastructure_loss_on_the_gate_is_inconclusive_not_a_rejection():
    f = E.Flips(("a",), units=4, excluded_units=2)
    d = E.flips_gate(f, level=0.1, max_excluded_frac=0.25)
    assert d["infra_aborted"] and not d["passed"]


def _family_tasks():
    return [SuiteTask(n, Path(n), "gate", family="held") for n in ("a", "b")]


def test_held_out_family_accepts_unequal_real_gains():
    """The rule this replaces needed a t-interval lower bound >= 0 at n = 2 (t = 12.7), which rejected these."""
    cand = {("a", 0): out(1.0, True), ("b", 0): out(1.0, True)}
    par = {("a", 0): out(0.1, False), ("b", 0): out(0.5, False)}
    r = E.lofo_flips(_family_tasks(), ["held"], [], (0,), cand.__getitem__, par.__getitem__, max_excluded_frac=0.25)
    assert r["passed"]
    assert r["ood_drop"] == {"family:held": 2}


def test_held_out_family_rejects_a_net_loss_of_certificates():
    cand = {("a", 0): out(0.1, False), ("b", 0): out(0.1, False)}
    par = {("a", 0): out(1.0, True), ("b", 0): out(0.1, False)}
    r = E.lofo_flips(_family_tasks(), ["held"], [], (0,), cand.__getitem__, par.__getitem__, max_excluded_frac=0.25)
    assert not r["passed"]
    assert not r["infra_aborted"]


# ---------------------------------------------------------------- scoring
def test_dev_only_score():
    assert V.dev_only_score(RunStatus.INFRA_FAILED, 0.5) is None
    assert V.dev_only_score(RunStatus.COMPLETED, 0.5) == 0.5
    assert V.dev_only_score(RunStatus.FAILED, 0.5) == -1.0


def _executed(targets=()):
    res = SimpleNamespace(status=RunStatus.COMPLETED, run_id="r", cost=V.Cost(),
                          state={"best": {"dev_score": 0.9}, "baseline_dev": 0.6, "task": {"metric": "accuracy"},
                                 "certificate": {"label": "CERTIFIED-INTERNAL"}, "audit": {"passed": True}})
    return V.ExecutedRun("h", SuiteTask("t", Path("t"), "evolve", family="f"), 0, result=res, targets=list(targets))


class NoReleases:
    def emit(self, *a, **k):
        raise AssertionError("an embargoed evolve-split run releases nothing")


def test_embargoed_evolve_run_scores_on_dev_and_releases_nothing(monkeypatch):
    monkeypatch.setattr(V, "failure_signatures", lambda store, run_id: [])
    monkeypatch.setattr(V, "dev_withheld", lambda store, run_id: False)
    o = V.finish_run(_executed(), store=None, releases=NoReleases(), embargoed=True)
    assert o.score == pytest.approx(0.5)
    assert o.verified is False
    assert o.cert_label is None  # even if a certificate were in the state, its label does not reach the proposer


def test_a_private_read_in_a_dev_only_run_stops_the_campaign():
    with pytest.raises(V.EmbargoBroken):
        V.finish_run(_executed(["some-target"]), store=None, releases=NoReleases(), embargoed=True)


def test_mode_and_evaluator_must_agree():
    plan = E.EmbargoPlan(16, 1, 0.1)
    with pytest.raises(ValueError, match="fail closed"):
        check_evidence_config(EvolveConfig(evidence="embargo"), SimpleNamespace(plan=None), ablated=False,
                              branching=None)
    with pytest.raises(ValueError, match="fail closed"):
        check_evidence_config(EvolveConfig(), SimpleNamespace(plan=plan), ablated=False, branching=None)
    with pytest.raises(ValueError, match="unknown evidence mode"):
        check_evidence_config(EvolveConfig(evidence="x"), SimpleNamespace(plan=None), ablated=False, branching=None)
    assert check_evidence_config(EvolveConfig(evidence="embargo"), SimpleNamespace(plan=plan), ablated=False,
                                 branching=None) is True
    assert check_evidence_config(EvolveConfig(), SimpleNamespace(), ablated=False, branching=None) is False


# ---------------------------------------------------------------- a whole generation, scripted models
@pytest.fixture
def big_suite(tmp_path):
    tasks = []
    for i, split in enumerate(["evolve", "evolve", "gate", "gate", "gate", "gate"]):
        d = tmp_path / f"task{i}"
        subprocess.run([sys.executable, str(ROOT / "examples/tasks/make_rings_task.py"), str(d),
                        "--n-train", "300", "--n-test", "200", "--seed", str(100 + i)],
                       check=True, capture_output=True)
        tasks.append(SuiteTask(f"rings{i}", d, split, family="rings" if split == "evolve" else "rings-heldout"))
    return tasks


def test_generation_accepts_through_the_gate_without_touching_evolve_targets(store, tmp_path, big_suite, monkeypatch):
    """The acceptance a legacy campaign cannot reach (tests/test_evolve.py, strict xfail): same structural fix, same
    scripted models. The parent gives up after the first crash, so it certifies nothing; the fix certifies on the
    gate."""
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    llm = MockBackend(executor)
    factory = lambda key: LocalSandbox(tmp_path / "sb" / key, i_understand_no_isolation=True)  # noqa: E731
    # the fix costs model calls the crashing parent never made, and two evolve tasks cannot give a t-interval:
    # both limits are relaxed here on purpose, and only here
    cfg = EvolveConfig(n_edits=4, race_rounds=2, evidence="embargo", require_evolve_ci=False,
                       embargo_max_cost_increase=1000.0)
    parent = seed_spec()
    gate_tasks = [t for t in big_suite if t.split == "gate"]
    plan = E.plan_campaign(parent, store, gate_tasks, generations=1, n_seeds=1, infra_retries=cfg.infra_retries,
                           gate_alpha=cfg.gate_alpha)
    ev = Evaluator({"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store, factory, big_suite,
                   plan=plan)
    proposer = MockBackend([json.dumps(PROPOSALS)])

    res = evolve_generation(parent, big_suite, evaluator=ev, proposer=proposer, store=store, cfg=cfg)

    assert res.accepted, res.decision
    assert res.new_parent.nodes["run"].params["max_fixes"] == 2
    assert res.loop_label == "MECHANICAL"  # internal gate data, not external or future
    assert "stop before any held-out data" in proposer.requests[0].messages[0].content
    # evolve targets: no claim registered, no release. Gate targets: one accept/reject bit each.
    evolve_targets = {held_out_target_id(t.task_dir) for t in big_suite if t.split == "evolve"}
    gate_targets = {held_out_target_id(t.task_dir) for t in gate_tasks}
    claimed = {t for e in ScopedKernel(store).ledger.entries("prereg")
               for t in e.body["claim"]["generalization_targets"]}
    assert not claimed & evolve_targets
    assert claimed <= gate_targets
    rel = released(store)
    assert {r["note"] for r in rel} == {"evolve.gate:decision"}
    assert sorted(r["dataset_id"] for r in rel) == sorted(gate_targets)


def test_candidate_rejected_on_public_evidence_costs_no_gate_run(store, tmp_path, big_suite, monkeypatch):
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    llm = MockBackend(executor)
    factory = lambda key: LocalSandbox(tmp_path / "sb" / key, i_understand_no_isolation=True)  # noqa: E731
    # default cost cap: the fix spends far more tokens than the parent that gives up, so it stops at the cap
    cfg = EvolveConfig(n_edits=4, race_rounds=2, evidence="embargo", require_evolve_ci=False)
    parent = seed_spec()
    plan = E.plan_campaign(parent, store, [t for t in big_suite if t.split == "gate"], generations=1, n_seeds=1,
                           infra_retries=cfg.infra_retries, gate_alpha=cfg.gate_alpha)
    ev = Evaluator({"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}, store, factory, big_suite,
                   plan=plan)

    res = evolve_generation(parent, big_suite, evaluator=ev, proposer=MockBackend([json.dumps(PROPOSALS)]), store=store,
                            cfg=cfg)

    assert not res.accepted
    assert [r["rejected_on"] for r in store.records("lineage") if r.get("status") == "rejected_gate"] == ["cost"]
    assert ScopedKernel(store).ledger.entries("prereg") == []
    assert released(store) == []
