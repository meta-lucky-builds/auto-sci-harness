"""Value-of-information race allocation (control/voi.py, GOAL §5 L1, §8 item 7): it changes only the order and
allocation of race evaluations; the acceptance rule is untouched, it is deterministic, it has no input through which
gate or sealed values could reach it, it never scores an excluded unit, and in seeded worlds the false-accept rate of
the full pipeline stays within alpha while it spends no more race evaluations than racing (matched budget)."""

import inspect
import math
import random
import sys
from pathlib import Path

import pytest

from custom_harness.control.voi import VoiConfig, knowledge_gradient, race_order, voi_race
from custom_harness.evolve.selection import race

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sim_allocation as sim


def _world(seed):
    rng = random.Random(seed)
    cands = [f"c{i}" for i in range(rng.randint(2, 5))]
    units = [(f"t{t}", s) for t in range(rng.randint(3, 6)) for s in range(2)]
    parent = {u: rng.gauss(0, 1) for u in units}
    eff = {c: rng.uniform(-0.5, 0.5) for c in cands}
    table = {(c, u): None if rng.random() < 0.08 else parent[u] + eff[c] + rng.gauss(0, 0.4)
             for c in cands for u in units}
    return cands, units, parent, table


@pytest.mark.parametrize("seed", range(40))
def test_deterministic_capped_and_never_scores_an_excluded_unit(seed):
    cands, units, parent, table = _world(seed)
    calls = []

    def ev(c, u):
        calls.append((c, u))
        return table[(c, u)]

    a = voi_race(cands, units, ev, parent, rounds=2)
    first = list(calls)
    calls.clear()
    b = voi_race(cands, units, ev, parent, rounds=2)
    assert a.survivors == b.survivors and a.stats == b.stats and calls == first  # same order, same result
    assert a.evaluations == len(first) and len(set(first)) == len(first)  # nothing evaluated twice
    for c in cands:
        mine = [u for cc, u in first if cc == c]
        assert mine == race_order(units, 2)[:len(mine)]  # each candidate walks the race's unit order (paired)
        valid = [u for u in mine if table[(c, u)] is not None]
        assert a.stats[c]["n_units"] == len(valid) and a.stats[c]["excluded"] == len(mine) - len(valid)
    capped = voi_race(cands, units, ev, parent, rounds=2, cfg=VoiConfig(max_evals=len(cands) + 1))
    assert capped.evaluations <= len(cands) + 1


def test_the_signature_has_no_held_out_input():
    """Gate/sealed values reach the evolver only as embargoed verdict bits (GOAL §16.5.3); this policy takes none:
    its only data inputs are the race's own evaluate callback and the parent's race-unit scores."""
    params = set(inspect.signature(voi_race).parameters)
    assert params == {"candidates", "units", "evaluate", "parent_scores", "rounds", "keep", "cfg"}


def test_knowledge_gradient_basics():
    assert knowledge_gradient(0.0, 0.0, 1.0, 0) == math.inf  # an arm with no valid unit is worth sampling
    near = knowledge_gradient(0.01, 0.0, 1.0, 4)
    far = knowledge_gradient(2.0, 0.0, 1.0, 4)
    assert near > far > 0  # contested arms are worth more
    assert knowledge_gradient(0.0, 0.0, 1.0, 4) > knowledge_gradient(0.0, 0.0, 1.0, 40)  # diminishing returns


def test_parent_must_cover_every_unit_as_in_the_race():
    with pytest.raises(ValueError, match="parent"):
        voi_race(["a"], [("t", 0)], lambda c, u: 1.0, {}, rounds=1)
    with pytest.raises(ValueError, match="parent"):
        race(["a"], [("t", 0)], lambda c, u: 1.0, {}, rounds=1)


def test_evolve_generation_wiring_accepts_voi_and_keeps_the_acceptance_rule():
    """The loop's acceptance code path is shared: only `_race` dispatches on the allocator."""
    from custom_harness.evolve import loop
    src = inspect.getsource(loop.evolve_generation)
    assert src.count("_race(") == 1 and "gate(evolve_s, gate_s" in src
    loop.check_race_config(loop.EvolveConfig(race_allocator="voi"))


def test_seeded_worlds_false_accepts_within_alpha_and_no_more_evaluations_than_racing():
    """Small version of `scripts/sim_allocation.py voi` (the full runs are reported in docs/control_allocation.md):
    global-null worlds (every accept is false) and mixed worlds. alpha = 0.05 (the paired CI's two-sided level)."""
    import argparse
    for null in (True, False):
        res = sim.cmd_voi(argparse.Namespace(worlds=150, start=0, p_infra=0.05, stop_frac=VoiConfig.stop_frac,
                                             n_init=VoiConfig.n_init, null=null, big=True))
        for k in ("race", "voi_matched", "voi_stop"):
            assert res[k]["false_accept_wilson95"][0] <= 0.05, (null, k, res[k])
        assert res["voi_matched"]["worlds_with_evals_le_race"] == 150
        assert res["voi_matched"]["mean_race_evals"] <= res["race"]["mean_race_evals"]
