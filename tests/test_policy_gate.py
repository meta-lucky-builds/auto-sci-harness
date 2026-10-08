"""GOAL §5 L7: the orchestrator policy changes only on measured gain (control/policy_gate.py). A declared trial of W
AB/BA round pairs with seeded order coins; round outcome = number of islands that accepted (accept bits only,
invariant 18); exact one-sided sign test on the pair differences; `Orchestrator.set_policy` refuses any change
without the passing decision this orchestrator recorded for exactly that challenger. Scripted islands, no model
calls. The modules under test are imported inside each test so that, without them, each test fails on its own."""

from __future__ import annotations

import hashlib
import importlib
import math
import random
from fractions import Fraction

import pytest

POLICY_KW = dict(stall_window=3, more_eval_share=0.5, stall_shrink=0.5, min_island_share=0.1, prompt_max_share=0.4,
                 min_edit_types=2, cadence=3)


def PG():
    return importlib.import_module("custom_harness.control.policy_gate")


def ISL():
    return importlib.import_module("custom_harness.control.islands")


def _orch(store=None):
    isl = ISL()
    islands = [isl.Island("a", {"codex": 1.0}, {"contract": 1.0}), isl.Island("b", {"local": 1.0}, {"hop": 1.0}),
               isl.Island("c", {"codex": 1.0}, {"tool": 1.0})]
    return isl.Orchestrator(islands, isl.OrchestratorPolicy(version="v1", **POLICY_KW), total_budget=30,
                            store=store)


def _challenger(version="v2", **kw):
    return ISL().OrchestratorPolicy(version=version, **(POLICY_KW | {"stall_window": 2} | kw))


def _run_trial(orch, y_challenger, y_incumbent):
    """Run the open trial's window: y_* = how many islands accept in a round of that arm (callables of the pair
    index, or ints)."""
    t = orch.trial
    for _ in range(2 * t.decl.n_pairs):
        orch.step()
        arm = t.arm(orch.round)
        k = t.index(orch.round) // 2
        y = y_challenger if arm == "challenger" else y_incumbent
        y = y(k) if callable(y) else y
        for j, iid in enumerate(sorted(orch.islands)):
            acc = j < y
            orch.observe({"island": iid, "generation": orch.round, "accepted": acc, "proposed": 1, "gated": 1,
                          "rejected_on": [] if acc else ["held_out_gate"], "edit_types": ["contract"]})
    orch.close_round()
    return orch.trial_decisions[t.decl.trial_id]


def test_exact_sign_test_values():
    pg = PG()
    assert pg.sign_p_value(5, 5) == Fraction(1, 32)
    assert pg.sign_p_value(9, 10) == Fraction(11, 1024)
    assert pg.sign_p_value(0, 0) == 1 and pg.sign_p_value(0, 7) == 1
    assert pg.exact_alpha(0.05) == Fraction(1, 20)  # the float's decimal repr, exactly
    for bad in (True, float("nan"), 1.0, 0, -0.1, "x"):
        with pytest.raises(pg.PolicyGateError):
            pg.exact_alpha(bad)
    r = pg.sign_test([1, 2, 1, 1, 3, 0, 0], alpha=0.05, min_nonzero=5)
    assert (r["n_plus"], r["n_minus"], r["n_zero"], r["p"], r["accepted"]) == (5, 0, 2, "1/32", True)
    r = pg.sign_test([1, 1, 1, 1, -1, 0], alpha=0.05, min_nonzero=5)
    assert (r["p"], r["accepted"]) == ("3/16", False)  # P(Bin(5,1/2) >= 4) = 6/32
    with pytest.raises(pg.PolicyGateError):
        pg.sign_test([0.5], alpha=0.05, min_nonzero=1)  # a continuous value never enters the test


def test_a_truly_better_challenger_is_accepted_and_adopted(store):
    orch = _orch(store)
    ch = _challenger()
    decl = orch.declare_trial(ch, order_seed="seed-1")  # config defaults: W=10, alpha=0.05, min_nonzero=5
    assert (decl.n_pairs, decl.alpha, decl.min_nonzero) == (10, "1/20", 5)
    dec = _run_trial(orch, 2, 0)
    assert (dec["n_plus"], dec["n_minus"], dec["n_zero"], dec["p"], dec["accepted"]) == (10, 0, 0, "1/1024", True)
    orch.set_policy(ch, trial_decision=dec)
    assert orch.policy is ch and orch.step()[0]["policy_version"] == "v2"
    kinds = {k: store.records(k) for k in ("policy_trial", "policy_trial_round", "policy_trial_decision")}
    assert len(kinds["policy_trial"]) == 1 and len(kinds["policy_trial_round"]) == 20
    assert kinds["policy_trial_decision"] == [dec]
    assert store.records("orchestrator_policy")[-1]["trial_id"] == decl.trial_id
    # every island of a trial round ran under the same arm's policy (one policy per round)
    for r in kinds["policy_trial_round"]:
        decs = [d for d in store.records("orchestrator_decision") if d["round"] == r["round"]]
        assert len(decs) == 3 and {d["policy_version"] for d in decs} == {"v2" if r["arm"] == "challenger"
                                                                          else "v1"}


@pytest.mark.parametrize("y_ch, y_inc", [(1, 1), (0, 1), (lambda k: k % 2, lambda k: 1 - k % 2)])
def test_an_identical_or_worse_challenger_is_refused(store, y_ch, y_inc):
    orch = _orch(store)
    ch = _challenger()
    orch.declare_trial(ch, order_seed="seed-2")
    dec = _run_trial(orch, y_ch, y_inc)
    assert dec["accepted"] is False
    with pytest.raises(ISL().IslandError, match="does not accept"):
        orch.set_policy(ch, trial_decision=dec)
    assert orch.policy.version == "v1"


def test_too_few_non_zero_pairs_are_refused_even_at_a_small_p():
    orch = _orch()
    orch.declare_trial(_challenger(), order_seed="s", n_pairs=10, alpha=0.1, min_nonzero=5)
    dec = _run_trial(orch, lambda k: 1 if k < 4 else 0, 0)  # 4 positive pairs, 6 ties
    assert (dec["n_plus"], dec["n_zero"], dec["p"]) == (4, 6, "1/16")  # 1/16 <= alpha 1/10 ...
    assert dec["accepted"] is False and "too few non-zero pairs" in dec["reason"]  # ... but 4 < min_nonzero


def test_the_order_coins_are_reproduced_from_the_declared_seed(store):
    pg = PG()
    orch = _orch(store)
    decl = orch.declare_trial(_challenger(), order_seed="declared-seed", trial_id="T1", n_pairs=6)
    coins = [int(hashlib.sha256(f"declared-seed|T1|{k}".encode()).hexdigest(), 16) & 1 for k in range(6)]
    assert decl.coins() == coins and store.records("policy_trial")[0]["coins"] == coins
    assert 0 < sum(coins) < 6  # both orders occur (a crossover, not a fixed AB order)
    assert decl.schedule() == [a for c in coins for a in (("incumbent", "challenger") if c == 0
                                                          else ("challenger", "incumbent"))]
    _run_trial(orch, 1, 0)
    used = [r["arm"] for r in store.records("policy_trial_round")]
    assert used == decl.schedule()
    assert pg.declare(trial_id="T1", incumbent_version="v1", incumbent_hash="h1", challenger_version="v2",
                      challenger_hash="h2", round_now=0, order_seed="declared-seed", n_pairs=6).coins() == coins


def test_a_change_without_a_trial_or_too_early_is_refused():
    isl = ISL()
    orch = _orch()
    for _ in range(3):
        orch.step()
    with pytest.raises(isl.IslandError, match="passing policy_trial_decision"):
        orch.set_policy(_challenger())  # cadence met, no trial
    with pytest.raises(isl.IslandError, match="can never pass"):  # 4 pairs cannot reach p <= 1/20
        orch.declare_trial(_challenger(), order_seed="s", n_pairs=4, min_nonzero=4)
    ch = _challenger()
    orch.declare_trial(ch, order_seed="s", n_pairs=5)
    with pytest.raises(isl.IslandError, match="still open"):
        orch.declare_trial(_challenger("v3"), order_seed="s")
    orch.set_policy(ch, trial_decision=_run_trial(orch, 1, 0))
    # after a change, a new trial may start only `cadence` rounds later
    with pytest.raises(isl.IslandError, match="rounds after the last change"):
        orch.declare_trial(_challenger("v3", stall_window=4), order_seed="s")
    with pytest.raises(isl.IslandError, match="used before"):
        orch.declare_trial(_challenger("v2", stall_window=5), order_seed="s")
    for _ in range(3):
        orch.step()
    orch.declare_trial(_challenger("v3", stall_window=4), order_seed="s")  # positive control: cadence met


def test_a_trialled_and_refused_challenger_cannot_be_trialled_again():
    orch = _orch()
    ch = _challenger()
    orch.declare_trial(ch, order_seed="s", n_pairs=5)
    assert _run_trial(orch, 0, 0)["accepted"] is False
    with pytest.raises(ISL().IslandError, match="used before"):  # no retry until a lucky pass
        orch.declare_trial(ch, order_seed="other-seed", n_pairs=5)


def test_a_forged_decision_is_refused():
    isl = ISL()
    orch = _orch()
    ch = _challenger()
    decl = orch.declare_trial(ch, order_seed="s", n_pairs=5)
    dec = _run_trial(orch, 1, 0)
    assert dec["accepted"] is True
    same_version_other_params = _challenger(stall_window=7)
    assert same_version_other_params.version == ch.version
    with pytest.raises(isl.IslandError, match="another challenger"):
        orch.set_policy(same_version_other_params, trial_decision=dec)
    with pytest.raises(isl.IslandError, match="another challenger"):
        orch.set_policy(_challenger("v9"), trial_decision=dec)
    forged = dict(dec) | {"challenger_version": "v9", "challenger_hash": _challenger("v9").policy_hash()}
    with pytest.raises(isl.IslandError, match="not one this orchestrator"):
        orch.set_policy(_challenger("v9"), trial_decision=forged)
    with pytest.raises(isl.IslandError, match="not one this orchestrator"):
        orch.set_policy(ch, trial_decision=dict(dec) | {"n_plus": 9})
    refused = _orch()
    rc = _challenger()
    refused.declare_trial(rc, order_seed="s", n_pairs=5)
    bad = _run_trial(refused, 0, 1)
    with pytest.raises(isl.IslandError):
        refused.set_policy(rc, trial_decision=bad | {"accepted": True, "p": "1/32"})
    assert PG().verify_decision(bad | {"accepted": True}, refused._trial_decls[bad["trial_id"]]) != []
    orch.set_policy(ch, trial_decision=dec)  # positive control: the genuine decision
    assert orch.policy is ch and decl.trial_id in orch.trial_decisions


def test_an_unfinished_window_is_never_accepted(store):
    orch = _orch(store)
    orch.declare_trial(_challenger(), order_seed="s", n_pairs=5)
    for _ in range(3):
        orch.step()
    dec = orch.abandon_trial()
    assert dec["complete"] is False and dec["accepted"] is False and "not completed" in dec["reason"]
    assert store.records("policy_trial_decision") == [dec] and orch.trial is None


def test_under_the_null_the_false_acceptance_rate_is_at_most_alpha():
    """Identical policies: every round's outcome is drawn from the same distribution whatever the arm. Over N
    simulated trials (seeded) the share accepted by the exact test is <= alpha + 3 binomial standard errors."""
    pg = PG()
    rng = random.Random(20261009)
    n_trials, alpha = 4000, 0.05
    accepted = 0
    for j in range(n_trials):
        decl = pg.declare(trial_id=f"null-{j}", incumbent_version="v1", incumbent_hash="h1", challenger_version="v2",
                          challenger_hash="h2", round_now=0, order_seed="null-sim")
        outcomes = [sum(rng.random() < 0.3 for _ in range(3)) for _ in range(2 * decl.n_pairs)]
        accepted += pg.decision(decl, outcomes)["accepted"]
    rate = accepted / n_trials
    tol = 3 * math.sqrt(alpha * (1 - alpha) / n_trials)
    assert rate <= alpha + tol, (rate, alpha + tol)
