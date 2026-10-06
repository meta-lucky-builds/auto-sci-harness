"""Winner's curse (CR P0-2, GOAL §7 "simulation: false-accept rate <= alpha").

All candidates are null (true effect 0) and every evaluation is pure noise. Racing still produces a
"winner" whose race data look good. Using those race data as acceptance evidence passes the evolve
criterion far more often than alpha; a fresh re-measurement does not.
"""

import random

from custom_harness.evolve import gate, race
from custom_harness.evolve.selection import paired_ci, paired_sample


def _trial(rng: random.Random, k: int = 8, n: int = 10):
    units = [(f"t{i}", 0) for i in range(n)]
    parent = {u: rng.gauss(0, 1) for u in units}
    race_scores = {(c, u): rng.gauss(0, 1) for c in range(k) for u in units}
    rr = race(list(range(k)), units, lambda c, u: race_scores[(c, u)], parent, rounds=2, keep=1, n_boot=500)
    winner = rr.survivors[0]
    reused = [race_scores[(winner, u)] - parent[u] for u in units]  # what the old loop did
    fresh_units = [(f"t{i}", 1000) for i in range(n)]
    fresh = paired_sample(fresh_units, lambda u: rng.gauss(0, 1), lambda u: rng.gauss(0, 1))
    gate_units = [(f"g{i}", 0) for i in range(n)]
    held_out = paired_sample(gate_units, lambda u: rng.gauss(0, 1), lambda u: rng.gauss(0, 1))
    return reused, fresh, held_out


def test_reusing_race_data_is_biased_and_fresh_remeasure_is_not():
    rng = random.Random(7)
    trials = 400
    reused_pass = fresh_pass = accepts = 0
    for _ in range(trials):
        reused, fresh, held_out = _trial(rng)
        reused_pass += paired_ci(reused)[1] > 0
        fresh_pass += paired_ci(fresh.diffs)[1] > 0
        accepts += gate(fresh, held_out, n_boot=500).accepted
    # one-sided rate of "CI lower bound > 0" under the null is alpha/2 = 2.5% (t interval, exact for normal
    # noise; measured 2.75% at seed 7); SE over 400 trials ~ 0.8 pp
    assert fresh_pass / trials <= 0.045
    assert reused_pass / trials >= 2.5 * fresh_pass / trials  # the winner's curse is real (measured 9.25%)
    assert accepts / trials <= 0.05  # end-to-end false-accept rate on null candidates
