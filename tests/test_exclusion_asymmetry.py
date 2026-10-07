"""Candidate-only infrastructure exclusions (STATUS §D: "a candidate-only infra exclusion is not counted against the
candidate"). The gate drops a unit from BOTH arms when either arm's run was an infrastructure failure, and fails
closed only above 25% exclusions. So a candidate that could make its own bad units fail as "infrastructure" (e.g. a
request-caused backend error misclassified as an outage, R2-3) could hide up to a quarter of its units.

Rule (verify/exclusion.py, exclusion accounting only; the statistics are unchanged): among units lost on exactly ONE
arm, random outages hit the candidate's and the parent's runs alike, so the candidate-only count is ~Binomial(m, 1/2)
given m one-arm losses. If P(X >= candidate-only) <= alpha (one-sided exact sign test, alpha = 0.01) the exclusions
are asymmetric beyond what random outages explain, and the comparison fails closed (the candidate is not accepted).

Wiring: the gate lives in evolve/selection.py (another agent's area); these tests apply the rule through a
monkeypatched gate exactly as the proposed production patch would, and prove both properties: the adversarial
candidate is rejected, and test_infra.py's outage campaign still reaches the clean campaign's decision."""

import json
import random

import pytest

import custom_harness.research  # noqa: F401
from custom_harness.evolve import Evaluator, EvolveConfig, evolve_generation, gate
from custom_harness.evolve import loop as evolve_loop
from custom_harness.evolve.selection import PairedSample, paired_sample
from custom_harness.gateway import MockBackend
from custom_harness.kernel import Kernel
from custom_harness.sandbox import LocalSandbox
from custom_harness.sandbox.docker import DockerUnavailable
from custom_harness.verify.exclusion import EXCLUSION_ALPHA, count_exclusions, exclusion_asymmetry, sign_test_p
from test_evolve import PROPOSALS, executor, seed_spec, suite  # noqa: F401
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


def _wire_rule(monkeypatch, log):
    """The proposed production patch, applied by monkeypatching evolve/loop.py: count exclusions per arm while
    building each paired sample, and fail the gate closed when the evolve+gate exclusions are asymmetric."""
    def counted_sample(units, candidate, parent):
        c_scores, p_scores = {u: candidate(u) for u in units}, {u: parent(u) for u in units}
        s = paired_sample(units, c_scores.__getitem__, p_scores.__getitem__)
        s.arm_counts = count_exclusions(units, c_scores.__getitem__, p_scores.__getitem__)  # type: ignore[attr-defined]
        return s

    def ruled_gate(evolve_s: PairedSample, gate_s: PairedSample, **kw):
        d = gate(evolve_s, gate_s, **kw)
        c = sum(s.arm_counts.candidate_only for s in (evolve_s, gate_s))
        p = sum(s.arm_counts.parent_only for s in (evolve_s, gate_s))
        why = exclusion_asymmetry(c, p)
        log.append((c, p, why))
        if why and d.accepted:
            d.accepted, d.reason = False, why
        return d
    monkeypatch.setattr(evolve_loop, "paired_sample", counted_sample)
    monkeypatch.setattr(evolve_loop, "gate", ruled_gate)


def test_outage_campaign_reaches_the_same_decision_with_the_rule(store, tmp_path, suite, monkeypatch):  # noqa: F811
    """test_infra.py's M0 campaign (Docker outages on both arms), rerun with the rule wired in."""
    log: list = []
    _wire_rule(monkeypatch, log)

    def campaign(sub, factory):
        from custom_harness.store import EvidenceStore
        st = EvidenceStore(tmp_path / sub)
        Kernel(st).approve_adapter("ml", approver="pytest")
        llm = MockBackend(executor)
        roles = {"proposer": llm, "executor": llm, "judge": llm, "replicator": llm}
        ev = Evaluator(roles, st, factory, suite, retry=NO_WAIT)
        res = evolve_generation(seed_spec(), suite, evaluator=ev, proposer=MockBackend([json.dumps(PROPOSALS)]),
                                store=st, cfg=EvolveConfig(n_edits=4, race_rounds=2))
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
    assert ev.infra_failures > 0 and log, "outages and the gate both happened"
    assert flaky.accepted == clean.accepted and flaky.decision == clean.decision, (log, flaky.decision)
    assert flaky.new_parent.spec_hash() == clean.new_parent.spec_hash()
