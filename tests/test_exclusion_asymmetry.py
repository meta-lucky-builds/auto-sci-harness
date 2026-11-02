"""Candidate-only infrastructure exclusions (STATUS §D: "a candidate-only infra exclusion is not counted against the
candidate"). The gate drops a unit from BOTH arms when either arm's run was an infrastructure failure, and fails
closed only above 25% exclusions. So a candidate that could make its own bad units fail as "infrastructure" (e.g. a
request-caused backend error misclassified as an outage, R2-3) could hide up to a quarter of its units.

Rule (verify/exclusion.py, exclusion accounting only; the statistics are unchanged): among units lost on exactly ONE
arm, random outages hit the candidate's and the parent's runs alike, so the candidate-only count is ~Binomial(m, 1/2)
given m one-arm losses. If P(X >= candidate-only) <= alpha (one-sided exact sign test, alpha = 0.01) the exclusions
are asymmetric beyond what random outages explain, and the comparison fails closed (the candidate is not accepted).

Wiring (2026-10-07): paired_sample counts exclusions per arm, evolve/loop.py applies `exclusion_check` to the fresh
evolve re-measurement + gate split and fails the candidate closed, recording `exclusions` and
`rejected_on="exclusion_asymmetry"` in the lineage and the harness_edit ledger. These tests go through the
production loop and prove both properties: the adversarial candidate is rejected (it was accepted before), and
test_infra.py's outage campaign still reaches the clean campaign's decision."""

import json
import random

import pytest

import custom_harness.research  # noqa: F401
from custom_harness.evolve import Evaluator, EvolveConfig, evolve_generation, gate
from custom_harness.evolve.multiplicity import MultiplicityDrift
from custom_harness.evolve.selection import paired_sample
from custom_harness.gateway import MockBackend
from custom_harness.kernel import Kernel
from custom_harness.sandbox import LocalSandbox
from custom_harness.sandbox.docker import DockerUnavailable
from custom_harness.verify.exclusion import EXCLUSION_ALPHA, count_exclusions, exclusion_asymmetry, sign_test_p
from test_evolve import (  # noqa: F401
    PROPOSALS,
    R2_2_OPEN,
    declared_seed_spec,
    executor,
    seed_spec,
    suite,
)
from test_infra import NO_WAIT, FlakyDockerLike


def test_sign_test_thresholds():
    assert sign_test_p(0, 0) == 1.0 and sign_test_p(3, 3) == pytest.approx(0.65625)
    assert exclusion_asymmetry(7, 0) is not None  # p = 1/128
    assert exclusion_asymmetry(6, 0) is None  # p = 1/64 > 0.01: a few one-sided losses are normal
    assert exclusion_asymmetry(9, 4) is None and exclusion_asymmetry(12, 1) is not None


def test_random_outages_rarely_trigger_the_rule():
    """Outages independent of the arm (each run fails w.p. 0.12, 40 units): false trigger rate <= alpha."""
    rng = random.Random(7)
    trials, hits = 4000, 0
    for _ in range(trials):
        c_only = p_only = 0
        for _ in range(40):
            c, p = rng.random() < 0.12, rng.random() < 0.12
            c_only += c and not p
            p_only += p and not c
        hits += exclusion_asymmetry(c_only, p_only) is not None
    assert hits / trials <= EXCLUSION_ALPHA, hits / trials


def _selective_candidate():
    """32 units; the candidate is worse than the parent on 8 of them and turns exactly those into 'infrastructure
    failures' (25% excluded: under the gate's cap). On the 24 it keeps, it is better."""
    units = [("t", i) for i in range(32)]
    parent = {u: 0.5 for u in units}
    cand = {u: (None if u[1] % 4 == 0 else 0.6 + 0.001 * u[1]) for u in units}
    return units, cand, parent


def test_selective_candidate_exclusions_pass_the_current_gate_but_not_the_rule():
    units, cand, parent = _selective_candidate()
    ev, gt = units[:16], units[16:]
    current = gate(paired_sample(ev, cand.get, parent.get), paired_sample(gt, cand.get, parent.get))
    assert current.accepted  # the gap: the bad units vanished from both arms
    counts = count_exclusions(units, cand.get, parent.get)
    assert counts.candidate_only == 8 and counts.parent_only == 0
    assert exclusion_asymmetry(counts.candidate_only, counts.parent_only) is not None


@pytest.mark.xfail(strict=True, raises=MultiplicityDrift, reason=R2_2_OPEN)
def test_outage_campaign_reaches_the_same_decision_with_the_rule(store, tmp_path, suite, monkeypatch):  # noqa: F811
    """test_infra.py's M0 campaign (Docker outages on both arms), run through the production loop with the rule wired
    in (evolve/loop.py). Until 2026-10-07 this test monkeypatched the rule in; that version also predates R2-2 (it
    used an undeclared seed spec, which the Evaluator now stops with MultiplicityDrift)."""
    monkeypatch.setenv("CHH_ALLOW_UNISOLATED", "1")
    stores: dict = {}

    def campaign(sub, factory):
        from custom_harness.store import EvidenceStore
        st = EvidenceStore(tmp_path / sub)
        Kernel(st).approve_adapter("ml", approver="pytest")
        llm = MockBackend(executor)
        roles = {"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}
        ev = Evaluator(roles, st, factory, suite, retry=NO_WAIT)
        cfg = EvolveConfig(n_edits=4, race_rounds=2)
        res = evolve_generation(declared_seed_spec(cfg), suite, evaluator=ev,
                                proposer=MockBackend([json.dumps(PROPOSALS)]), store=st, cfg=cfg)
        stores[sub] = [r["exclusions"] for r in st.records("harness_edit")]
        st.close()
        return res, ev

    clean, _ = campaign("clean", lambda key: LocalSandbox(tmp_path / "sb1" / key, i_understand_no_isolation=True))
    created = {"n": 0}

    def outage_factory(key):
        created["n"] += 1
        if created["n"] % 4 == 0:
            raise DockerUnavailable("Docker is not running")
        return FlakyDockerLike(tmp_path / "sb2" / f"{key}-{created['n']}", i_understand_no_isolation=True)

    FlakyDockerLike.count = 0
    flaky, ev = campaign("flaky", outage_factory)
    assert ev.infra_failures > 0 and stores["flaky"], "outages and the gate both happened"
    assert all(e["passed"] for e in stores["clean"] + stores["flaky"]), stores
    assert flaky.accepted == clean.accepted and flaky.decision == clean.decision, (stores, flaky.decision)
    assert flaky.new_parent.spec_hash() == clean.new_parent.spec_hash()


# ------------------------------------------------------------------ the rule wired into the REAL evolution loop
class ScriptedEvaluator:
    """The Evaluator interface evolve_generation uses (outcome / score / calls / cache), with scripted RunOutcomes
    instead of research runs, so a campaign can have enough units for the rule to have power. Everything else is
    the production loop: proposal parsing, static checks, race, fresh re-measurement, gate, LOFO, cost, ledger.

    Parent: 0.5 (+ a small per-seed spread) on every unit. Candidate: 0.1 better on the units it keeps; on the units
    where `cand_hidden(seed)` it reports INFRA_FAILED (score None), i.e. it turns those units into "infrastructure
    failures". `parent_down(seed)` makes the parent's run of a unit an infrastructure failure (an outage)."""

    def __init__(self, parent_hash, cand_hidden, parent_down=lambda s: False):
        self.parent_hash, self.cand_hidden, self.parent_down = parent_hash, cand_hidden, parent_down
        self.cache: dict = {}
        self.calls = 0
        self.infra_failures = 0

    def outcome(self, spec, unit):
        from custom_harness.evolve.evaluate import RunOutcome
        from custom_harness.records import Cost, RunStatus
        key = (spec.spec_hash(), unit[0], unit[1])
        if key not in self.cache:
            self.calls += 1
            task, seed = unit
            is_parent = key[0] == self.parent_hash
            down = self.parent_down(seed) if is_parent else self.cand_hidden(seed)
            score = None if down else 0.5 + 0.01 * (seed % 3) + (0.0 if is_parent else 0.1 + 0.001 * (seed % 5))
            status = RunStatus.INFRA_FAILED.value if down else RunStatus.COMPLETED.value
            self.infra_failures += down
            self.cache[key] = RunOutcome(task, f"run_{len(self.cache)}", seed, score, False, status,
                                         Cost(prompt_tokens=100, completion_tokens=10))
        return self.cache[key]

    def score(self, spec, unit):
        return self.outcome(spec, unit).score


def _hiding_campaign(store, tmp_path, cand_hidden, parent_down=lambda s: False):
    from custom_harness.evolve import SuiteTask
    tasks = [SuiteTask("e0", tmp_path, "evolve", family="A"), SuiteTask("e1", tmp_path, "evolve", family="A"),
             SuiteTask("g0", tmp_path, "gate", family="H"), SuiteTask("g1", tmp_path, "gate", family="H")]
    parent = seed_spec()
    ev = ScriptedEvaluator(parent.spec_hash(), cand_hidden, parent_down)
    proposal = {"edits": [PROPOSALS["edits"][0]]}  # a valid edit (max_fixes 0 -> 2)
    res = evolve_generation(parent, tasks, evaluator=ev, proposer=MockBackend([json.dumps(proposal)]), store=store,
                            cfg=EvolveConfig(seeds=tuple(range(8)), n_edits=1, race_rounds=2))
    return res, ev


def test_real_loop_rejects_a_candidate_that_hides_its_bad_units_as_infra_failures(store, tmp_path):
    """16 fresh evolve units + 16 gate units; the candidate turns seeds = 0 mod 4 (2 per task: 4/16 = 25% per split,
    within the gate's exclusion cap) into infrastructure failures. Before the rule this candidate was ACCEPTED."""
    res, ev = _hiding_campaign(store, tmp_path, cand_hidden=lambda s: s % 4 == 0)
    assert ev.infra_failures > 0
    gated = [c for c in res.candidates if c["status"] in ("accepted", "rejected_gate")]
    assert len(gated) == 1 and gated[0]["pooled_gate_accepted"] is True, gated  # the statistics alone accept it
    assert not res.accepted, res.decision
    assert "exclusions asymmetric" in res.decision and res.new_parent.spec_hash() == seed_spec().spec_hash()
    excl = gated[0]["exclusions"]
    assert (excl["candidate_only"], excl["parent_only"], excl["passed"]) == (8, 0, False), excl
    assert gated[0]["rejected_on"] == "exclusion_asymmetry"
    ledger = store.records("harness_edit")[-1]
    assert ledger["accepted"] is False and ledger["decision"] == res.decision
    assert ledger["exclusions"] == excl and ledger["rejected_on"] == "exclusion_asymmetry"
    # the proposer sees the stage only, never the per-arm counts (they include gate units)
    from custom_harness.evolve.export import proposer_view
    view = proposer_view(ledger, {"e0", "e1"})
    assert view["rejected_on"] == "exclusion_asymmetry" and "exclusions" not in view
    lineage = [r for r in store.records("lineage") if r.get("status") == "rejected_gate"]
    assert lineage and lineage[-1]["exclusions"] == excl


def test_real_loop_still_accepts_with_symmetric_outages(store, tmp_path):
    """Positive control: outages on BOTH arms (candidate seeds = 0 mod 8, parent seeds = 4 mod 8: 4 vs 4 one-arm
    losses) are what random outages look like; the candidate is still accepted and the counts are recorded."""
    res, _ = _hiding_campaign(store, tmp_path, cand_hidden=lambda s: s % 8 == 0, parent_down=lambda s: s % 8 == 4)
    assert res.accepted, res.decision
    excl = next(c for c in res.candidates if c["status"] == "accepted")["exclusions"]
    assert (excl["candidate_only"], excl["parent_only"], excl["passed"]) == (4, 4, True), excl
