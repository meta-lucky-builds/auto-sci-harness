"""Leave-one-family-out / leave-one-domain-out gates (GOAL §5 L6 step 8, invariant 16).

A family (domain) is HELD OUT when it has gate tasks and no evolve task. A campaign without one is refused (fail
closed), and a candidate is accepted only if, on EACH held-out family/domain separately, the Student-t lower bound of
its paired difference to the parent is >= -eps, in addition to the pooled gate. Scores here come from a fixed table
(no harness runs), so every decision is deterministic.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
import yaml

from custom_harness.evolve import Evaluator, EvolveConfig, RunOutcome, SuiteTask, evolve_generation
from custom_harness.evolve.driver import load_suite
from custom_harness.gateway import MockBackend
from custom_harness.records import Cost
from custom_harness.research import suite as suite_mod
from custom_harness.research.suite import assign_splits, build_suite, check_split_isolation, rings, write_task
from test_evolve import PROPOSALS, seed_spec
from test_suite_builder import _fake_real_data

ONE_EDIT = json.dumps({"edits": [PROPOSALS["edits"][0]]})  # max_fixes 0 -> 2: a valid control-flow edit


class TableEvaluator(Evaluator):
    """Scores from a table: score(arm, task_id, seed) with arm 'parent' or 'cand'. No harness is run."""

    def __init__(self, store, tasks, parent_hash, score):
        super().__init__({}, store, lambda k: None, tasks)
        self.parent_hash, self._score = parent_hash, score

    def outcome(self, spec, unit):
        key = (spec.spec_hash(), unit[0], unit[1])
        if key not in self.cache:
            self.calls += 1
            arm = "parent" if spec.spec_hash() == self.parent_hash else "cand"
            # equal cost in both arms: the cost term (test_evolve_evidence.py) passes, fail closed without cost data
            self.cache[key] = RunOutcome(unit[0], "", unit[1], self._score(arm, unit[0], unit[1]), False,
                                         "completed", Cost(prompt_tokens=100), [])
        return self.cache[key]


def _tasks(tmp_path: Path, held_out: list[str]) -> list[SuiteTask]:
    """Family A: 3 evolve + 4 gate tasks. Family H (held out): the given gate task ids."""
    tasks = [SuiteTask(f"e{i}", tmp_path, "evolve", family="A") for i in range(3)]
    tasks += [SuiteTask(f"g{i}", tmp_path, "gate", family="A") for i in range(4)]
    tasks += [SuiteTask(t, tmp_path, "gate", family="H") for t in held_out]
    return tasks


def _scorer(held_out_gain: float):
    """Candidate: +1 (slightly varied) on evolve and on family A's gate tasks; `held_out_gain` (slightly varied)
    on family H. Parent: 0 everywhere."""
    def score(arm: str, task: str, seed: int) -> float:
        if arm == "parent":
            return 0.0
        jitter = 0.001 * (int(task[1:]) + 3 * (seed % 1000))
        return (held_out_gain if task.startswith("h") else 1.0) + jitter
    return score


def _run(store, tmp_path, tasks, score, seeds=(0, 1)):
    parent = seed_spec()
    ev = TableEvaluator(store, tasks, parent.spec_hash(), score)
    return evolve_generation(parent, tasks, evaluator=ev, proposer=MockBackend([ONE_EDIT]), store=store,
                             cfg=EvolveConfig(seeds=seeds, n_edits=1, race_rounds=1))


# ---------------------------------------------------------------- the gate
def test_pooled_gain_with_held_out_family_regression_is_rejected(store, tmp_path):
    res = _run(store, tmp_path, _tasks(tmp_path, ["h0", "h1"]), _scorer(-0.02))
    raced = [c for c in res.candidates if c["status"] in ("accepted", "rejected_gate")]
    assert raced and raced[-1]["gate"]["lo"] > 0  # the pooled gate alone would have accepted
    assert not res.accepted and res.new_parent.spec_hash() == seed_spec().spec_hash()
    assert raced[-1]["status"] == "rejected_gate" and "held-out family H" in res.decision
    fam = raced[-1]["lofo"]["families"]["H"]
    assert fam["passed"] is False and fam["n"] == 4 and fam["mean_diff"] < 0


def test_candidate_passing_pooled_and_held_out_gates_is_accepted(store, tmp_path):
    res = _run(store, tmp_path, _tasks(tmp_path, ["h0", "h1"]), _scorer(0.5))
    assert res.accepted, res.decision
    assert res.new_parent.nodes["run"].params["max_fixes"] == 2


def test_held_out_family_with_fewer_than_two_valid_units_rejects(store, tmp_path):
    res = _run(store, tmp_path, _tasks(tmp_path, ["h0"]), _scorer(0.5), seeds=(0,))
    assert not res.accepted
    fam = [c for c in res.candidates if "lofo" in c][-1]["lofo"]["families"]["H"]
    assert fam["passed"] is False and fam["n"] == 1 and "valid paired units" in fam["reason"]


def test_ledger_records_per_family_results_and_ood_drop(store, tmp_path):
    _run(store, tmp_path, _tasks(tmp_path, ["h0", "h1"]), _scorer(-0.02))
    rec = store.records("harness_edit")[-1]
    assert rec["accepted"] is False
    fam = rec["lofo"]["families"]["H"]
    assert set(fam) >= {"n", "mean_diff", "lo", "hi", "passed"}
    assert rec["lofo"]["ood_drop"]["family:H"] == pytest.approx(fam["mean_diff"])
    assert rec["lofo"]["domains"] == {}  # one domain only: no leave-one-domain-out gate yet


def test_held_out_domain_regression_is_rejected(store, tmp_path):
    """Two domains: the held-out family passes, but the held-out domain (here: the formal tasks) regresses."""
    tasks = _tasks(tmp_path, ["h0", "h1"])
    tasks += [SuiteTask(f"f{i}", tmp_path, "gate", family="F", domain="formal") for i in range(2)]

    def score(arm, task, seed):
        if task.startswith("f"):
            return 0.0 if arm == "parent" else -0.02 - 0.001 * (int(task[1:]) + seed)
        return _scorer(0.5)(arm, task, seed)

    res = _run(store, tmp_path, tasks, score)
    assert not res.accepted and "held-out domain formal" in res.decision
    rec = store.records("harness_edit")[-1]
    assert rec["lofo"]["domains"]["formal"]["passed"] is False


# ---------------------------------------------------------------- refusing campaigns without a held-out group
def test_generation_refuses_a_suite_without_a_held_out_family(store, tmp_path):
    tasks = [SuiteTask("e0", tmp_path, "evolve", family="A"), SuiteTask("g0", tmp_path, "gate", family="A")]
    with pytest.raises(ValueError, match="held-out family"):
        _run(store, tmp_path, tasks, _scorer(0.5))


def test_generation_refuses_tasks_without_a_family(store, tmp_path):
    tasks = [SuiteTask("e0", tmp_path, "evolve"), SuiteTask("g0", tmp_path, "gate")]
    with pytest.raises(ValueError, match="no family"):
        _run(store, tmp_path, tasks, _scorer(0.5))


def test_generation_refuses_two_domains_without_a_held_out_domain(store, tmp_path):
    tasks = _tasks(tmp_path, ["h0", "h1"])
    tasks += [SuiteTask("fe", tmp_path, "evolve", family="F", domain="formal"),
              SuiteTask("fg", tmp_path, "gate", family="F2", domain="formal")]
    with pytest.raises(ValueError, match="held-out domain"):
        _run(store, tmp_path, tasks, _scorer(0.5))


def _suite_file(tmp_path: Path, rows: list[dict]) -> Path:
    entries = []
    for i, r in enumerate(rows):
        spec = rings(i + 1, 60)
        d = write_task(spec, tmp_path)
        entries.append({"id": spec.task_id, "dir": str(d), "source": spec.task_id, **r})
    p = tmp_path / "suite.yaml"
    p.write_text(yaml.safe_dump({"tasks": entries}), encoding="utf-8")
    return p


def test_load_suite_refuses_a_suite_without_a_held_out_family(tmp_path):
    p = _suite_file(tmp_path, [{"split": "evolve", "family": "rings"}, {"split": "gate", "family": "rings"},
                               {"split": "sealed", "family": "xor"}])  # sealed does not count as held out
    with pytest.raises(ValueError, match="held-out family"):
        load_suite(p)


def test_load_suite_refuses_a_task_without_family(tmp_path):
    p = _suite_file(tmp_path, [{"split": "evolve", "family": "rings"}, {"split": "gate"}])
    with pytest.raises(ValueError, match="no `family`"):
        load_suite(p)


def test_load_suite_reads_family_and_domain(tmp_path):
    p = _suite_file(tmp_path, [{"split": "evolve", "family": "rings"},
                               {"split": "gate", "family": "spirals", "domain": "ml"},
                               {"split": "sealed", "family": "xor"}])
    tasks = load_suite(p)
    assert [(t.family, t.domain) for t in tasks] == [("rings", "ml"), ("spirals", "ml"), ("xor", "ml")]


def test_load_suite_refuses_two_domains_without_a_held_out_domain(tmp_path):
    p = _suite_file(tmp_path, [{"split": "evolve", "family": "rings"}, {"split": "gate", "family": "spirals"},
                               {"split": "evolve", "family": "lean", "domain": "formal"},
                               {"split": "gate", "family": "lean2", "domain": "formal"}])
    with pytest.raises(ValueError, match="held-out domain"):
        load_suite(p)


# ---------------------------------------------------------------- the suite builder
def test_builder_default_holds_out_a_whole_family_and_stays_isolated(tmp_path):
    path = build_suite(tmp_path / "s", real_data=_fake_real_data())
    tasks = yaml.safe_load(path.read_text(encoding="utf-8"))["tasks"]
    assert all(t["domain"] == "ml" for t in tasks)
    by_family: dict[str, set[str]] = {}
    for t in tasks:
        by_family.setdefault(t["family"], set()).add(t["split"])
    assert by_family["spirals"] == {"gate"}
    assert set(Counter(t["split"] for t in tasks)) == {"evolve", "gate", "sealed"}
    assert check_split_isolation(tasks) == []
    assert len(load_suite(path)) == len(tasks) == 21


def test_assign_splits_refuses_no_held_out_or_unknown_family():
    specs = [gen(seed, 40) for gen, seed, _n in suite_mod.SYNTHETIC]
    with pytest.raises(ValueError, match="held-out famil"):
        assign_splits(specs, heldout_families=())
    with pytest.raises(ValueError, match="nope"):
        assign_splits(specs, heldout_families=("nope",))
    splits = assign_splits(specs, heldout_families=("friedman",))
    assert {splits[s.task_id] for s in specs if s.family == "friedman"} == {"gate"}
