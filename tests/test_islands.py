"""L7 search over search (GOAL §5): islands with different proposer/mutator mixes, an orchestrator that detects
stalls and reallocates budget across islands, edit types and levers with logged evidence, edit-type quotas against
prompt-only collapse, a versioned policy on a slower cadence. Deterministic, scripted islands; no model calls."""

from __future__ import annotations

import json

import pytest

from custom_harness.control.islands import (
    DECISION_KIND,
    POLICY_KIND,
    PROMPT_TYPE,
    GenerationSignal,
    Island,
    IslandError,
    Orchestrator,
    OrchestratorPolicy,
    admit,
    plan_edit_types,
    signal_from_generation,
)

POLICY = OrchestratorPolicy(version="orch-1", stall_window=3, more_eval_share=0.5, stall_shrink=0.5,
                            min_island_share=0.1, prompt_max_share=0.4, min_edit_types=2, cadence=3)


def _islands():
    return [Island("promptish", {"codex": 1.0}, {"prompt_text": 0.8, "contract": 0.2}),
            Island("noisy", {"local": 1.0}, {"hop": 0.5, "control_flow": 0.5}),
            Island("good", {"codex": 0.5, "local": 0.5}, {"contract": 0.5, "tool": 0.5})]


def _sig(island, g, accepted, gated, rejected_on, types):
    return {"island": island, "generation": g, "accepted": accepted, "proposed": len(types), "gated": gated,
            "rejected_on": rejected_on, "edit_types": types}


def _script(orch, rounds=3):
    """promptish: stalls, rejected on the held-out gate, proposing mostly prompt edits. noisy: stalls, every gated
    candidate rejected on the (underpowered) evolve re-measurement. good: accepts every other generation."""
    for g in range(rounds):
        orch.observe(_sig("promptish", g, False, 1, ["held_out_gate"], ["prompt_text", "prompt_text", "contract"]))
        orch.observe(_sig("noisy", g, False, 2, ["evolve", "evolve"], ["hop", "control_flow"]))
        orch.observe(_sig("good", g, g % 2 == 0, 1, [] if g % 2 == 0 else ["cost"], ["contract", "tool"]))


def test_islands_must_differ_and_be_at_least_two():
    a = Island("a", {"codex": 1.0}, {"contract": 1.0})
    with pytest.raises(IslandError, match="same proposer and mutator mix"):
        Orchestrator([a, Island("b", {"codex": 2.0}, {"contract": 2.0})], POLICY, total_budget=10)  # same mix
    with pytest.raises(IslandError, match="at least two"):
        Orchestrator([a], POLICY, total_budget=10)
    with pytest.raises(IslandError, match="unknown edit types"):
        Island("c", {"codex": 1.0}, {"rewrite_everything": 1.0})


def test_stalls_are_detected_and_the_lever_and_its_evidence_are_logged(store):
    orch = Orchestrator(_islands(), POLICY, total_budget=30, store=store)
    _script(orch)
    d = {x["island"]: x for x in orch.step()}
    assert (d["promptish"]["stalled"], d["noisy"]["stalled"], d["good"]["stalled"]) == (True, True, False)
    # noisy: its rejections were on the evolve re-measurement -> more evaluation (one more seed per unit)
    assert d["noisy"]["lever"] == "more_evaluation" and d["noisy"]["n_seeds"] == 2
    assert d["noisy"]["evidence"]["rejected_on"] == {"evolve": 6} and d["noisy"]["evidence"]["gated"] == 6
    # promptish: rejected on the held-out gate -> new harness edits, diversified mix, shrunk budget
    assert d["promptish"]["lever"] == "harness_edit" and "diversify" in d["promptish"]["why"]
    assert d["promptish"]["mutator_mix"][PROMPT_TYPE] < 0.8 and len(d["promptish"]["mutator_mix"]) > 2
    assert d["good"]["lever"] == "harness_edit" and d["good"]["why"] == "not stalled"
    # budget: reallocated away from the island stalled on edits, sums exactly, floor respected
    assert sum(x["budget"] for x in d.values()) == 30
    assert d["promptish"]["budget"] < d["good"]["budget"] and min(x["budget"] for x in d.values()) >= 3
    assert all("weights" != x["lever"] for x in d.values())  # L8 lever: declared, never chosen (not built)
    logged = store.records(DECISION_KIND)
    assert [x["island"] for x in logged] == ["good", "noisy", "promptish"]
    assert all(x["policy_version"] == "orch-1" and x["policy_hash"] == POLICY.policy_hash() and x["evidence"]
               for x in logged)


def test_orchestrator_is_deterministic():
    runs = []
    for _ in range(2):
        orch = Orchestrator(_islands(), POLICY, total_budget=30)
        _script(orch)
        runs.append(json.dumps([orch.step(), orch.step()], sort_keys=True))
    assert runs[0] == runs[1]


def test_edit_type_quota_prevents_collapse_into_prompt_only_edits():
    plan = plan_edit_types({PROMPT_TYPE: 1.0}, 5, POLICY)
    assert len(plan) == 5 and plan.count(PROMPT_TYPE) == 2 and len(set(plan)) >= 2  # floor(0.4 * 5) = 2
    for n in range(1, 12):
        p = plan_edit_types({PROMPT_TYPE: 0.95, "contract": 0.05}, n, POLICY)
        assert len(p) == n and p.count(PROMPT_TYPE) <= int(0.4 * n) and len(set(p)) >= min(2, n)
    ok, refused = admit([PROMPT_TYPE, "hop", PROMPT_TYPE, PROMPT_TYPE, PROMPT_TYPE], POLICY)
    assert ok == [0, 1, 2] and refused == [3, 4]  # cap = floor(0.4 * 5) = 2 prompt-only edits
    # an island whose proposer keeps sending prompt edits: its mix is capped and every plan obeys the quota
    orch = Orchestrator([Island("p", {"codex": 1.0}, {PROMPT_TYPE: 1.0}),
                         Island("q", {"local": 1.0}, {"hop": 1.0})], POLICY, total_budget=10)
    whys = []
    for g in range(6):
        orch.observe(_sig("p", g, True, 1, [], [PROMPT_TYPE] * 3))  # accepted: not stalled, but collapsing
        orch.observe(_sig("q", g, True, 1, [], ["hop"]))
        d = {x["island"]: x for x in orch.step()}
        whys.append(d["p"]["why"])
        assert d["p"]["edit_plan"].count(PROMPT_TYPE) <= int(0.4 * len(d["p"]["edit_plan"]))
        assert d["p"]["mutator_mix"][PROMPT_TYPE] <= 0.4 + 1e-9
    assert "prompt weight capped" in whys[0]  # capped on the first round that saw the collapse


def _accept_if_challenger(orch):
    """Trial evidence for the policy test below: an accepting island in the challenger's round, none otherwise."""
    if orch.trial is not None and orch.trial.arm(orch.round) == "challenger":
        orch.observe(_sig("good", orch.round, True, 1, [], ["contract"]))


def test_the_policy_is_versioned_and_changes_only_on_a_slower_cadence(store):
    orch = Orchestrator(_islands(), POLICY, total_budget=30, store=store)
    new = OrchestratorPolicy(version="orch-2", stall_window=2, cadence=3)
    # a change needs a passing policy trial (control/policy_gate.py; test_policy_gate.py tests the gate itself).
    # Here the smallest trial that fits this test's three rounds: one AB/BA pair, min_nonzero 1, alpha 1/2.
    decl = orch.declare_trial(new, order_seed="test", n_pairs=1, alpha=0.5, min_nonzero=1)
    orch.step()
    _accept_if_challenger(orch)
    with pytest.raises(IslandError, match="every 3 rounds"):
        orch.set_policy(new)
    with pytest.raises(IslandError, match="used before"):
        orch.set_policy(OrchestratorPolicy(version="orch-1", stall_window=9))  # same version, other parameters
    orch.step()
    _accept_if_challenger(orch)
    orch.step()
    orch.set_policy(new, trial_decision=orch.trial_decisions[decl.trial_id])
    assert orch.policy is new and [r["version"] for r in store.records(POLICY_KIND)] == ["orch-1", "orch-2"]
    assert store.records(POLICY_KIND)[-1]["round"] == 3
    assert orch.step()[0]["policy_version"] == "orch-2"
    assert POLICY.policy_hash() != OrchestratorPolicy(version="orch-1", stall_window=9).policy_hash()


def test_attack_signal_carrying_held_out_values_is_refused():
    """A signal that smuggles a continuous gate value (or a per-task gate result) into the orchestrator."""
    base = _sig("good", 0, False, 1, ["held_out_gate"], ["contract"])
    attacks = [base | {"gate_mean_diff": 0.137}, base | {"per_task": {"g0": -0.2}}, base | {"proposed": 1.37},
               base | {"accepted": 0.6}, base | {"rejected_on": ["gate:lo=-0.02"]}]
    for a in attacks:
        assert a != base  # the attack really carries something the base does not
        with pytest.raises(IslandError):
            GenerationSignal.from_view(a)
    orch = Orchestrator(_islands(), POLICY, total_budget=30)
    with pytest.raises(IslandError):
        orch.observe(attacks[0])
    orch.observe(base)  # positive control


def test_signals_from_real_generation_records_use_only_the_proposer_visible_views():
    stats = {"parent": "p", "proposed": 2, "accepted": 0, "rejected_gate": 1, "gated": 1, "raced": 1,
             "yield": 0.0, "secret_gate_mean": 0.42}
    lineage = [{"status": "raced", "candidate": "c", "edit": {"type": "hop"}, "race": {}},
               {"status": "rejected_static", "edit": {"type": "prompt_text"}, "reason": "x"},
               {"status": "rejected_gate", "candidate": "c", "rejected_on": "held_out_gate",
                "gate": {"mean_diff": -0.3, "lo": -0.5}, "lofo": {"ood_drop": {"family:H": -0.3}}}]
    s = signal_from_generation("good", 4, stats, lineage)
    assert s == GenerationSignal("good", 4, False, 2, 1, ("held_out_gate",), ("hop", "prompt_text"))


# ==================================================================================== one stall definition
def test_the_orchestrator_stall_decision_is_generations_stall_evidence(monkeypatch):
    """Exactly ONE function defines "stalled": the orchestrator follows evolve.generations.stall_evidence. Patched
    to the opposite answer, every island's stall decision flips with it (an inline copy of the rule would not)."""
    from custom_harness.evolve import generations
    real = generations.stall_evidence
    seen = []

    def opposite(signals, window=generations.STALL_WINDOW):
        ev = real(signals, window)
        seen.append(window)
        return ev | {"stalled": not ev["stalled"]}

    orch = Orchestrator(_islands(), POLICY, total_budget=30)
    _script(orch)
    monkeypatch.setattr(generations, "stall_evidence", opposite)
    d = {x["island"]: x for x in orch.step()}
    assert (d["promptish"]["stalled"], d["noisy"]["stalled"], d["good"]["stalled"]) == (False, False, True)
    assert d["good"]["why"].startswith("stalled") and d["noisy"]["lever"] == "harness_edit"
    assert seen and set(seen) == {POLICY.stall_window}  # the policy's window is passed to the one rule
    assert d["good"]["evidence"]["stalled"] is True  # the decision's evidence is that function's output


def test_the_orchestrator_source_has_no_inline_stall_rule():
    """Static check: in the decision methods of class Orchestrator (step, evidence) no comparison reads `accepted`,
    `observed` or `stall_window` (the ingredients of the stall rule), and `stall_evidence` is called. Comments and
    docstrings are not code."""
    import ast
    import inspect

    from custom_harness.control import islands
    tree = ast.parse(inspect.getsource(islands))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Orchestrator")
    banned = {"accepted", "observed", "stall_window"}

    def names(node):
        for x in ast.walk(node):
            if isinstance(x, ast.Name):
                yield x.id
            elif isinstance(x, ast.Attribute):
                yield x.attr
            elif isinstance(x, ast.Constant) and isinstance(x.value, str):
                yield x.value
    methods = [f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name in ("step", "evidence")]
    assert len(methods) == 2
    bad = [ast.unparse(c) for f in methods for c in ast.walk(f)
           if isinstance(c, ast.Compare) and banned & set(names(c))]
    assert bad == []
    calls = {ast.unparse(c.func) for c in ast.walk(tree) if isinstance(c, ast.Call)}
    assert "generations.stall_evidence" in calls and "stall_evidence" in calls


def test_signals_inconsistent_with_the_ledger_shape_are_refused():
    """An accept needs a gated candidate, and accepts + rejections cannot exceed the gated count; a GenerationSignal
    built directly is re-validated too (it no longer bypasses the whitelist)."""
    with pytest.raises(IslandError):
        GenerationSignal.from_view(_sig("good", 0, True, 0, [], ["contract"]))
    with pytest.raises(IslandError):
        GenerationSignal.from_view(_sig("good", 0, True, 1, ["cost"], ["contract"]))
    orch = Orchestrator(_islands(), POLICY, total_budget=30)
    with pytest.raises(IslandError):
        orch.observe(GenerationSignal("good", 0, True, 1, 0, (), ("contract",)))
    with pytest.raises(IslandError):
        orch.observe(GenerationSignal("good", 0, False, 1, 1, ("gate:0.13",), ("contract",)))
    orch.observe(GenerationSignal("good", 0, True, 1, 1, (), ("contract",)))  # positive control
