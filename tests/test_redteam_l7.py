"""Red team (s3-evolve__redteam, 2026-10-09) against L7 search over search at 8f1bf8a: the policy gate
(control/policy_gate.py + Orchestrator), the edit-type quota, and what the orchestrator's decision records carry
(GOAL §5 L7, invariant 18). A test that FAILS here is a live finding. Scripted islands, no model calls."""

from __future__ import annotations

import json

from custom_harness.control import islands as isl
from custom_harness.control import policy_gate as pg
from custom_harness.evolve import EvolveConfig, evolve_generation
from custom_harness.gateway import MockBackend
from test_evolve import seed_spec
from test_evolve_generations import TableEvaluator, _tasks
from test_policy_gate import POLICY_KW, _challenger, _orch


def _signal(iid, gen, accepted, rejected_on=("held_out_gate",)):
    return {"island": iid, "generation": gen, "accepted": accepted, "proposed": 1, "gated": 1,
            "rejected_on": [] if accepted else list(rejected_on), "edit_types": ["contract"]}


def _window(orch, y_of_position, report=lambda arm, iid: True):
    """Run the open trial's window. y_of_position(i) = islands that accept in trial round i (i = 0, 1, ...): the
    world does NOT depend on the arm (sharp null for the policy). `report(arm, iid)`: whether that island's signal
    reaches the orchestrator."""
    t = orch.trial
    for _ in range(2 * t.decl.n_pairs):
        orch.step()
        i, arm = t.index(orch.round), t.arm(orch.round)
        y = y_of_position(i)
        for j, iid in enumerate(sorted(orch.islands)):
            if report(arm, iid):
                orch.observe(_signal(iid, orch.round, j < y))
    orch.close_round()
    return orch.trial_decisions[t.decl.trial_id]


def test_attack_the_declarer_chooses_the_order_coins_and_a_period_effect_passes_a_null_challenger(store):
    # KILLED (s3-evolve fix): the Orchestrator draws each pair's coin from fresh entropy when the pair starts, so a
    # searched seed no longer fixes the schedule. Checked below as a rate: the same attack (searched seed + pure
    # period effect) repeated with the default entropy is accepted at most at the test's level.
    """ATTACK (policy gate validity): the order coins are sha256(order_seed|trial_id|k), and both inputs are chosen
    by the declarer (evolve_islands defaults order_seed to the CAMPAIGN NAME and trial_id to a fixed pattern), so the
    whole schedule is known before the first round. The sign test is valid under the sharp null only if the coins
    are independent of the outcomes. With any within-pair period effect (here: the first round of a pair has one
    more accepting island, whatever the policy; e.g. carry-over of the stall state) a declarer who searches ~100
    seeds for >= 9 of 10 'challenger first' coins gets a challenger with NO effect adopted at nominal alpha 1/20."""
    probe = pg.declare(trial_id="v1->v2@0", incumbent_version="v1", incumbent_hash="a", challenger_version="v2",
                       challenger_hash="b", round_now=0, order_seed="x")
    seed = next(s for s in (f"campaign-{i}" for i in range(5000))
                if sum(pg.order_coin(s, probe.trial_id, k) for k in range(probe.n_pairs)) >= 9)
    assert sum(pg.order_coin(seed, probe.trial_id, k) for k in range(probe.n_pairs)) >= 9  # the search worked
    n, accepted = 300, 0
    for _ in range(n):
        orch = _orch(None)
        orch.declare_trial(_challenger(), order_seed=seed, trial_id=probe.trial_id)
        dec = _window(orch, lambda i: 2 if i % 2 == 0 else 1)  # period effect only: the policy does nothing
        accepted += dec["accepted"] is True
    # under drawn coins n_plus ~ Bin(10, 1/2): P(accept) = P(n_plus >= 9) = 11/1024; the expected count over 300 runs
    # is 3.2, and P(count >= 13) < 1e-4. With the searched seed's own coins every run would be accepted.
    assert accepted <= 12, f"{accepted}/{n} no-effect challengers accepted with a searched order seed"


def test_attack_a_refused_or_abandoned_challenger_is_re_trialled_under_a_new_version_string(store):
    """ATTACK (re-declaring until one passes; peeking + early stopping): `trial_refusal` keys 'trialled before' on
    the VERSION STRING. The same parameters under a new version name (policy_hash differs only because the version
    is hashed) can be trialled again, and an open trial can be abandoned after peeking at its first pairs. alpha
    holds per trial, so k re-trials of one challenger give it about k x alpha chances."""
    orch = _orch(store)
    v2 = _challenger("v2")
    orch.declare_trial(v2, order_seed="s")
    for _ in range(4):  # peek at two pairs, then stop the bad-looking trial
        orch.step()
        for iid in sorted(orch.islands):  # every island reports (a missing island would void the trial itself)
            orch.observe(_signal(iid, orch.round, False))
        orch.close_round()
    assert orch.abandon_trial()["accepted"] is False
    same = isl.OrchestratorPolicy(**(POLICY_KW | {"stall_window": 2, "version": "v2-retry"}))
    assert {k: v for k, v in vars(same).items() if k != "version"} == \
        {k: v for k, v in vars(v2).items() if k != "version"}  # identical parameters
    why = orch.trial_refusal(same)
    assert why is not None, "the same challenger parameters may be trialled again under a new version string"


def test_attack_a_trial_round_with_missing_island_signals_counts_them_as_non_accepts(store):
    """ATTACK (a trial whose rounds are not all counted): close_round records y = accepts OBSERVED in the round; an
    island whose signal never arrives (its generation raised, or evolve_islands broke out of the round on
    BudgetExceeded and then closed the round and the trial) silently counts as 'did not accept'. World: island a
    accepts in every round under either arm (sharp null); its incumbent-round signals are lost. The challenger
    passes."""
    orch = _orch(store)
    orch.declare_trial(_challenger(), order_seed="s")
    dec = _window(orch, lambda i: 1, report=lambda arm, iid: arm == "challenger" or iid != "a")
    # KILLED (s3-evolve fix): a trial round in which an island neither reported nor was explicitly skipped voids
    # the trial instead of counting the island as a non-accept
    assert dec["complete"] is False
    assert dec["accepted"] is not True, (
        f"a trial whose incumbent rounds lost an island's signal was decided as complete and accepted: p = {dec['p']}")


PROMPT_NODES = ("hypothesize", "implement", "fix", "improve", "critic")
DISGUISES = ("params", "control_flow", "tool", "hop", "routing")


def test_attack_prompt_only_edits_mislabelled_as_other_types_pass_the_edit_type_quota(store, tmp_path):
    """ATTACK (L7 'edit-type quotas prevent collapse into prompt-only edits'): the quota counts the DECLARED
    `type`, and nothing checks that the ops match it. Five edits that only rewrite node system prompts, typed
    params / control_flow / tool / hop / routing, are all admitted under prompt_max_share 0.4 (at most 2 would be)."""
    edits = [{"type": t, "ops": [{"op": "replace", "path": f"/nodes/{node}/system",
                                  "value_json": json.dumps(f"You are careful (d{i}).")}],
              "rationale": "wording", "evidence": [], "failure_clusters_fixed": [], "expected_delta": 0.1,
              "tasks_at_risk": [], "claimed_change": [f"/nodes/{node}/system"]}
             for i, (t, node) in enumerate(zip(DISGUISES, PROMPT_NODES, strict=True))]
    tasks = _tasks(tmp_path)
    pol = isl.OrchestratorPolicy(version="p1", prompt_max_share=0.4)
    res = evolve_generation(seed_spec(), tasks, evaluator=TableEvaluator({}, store, None, tasks),
                            proposer=MockBackend([json.dumps({"edits": edits})]), store=store,
                            cfg=EvolveConfig(seeds=(0, 1), n_edits=5, race_rounds=1),
                            edit_admission=lambda es: isl.admit([e.type.value for e in es], pol))
    quota = [c for c in res.candidates if "edit-type quota" in str(c.get("reason", ""))]
    prompt_only_admitted = {c["edit"]["ops"][0]["path"] for c in res.candidates
                            if c not in quota and isinstance(c.get("edit"), dict) and c["edit"].get("ops")
                            and all(str(o.get("path", "")).endswith("/system") for o in c["edit"]["ops"])}
    assert len(prompt_only_admitted) <= 2, (
        f"{len(prompt_only_admitted)} prompt-only edits passed the prompt quota under other type labels: "
        f"{sorted(prompt_only_admitted)}")


def test_attack_orchestrator_decision_records_carry_the_unreleased_gate_stage(store):
    """ATTACK (invariant 18; root cause in the s3-evolve red-team test
    test_redteam_route_reveal.py::test_attack_the_proposer_visible_rejecting_stage_carries_a_gate_bit_no_release_counts):
    'cost' vs 'held_out_gate' tells whether the held-out gate and every LOFO group passed, while only the accept bit
    is released. The orchestrator copies the per-stage counts into every `orchestrator_decision` record's evidence."""
    # KILLED at the root (s3-evolve fix, loop.py GATE_STAGES): the stage the orchestrator copies is itself a
    # ledgered `count` release of the generation (test_redteam_route_reveal.py::
    # test_attack_the_proposer_visible_rejecting_stage_carries_a_gate_bit_no_release_counts), so the evidence below
    # differs only by released values. Checked: every stage a signal may carry is a releasable stage code.
    from custom_harness.evolve.loop import GATE_STAGES
    assert set(isl.REJECT_STAGES) <= {s for s in GATE_STAGES if s is not None}
    out = []
    for stage in ("cost", "held_out_gate"):
        orch = isl.Orchestrator([isl.Island("a", {"p": 1.0}, {"contract": 1.0}),
                                 isl.Island("b", {"p": 1.0}, {"hop": 1.0})],
                                isl.OrchestratorPolicy(version="v1", **POLICY_KW), total_budget=10)
        for g in range(3):
            orch.step()
            for iid in ("a", "b"):
                orch.observe(_signal(iid, g, False, rejected_on=(stage,)))
        out.append([d["evidence"]["rejected_on"] for d in orch.step()])
    assert out[0] != out[1]  # the evidence carries the (now released) stage counts
