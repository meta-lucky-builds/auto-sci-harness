"""GOAL §5 L7 wired: `evolve.search.evolve_islands` runs islands through the REAL generation loop
(`evolve_generation`) under the orchestrator of control/islands.py: per-island lineages and generation ledgers,
sequential islands, the edit-type quota enforced in the loop, the weights lever routed to the real L8 export (nothing
trained), a downgrade-only controller on budgets, the worst-case multiplicity bound, and a policy trial.

Scores come from the table evaluator of test_evolve_generations (no harness, no model); proposers and the
controller are gateway MockBackends. The module under test is imported inside each test (via the fixture) so that,
without it, each test fails on its own."""

from __future__ import annotations

import importlib
import json

import pytest

from custom_harness.evolve import Edit, EvolveConfig, SuiteTask, evolve_generation
from custom_harness.gateway import MockBackend
from test_evolve import seed_spec
from test_evolve_generations import CFG, DECLINE, THREE_EDITS, TableEvaluator, _tasks
from test_weights_export import OPEN, PAIR, SET_A, SET_B, traj, verdicts_for

PROMPT_NODES = ("hypothesize", "implement", "fix", "improve", "critic")


def _prompt_only(n=5, tag="v"):
    """n distinct, honest prompt_text edits (one per node system prompt): a proposer collapsed into prompt edits."""
    edits = [{"type": "prompt_text", "ops": [{"op": "replace", "path": f"/nodes/{node}/system",
                                               "value_json": json.dumps(f"You are careful ({tag}{i}).")}],
              "rationale": "wording", "evidence": [], "failure_clusters_fixed": [], "expected_delta": 0.1,
              "tasks_at_risk": [], "claimed_change": [f"/nodes/{node}/system"]}
             for i, node in enumerate(PROMPT_NODES[:n])]
    return json.dumps({"edits": edits})


def _islands_mod():
    return importlib.import_module("custom_harness.control.islands")


def _policy(**kw):
    return _islands_mod().OrchestratorPolicy(**({"version": "p1", "prompt_max_share": 0.4} | kw))


def _islands():
    isl = _islands_mod()
    return [isl.Island("a", {"p1": 1.0}, {"control_flow": 1.0}),
            isl.Island("b", {"p1": 1.0, "p2": 1.0}, {"prompt_text": 0.5, "hop": 0.5})]


@pytest.fixture
def search(monkeypatch, store, tmp_path):
    S = importlib.import_module("custom_harness.evolve.search")
    seen: dict[str, list] = {"runs_per_task": [], "dirs": [], "gen": []}

    def check(spec, store_, dirs, runs_per_task):
        seen["runs_per_task"].append(runs_per_task)
        seen["dirs"].append(list(dirs))
        return 0

    real_gen = S.evolve_generation

    def gen_spy(*a, **kw):
        seen["gen"].append({"n_edits": kw["cfg"].n_edits, "seeds": kw["cfg"].seeds, "proposer": kw["proposer"],
                            "edit_admission": kw.get("edit_admission")})
        return real_gen(*a, **kw)

    monkeypatch.setattr(S, "Evaluator", TableEvaluator)
    monkeypatch.setattr(S, "check_campaign", check)
    monkeypatch.setattr(S, "evolve_generation", gen_spy)
    sealed = [SuiteTask("s0", tmp_path, "sealed", family="A")]  # TableEvaluator refuses a sealed task: filtered

    def run(rounds, *, proposers=None, islands=None, policy=None, cfg=None, total_budget=12, **kw):
        return S.evolve_islands(
            seed_spec(), _tasks(tmp_path) + sealed, islands=islands or _islands(), policy=policy or _policy(),
            rounds=rounds, total_budget=total_budget,
            proposers=proposers or {"p1": MockBackend([THREE_EDITS]), "p2": MockBackend([THREE_EDITS])},
            backends={}, store=store, sandbox_factory=lambda k: None, cfg=EvolveConfig(**(cfg or CFG)),
            campaign="L7", log=lambda s: None, **kw)
    run.seen = seen  # type: ignore[attr-defined]
    run.S = S  # type: ignore[attr-defined]
    return run


# ==================================================================================== edit-type quota, in the loop
def test_edit_type_quota_is_enforced_in_the_real_loop(store, tmp_path):
    """A proposer that proposes only prompt_text edits: at most floor(prompt_max_share * n) = floor(0.4 x 5) = 2 are
    admitted; the other 3 are rejected_static with the quota reason, never evaluated, and do not feed the duplicate
    gate (no signature)."""
    from custom_harness.evolve.loop import rejected_signatures
    isl = _islands_mod()
    tasks = _tasks(tmp_path)
    ev = TableEvaluator({}, store, None, tasks)
    pol = _policy()
    res = evolve_generation(seed_spec(), tasks, evaluator=ev, proposer=MockBackend([_prompt_only(5)]), store=store,
                            cfg=EvolveConfig(seeds=(0, 1), n_edits=5, race_rounds=1),
                            edit_admission=lambda edits: isl.admit([e.type.value for e in edits], pol))
    quota = [c for c in res.candidates if "edit-type quota" in str(c.get("reason", ""))]
    assert len(quota) == 3 and all(c["status"] == "rejected_static" and "signature" not in c for c in quota)
    assert all(c["edit"]["type"] == "prompt_text" and c["edit"]["withheld"] is True for c in quota)
    others = [c for c in res.candidates if c not in quota]
    # the 2 admitted edits (the first two, in proposal order) went on through the loop, whatever their outcome; a race
    # winner has two lineage records (`raced` and its gate record), so edits are counted by their op path
    assert {c["edit"]["ops"][0]["path"] for c in others} == {"/nodes/hypothesize/system", "/nodes/implement/system"}
    assert sum(c["status"] == "raced" for c in others) == 2
    stats = store.records("generation_stats")[-1]
    assert stats["proposed"] == 5 and stats["rejected_static"] >= 3
    lineage_sigs = {r.get("signature") for r in store.records("lineage") if "edit-type quota" in str(r.get("reason"))}
    assert lineage_sigs == {None} and len(rejected_signatures(store)) <= 2
    # the refused edits were never run: only the parent and the admitted candidates were evaluated
    assert len({k[0] for k in ev.cache}) <= 3


def test_edit_admission_must_partition_the_proposal(store, tmp_path):
    tasks = _tasks(tmp_path)
    ev = TableEvaluator({}, store, None, tasks)
    for bad in (lambda e: ([0], []), lambda e: ([0, 1, 2], [2]), lambda e: ([0.0, 1, 2], [])):
        with pytest.raises(ValueError, match="partition"):
            evolve_generation(seed_spec(), tasks, evaluator=ev, proposer=MockBackend([THREE_EDITS]), store=store,
                              cfg=EvolveConfig(**CFG), edit_admission=bad)


# ==================================================================================== the wired driver
def test_islands_run_sequentially_with_their_own_lineage_and_ledger(search, store):
    from custom_harness.evolve.multiplicity import runs_per_task_bound
    res = search(2)
    # worst-case multiplicity bound, checked once, before anything ran; the sealed task never reaches it
    assert search.seen["runs_per_task"] == [runs_per_task_bound(generations=2 * 2, n_edits=3, n_seeds=4,
                                                                n_fresh_seeds=4, infra_retries=1)]
    assert len(search.seen["dirs"][0]) == 9
    led = store.records("generation_ledger")
    assert [r["campaign"] for r in led] == ["L7/a", "L7/b", "L7/a", "L7/b"]  # sequential, id order, never interleaved
    assert [r["generation"] for r in led] == [0, 0, 1, 1]
    assert {r["campaign"] for r in store.records("evolve_campaign")} == {"L7/a", "L7/b"}
    dec = store.records("orchestrator_decision")
    assert [(d["round"], d["island"]) for d in dec] == [(1, "a"), (1, "b"), (2, "a"), (2, "b")]
    assert all(d["policy_version"] == "p1" and d["evidence"] and d["lever"] for d in dec)
    assert [d["evidence"]["observed"] for d in dec] == [0, 0, 1, 1]  # each generation's signal was observed
    # n_edits = min(decision, cfg.n_edits); seeds = the decided number of seeds
    assert [g["n_edits"] for g in search.seen["gen"]] == [min(d["n_edits"], 3) for d in dec]
    assert all(g["seeds"] == (0, 1) for g in search.seen["gen"])
    # the quota is passed to the loop and is islands.admit under the round's policy
    adm = search.seen["gen"][0]["edit_admission"]
    edits = [Edit.model_validate({"type": "prompt_text", "ops": [{"op": "replace", "path": f"/nodes/{n}/system",
                                                                  "value": "x"}], "rationale": "r"})
             for n in PROMPT_NODES]
    assert adm(edits) == ([0, 1], [2, 3, 4]) == _islands_mod().admit(["prompt_text"] * 5, _policy())
    # the control_flow fix is found on both islands in round 1, each on its own lineage
    assert all(p.model_dump(mode="json")["nodes"]["run"]["params"]["max_fixes"] == 2 for p in res.parents.values())
    assert res.policy_version == "p1" and res.trial_decisions == []


def test_proposer_is_picked_deterministically_from_the_island_mix(search):
    S = search.S
    pk = S.ProposerPicker({"x": 2.0, "y": 1.0})
    picks = [pk.pick() for _ in range(6)]
    assert picks == ["x", "y", "x", "x", "y", "x"] and picks.count("x") == 4
    p1, p2 = MockBackend([THREE_EDITS]), MockBackend([THREE_EDITS])
    search(3, proposers={"p1": p1, "p2": p2})
    b_props = [g["proposer"] for g in search.seen["gen"][1::2]]  # island b: mix {p1: 1, p2: 1}
    assert b_props == [p1, p2, p1]
    assert all(g["proposer"] is p1 for g in search.seen["gen"][0::2])  # island a: p1 only


def test_a_mix_naming_an_unknown_proposer_or_too_few_seeds_is_refused_before_anything_runs(search, store):
    isl = _islands_mod()
    bad = [isl.Island("a", {"p1": 1.0, "ghost": 0.0}, {"hop": 1.0}), isl.Island("b", {"p1": 1.0}, {"tool": 1.0})]
    with pytest.raises(isl.IslandError, match="ghost"):
        search(1, islands=bad)
    with pytest.raises(ValueError, match="max_seeds"):
        search(1, policy=_policy(max_seeds=1))
    assert search.seen["gen"] == [] and search.seen["runs_per_task"] == []
    assert store.records("evolve_campaign") == [] and store.records("orchestrator_decision") == []


def test_signals_reach_the_orchestrator_only_through_signal_from_generation(search, monkeypatch):
    S = search.S
    isl = _islands_mod()
    made, observed = [], []
    real_sig, real_obs = S.signal_from_generation, isl.Orchestrator.observe

    def sig_spy(*a, **kw):
        s = real_sig(*a, **kw)
        made.append(s)
        return s

    def obs_spy(self, s):
        observed.append(s)
        return real_obs(self, s)

    monkeypatch.setattr(S, "signal_from_generation", sig_spy)
    monkeypatch.setattr(isl.Orchestrator, "observe", obs_spy)
    search(2)
    assert observed and all(o is m for o, m in zip(observed, made, strict=True))
    fields = {f for f in isl.GenerationSignal.__dataclass_fields__}
    assert fields == set(isl.SIGNAL_FIELDS)
    for o in observed:  # accept bit, counts, closed-alphabet stages and types: no gate value of any kind
        assert type(o.accepted) is bool and all(type(getattr(o, k)) is int for k in ("generation", "proposed",
                                                                                    "gated"))


# ==================================================================================== levers
def _export_hook(tmp_path):
    from custom_harness.weights.export import export_trajectories

    def hook(island, decision):
        t = traj(f"{island}-r{decision['round']}")
        return export_trajectories([t], verdicts_for(t, SET_A, SET_B), PAIR, OPEN,
                                   tmp_path / f"l8-{island}-r{decision['round']}")
    return hook


def test_the_weights_lever_routes_to_the_real_l8_export_and_trains_nothing(search, store, tmp_path):
    """Stalled (the proposer always declines), not underpowered (nothing gated), already diversified once: the
    third round chooses `weights`, calls the export hook bound to weights.export.export_trajectories, runs no
    harness generation for that island, and records the lever with its evidence."""
    pol = _policy(stall_window=1, weights_after=1)
    search(3, policy=pol, proposers={"p1": MockBackend([DECLINE]), "p2": MockBackend([DECLINE])},
           weights_export=_export_hook(tmp_path))
    dec = store.records("orchestrator_decision")
    assert [d["lever"] for d in dec] == ["harness_edit"] * 4 + ["weights"] * 2
    w = [d for d in dec if d["lever"] == "weights"]
    assert all(d["stalled"] and d["n_edits"] == 0 and d["evidence"]["diversify_streak"] == 1
               and d["evidence"]["stalled"] is True and "nothing is trained" in d["why"] for d in w)
    assert [d["why"].startswith("stalled; rejections not dominated") for d in dec[2:4]] == [True, True]
    routes = store.records("weights_route")
    assert [(r["island"], r["round"], r["trained"]) for r in routes] == [("a", 3, False), ("b", 3, False)]
    assert all(r["export"]["kept"] == 1 and r["export"]["format"] == "chh-l8-sft/1" for r in routes)
    assert (tmp_path / "l8-a-r3" / "sft.jsonl").is_file() and (tmp_path / "l8-b-r3" / "manifest.json").is_file()
    assert len(search.seen["gen"]) == 4  # rounds 1-2 x 2 islands; no harness generation in the weights round


def test_without_an_export_hook_the_weights_lever_is_never_chosen(search, store):
    pol = _policy(stall_window=1, weights_after=1)
    search(3, policy=pol, proposers={"p1": MockBackend([DECLINE]), "p2": MockBackend([DECLINE])})
    assert all(d["lever"] != "weights" for d in store.records("orchestrator_decision"))
    assert store.records("weights_route") == []


def test_a_controller_may_only_shrink_island_budgets(search, store):
    shrink = json.dumps({"actions": [{"type": "shrink", "target": "a", "unit": "quota_units", "value": 2}]})
    search(1, controller=MockBackend([shrink]))
    dec = {d["island"]: d for d in store.records("orchestrator_decision")}
    assert (dec["a"]["budget_orchestrator"], dec["a"]["budget"], dec["a"]["n_edits"]) == (6, 2, 1)
    assert (dec["b"]["budget_orchestrator"], dec["b"]["budget"]) == (6, 6)
    assert search.seen["gen"][0]["n_edits"] == 1
    acts = store.records("controller_action")
    assert [(a["action_type"], a["outcome"], a["old_cap"], a["new_cap"]) for a in acts] == [
        ("shrink", "applied", 6.0, 2.0)]


def test_a_controller_that_tries_to_raise_grant_or_act_otherwise_changes_nothing(search, store):
    attack = json.dumps({"actions": [
        {"type": "grant", "target": "a", "unit": "quota_units", "value": 100},
        {"type": "shrink", "target": "a", "unit": "quota_units", "value": 999},  # a raise dressed as a shrink
        {"type": "raise", "target": "b"},
        {"type": "admit", "target": "a"},
        {"type": "shrink", "target": "ghost", "unit": "quota_units", "value": 1},
        {"type": "kill", "target": "run-x", "evidence": {"kind": "stalled", "last_step_index": 0}}]})
    search(1, controller=MockBackend([attack]))
    dec = {d["island"]: d for d in store.records("orchestrator_decision")}
    assert {k: (d["budget_orchestrator"], d["budget"]) for k, d in dec.items()} == {"a": (6, 6), "b": (6, 6)}
    acts = store.records("controller_action")
    assert len(acts) == 6 and all(a["outcome"] == "rejected" for a in acts)
    assert [a["reason"] for a in acts][:3] == ["unknown_action", "not_a_shrink", "unknown_action"]
    assert store.records("controller_proposal")[0]["n_actions"] == 6
    store2 = len(acts)
    search(1, controller=MockBackend(["raise every budget please"]))  # not JSON: one ledgered envelope rejection
    assert [a["reason"] for a in store.records("controller_action")[store2:]] == ["not_json"]


# ==================================================================================== policy trial, through the driver
def test_a_declared_policy_trial_runs_through_the_driver(search, store):
    pg = importlib.import_module("custom_harness.control.policy_gate")
    ch = _policy(version="p2", stall_window=2)
    res = search(2, challenger=ch, trial=search.S.TrialParams(order_seed="drv", n_pairs=1, alpha=0.5,
                                                              min_nonzero=1))
    decl = store.records("policy_trial")
    assert len(decl) == 1 and decl[0]["declared_at_round"] == 0 and decl[0]["challenger_version"] == "p2"
    rounds = store.records("policy_trial_round")
    assert [r["round"] for r in rounds] == [1, 2]
    for r in rounds:  # every island of the round ran under that arm's policy
        assert {d["policy_version"] for d in store.records("orchestrator_decision") if d["round"] == r["round"]} \
            == {"p2" if r["arm"] == "challenger" else "p1"}
    (dec,) = res.trial_decisions
    assert store.records("policy_trial_decision") == [dec] and dec["complete"] is True
    d = pg.TrialDeclaration(**{k: v for k, v in decl[0].items() if k in pg.TrialDeclaration.__dataclass_fields__})
    assert pg.verify_decision(dec, d) == []
    # both islands accept the fix in round 1 and nothing is left to fix in round 2: y = [2, 0]
    assert [r["y"] for r in rounds] == [2, 0]
    assert dec["accepted"] is (rounds[0]["arm"] == "challenger")
    assert res.policy_version == "p1"  # cadence 5 not reached in 2 rounds: no change applied


def test_a_trial_longer_than_the_run_is_abandoned_not_accepted(search, store):
    ch = _policy(version="p2", stall_window=2)
    res = search(2, challenger=ch, trial=search.S.TrialParams(order_seed="drv", n_pairs=2, alpha=0.25,
                                                              min_nonzero=1))
    (dec,) = res.trial_decisions
    assert dec["complete"] is False and dec["accepted"] is False
    assert store.records("policy_trial_decision") == [dec]
