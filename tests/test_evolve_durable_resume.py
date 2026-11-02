"""Durable evolve resume (evolve/durable.py, Evaluator(durable_key=...), driver `campaign=`): a campaign killed in the
middle of a generation resumes without re-running or re-charging a finished unit, without calling the proposer again
for the already-proposed generation, and reaches the same decisions as an uninterrupted campaign.

World: the real driver, the real evolve_generation and the real Evaluator (real engine runs of a one-node module
harness from tests/test_evalrace.py whose score is a deterministic function of (effect, task, seed) and which spends
tokens through a mock backend). The proposer proposes one edit (effect 0.0 -> 0.3). The multiplicity check is
replaced by a no-op (the one-node spec has no claim node to declare it). The "kill" is an exception raised in place of
the N-th harness run, before it starts (so no real run is lost or half-charged).
"""

from __future__ import annotations

import json

import pytest

import test_evalrace as W
from custom_harness.budget import persistent_budget
from custom_harness.evolve import EvolveConfig, SuiteTask
from custom_harness.evolve.loop import Evaluator
from custom_harness.gateway import MockBackend
from custom_harness.sandbox import LocalSandbox

EDIT = json.dumps({"edits": [{
    "type": "params", "ops": [{"op": "replace", "path": "/nodes/a/params/effect", "value_json": "0.3"}],
    "rationale": "a larger effect", "evidence": [], "failure_clusters_fixed": [], "expected_delta": 0.3,
    "tasks_at_risk": [], "claimed_change": ["/nodes/a/params/effect"]}]})
CFG = dict(seeds=(0, 1), n_edits=1, race_rounds=1)


class Killed(BaseException):
    """The process dies (not an Exception: nothing in the loop may catch it)."""


def _suite(root):
    out = []
    for name, split, fam in (("e0", "evolve", "A"), ("e1", "evolve", "A"), ("g0", "gate", "A"), ("g1", "gate", "A"),
                             ("h0", "gate", "H"), ("h1", "gate", "H")):
        (root / name).mkdir(parents=True, exist_ok=True)
        out.append(SuiteTask(name, root / name, split, family=fam))
    return out


def _campaign(store, root, proposer, *, kill_at: int | None, monkeypatch):
    """One invocation of the driver on campaign "c" (a fresh process: new Evaluator, budget re-loaded)."""
    from custom_harness.evolve import driver
    monkeypatch.setattr(driver, "check_campaign", lambda spec, store, dirs, runs_per_task: 0)
    real = Evaluator.attempt
    n = {"attempts": 0}

    def attempt(self, att):
        n["attempts"] += 1
        if kill_at is not None and n["attempts"] == kill_at:
            raise Killed(f"process killed before harness run #{kill_at}")
        return real(self, att)

    monkeypatch.setattr(Evaluator, "attempt", attempt)
    budget = persistent_budget(store, "c", {"tokens": None})
    factory = lambda key: LocalSandbox(root / "sb" / key, i_understand_no_isolation=True)  # noqa: E731
    try:
        driver.evolve(W.spec(0.0), _suite(root / "tasks"), generations=1,
                      backends={"executor": MockBackend(lambda req: "ok fine")}, proposer=proposer, store=store,
                      sandbox_factory=factory, cfg=EvolveConfig(**CFG), campaign_budget=budget, campaign="c",
                      log=lambda s: None)
    finally:
        monkeypatch.setattr(Evaluator, "attempt", real)
    return budget


def _decisions(store):
    lineage = [{k: r.get(k) for k in ("status", "candidate", "survived", "rejected_on", "race", "evolve_fresh",
                                      "gate", "lofo", "cost", "exclusions")} for r in store.records("lineage")]
    edits = [{k: r.get(k) for k in ("candidate", "accepted", "decision", "actual_evolve_delta", "actual_gate_delta",
                                    "per_task", "rejected_on")} for r in store.records("harness_edit")]
    led = [{k: r[k] for k in ("generation", "parent", "new_parent", "signal", "budget")}
           for r in store.records("generation_ledger")]
    return lineage, edits, led


def _spent(budget):
    return budget.spent.prompt_tokens + budget.spent.completion_tokens


def test_a_campaign_killed_mid_generation_resumes_with_no_double_charge_and_identical_decisions(
        tmp_path, monkeypatch):
    from custom_harness.kernel import Kernel
    from custom_harness.store import EvidenceStore

    def fresh_store(name):
        s = EvidenceStore(tmp_path / name / "store")
        Kernel(s).approve_adapter("ml", approver="pytest")
        return s

    # control: the same campaign, never interrupted
    control = fresh_store("control")
    W.RUNS.clear()
    p0 = MockBackend([EDIT])
    b0 = _campaign(control, tmp_path / "control", p0, kill_at=None, monkeypatch=monkeypatch)
    control_runs, control_spent = len(W.RUNS), _spent(b0)
    control_decisions = _decisions(control)
    assert len(p0.requests) == 1 and control_runs > 8 and control_spent > 0
    assert control_decisions[2] and control_decisions[2][0]["signal"]["gated"] == 1  # the candidate reached the gate

    # killed: the process dies before harness run #8 (after the parent's units, the smoke run and part of the race)
    store = fresh_store("killed")
    W.RUNS.clear()
    proposer = MockBackend([EDIT])
    with pytest.raises(Killed):
        _campaign(store, tmp_path / "killed", proposer, kill_at=8, monkeypatch=monkeypatch)
    runs_before_kill = len(W.RUNS)
    assert runs_before_kill == 7 and len(proposer.requests) == 1
    assert store.records("generation_ledger") == [] and len(store.records("evolve_proposal")) == 1
    assert len(store.records("evolve_unit")) == 7  # every finished unit is durable

    # resumed in a new "process": same store, same campaign
    b2 = _campaign(store, tmp_path / "killed", proposer, kill_at=None, monkeypatch=monkeypatch)
    assert len(proposer.requests) == 1  # the proposer was NOT asked again for the proposed generation
    assert len(W.RUNS) == control_runs  # no finished unit ran twice (7 before the kill + the rest after)
    assert _spent(b2) == control_spent  # and none was charged twice
    assert _decisions(store) == control_decisions  # identical race, gate and ledger decisions
    assert len(store.records("evolve_proposal")) == 1


def test_a_persisted_proposal_for_another_parent_is_refused(tmp_path, store, monkeypatch):
    from custom_harness.evolve.durable import ProposalLog
    from custom_harness.evolve.generations import LedgerError
    log = ProposalLog(store, "c", 0)
    assert log.load("a" * 64) is None
    from custom_harness.evolve.propose import Proposal
    log.save("a" * 64, Proposal([], ["bad edit"], None, "nothing to do"), {"r1"}, {"s1"})
    replayed, known, rejected = log.load("a" * 64)
    assert replayed.edits == [] and replayed.problems == ["bad edit"] and replayed.decline == "nothing to do"
    assert known == {"r1"} and rejected == {"s1"}
    with pytest.raises(LedgerError, match="parent"):
        log.load("b" * 64)
    with pytest.raises(LedgerError, match="already"):
        log.save("a" * 64, Proposal([], [], None, None), set(), set())
