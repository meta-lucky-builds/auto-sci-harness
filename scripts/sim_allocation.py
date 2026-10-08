"""Seeded-world simulations for the race allocators (GOAL §5 L1; docs/control_allocation.md). CPU-light, one process.

    python scripts/sim_allocation.py equivalence --worlds 300
    python scripts/sim_allocation.py voi --worlds 3000

`equivalence`: per world, the FULL selection + acceptance pipeline (race -> fresh re-measure of the winner -> gate +
exclusion check, the real functions of evolve/selection.py) is run with the batch race and with the per-completion
allocator (control/allocator.py, a simulated executor completing work in a seeded random order, slots 1..4), in a
clean environment and with infrastructure outages (an evaluation fails, is retried once with the same seed; a unit
whose retry fails too is EXCLUDED). Reports how many accept/reject decisions differ.

`voi`: the same pipeline with three selectors: the batch race, VOI with the race's spend as its budget in that world
(`voi_matched`: never more evaluations than the race), and VOI with only its own stopping rule (`voi_stop`).
Reports, with Wilson 95% two-sided intervals: the false-accept rate (accepted AND the accepted candidate's true gate
effect <= 0), the true-accept rate, how often the true best candidate was selected, and race evaluations spent.

World model (an assumption, not calibrated on real campaigns): K in 2..6 candidates; 3..6 evolve tasks x 1..2 seeds;
2..4 gate tasks x 1..2 seeds; fresh re-measure seeds disjoint from the race seeds; per-unit difficulty shared by both
arms (paired); true evolve effect mu_c: null (0) w.p. 0.35, harmful U(-0.4, 0) w.p. 0.25, helpful U(0.02, 0.4)
w.p. 0.4; task-level effect heterogeneity N(0, 0.1); within-unit noise sd U(0.2, 0.6); the gate effect is mu_c minus
an overfitting gap U(0, 0.15) w.p. 0.5; each evaluation hits an infrastructure failure w.p. p_infra (default 0.05).
Every value is a deterministic function of (world, arm, unit), so all selectors see the same numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
import time
from dataclasses import dataclass

from custom_harness.control.allocator import MemoryUnitStore, RaceAllocator
from custom_harness.control.voi import VoiConfig, voi_race
from custom_harness.evolve.selection import exclusion_check, gate, paired_sample, race


def _h(*parts: object) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16))


@dataclass
class World:
    seed: int
    cands: list[str]
    mu: dict[str, float]
    mu_gate: dict[str, float]
    het: dict[tuple[str, str], float]
    sd: float
    evolve: list[tuple[str, int]]
    fresh: list[tuple[str, int]]
    gate: list[tuple[str, int]]
    p_infra: float
    rounds: int

    def value(self, arm: str, unit: tuple[str, int]) -> float:
        """Clean score of `arm` ("parent" or a candidate) on `unit`."""
        task = unit[0]
        base = _h(self.seed, "difficulty", unit).gauss(0, 1.0)
        noise = _h(self.seed, "noise", arm, unit).gauss(0, self.sd)
        if arm == "parent":
            return base + noise
        eff = self.mu_gate[arm] if task.startswith("g") else self.mu[arm]
        return base + eff + self.het[(arm, task)] + noise

    def attempt_ok(self, arm: str, unit: tuple[str, int], attempt: int, outages: bool) -> bool:
        return not outages or _h(self.seed, "infra", arm, unit, attempt).random() >= self.p_infra

    def score(self, arm: str, unit: tuple[str, int], outages: bool, retries: int = 1) -> float | None:
        """Evaluator semantics: an infra-failed attempt is retried (same seed) up to `retries` times; still failing
        = excluded (None), never 0."""
        for a in range(1 + retries):
            if self.attempt_ok(arm, unit, a, outages):
                return self.value(arm, unit)
        return None


def make_world(seed: int, p_infra: float = 0.05, *, null: bool = False, big: bool = False) -> World:
    """`null`: every candidate has true effect 0 on evolve and <= 0 on the gate (the global null; any accept is
    false). `big`: 6..12 evolve tasks x 2..3 seeds, 4..6 gate tasks (more units, more power, more selection)."""
    r = random.Random(seed)
    k = r.randint(2, 6)
    cands = [f"c{i}" for i in range(k)]
    mu, mu_gate = {}, {}
    for c in cands:
        x = r.random()
        mu[c] = 0.0 if (null or x < 0.35) else (r.uniform(-0.4, 0.0) if x < 0.6 else r.uniform(0.02, 0.4))
        mu_gate[c] = mu[c] - (r.uniform(0, 0.15) if r.random() < 0.5 else 0.0)
    n_ev, n_g, n_s = r.randint(3, 6), r.randint(2, 4), r.randint(1, 2)
    if big:
        n_ev, n_g, n_s = r.randint(6, 12), r.randint(4, 6), r.randint(2, 3)
    ev_tasks, g_tasks = [f"e{i}" for i in range(n_ev)], [f"g{i}" for i in range(n_g)]
    het = {(c, t): r.gauss(0, 0.1) for c in cands for t in ev_tasks + g_tasks}
    evolve = [(t, s) for t in ev_tasks for s in range(n_s)]
    fresh = [(t, 1000 + s) for t in ev_tasks for s in range(n_s)]
    gate_u = [(t, s) for t in g_tasks for s in range(n_s)]
    return World(seed, cands, mu, mu_gate, het, r.uniform(0.2, 0.6), evolve, fresh, gate_u, p_infra,
                 r.randint(1, 3))


def accept(w: World, winner: str | None, outages: bool) -> bool:
    """The unchanged acceptance rule of evolve/loop.py on the selected candidate: paired Student-t gate on FRESH
    evolve units + the gate split, and the per-arm exclusion check. (LOFO and the cost term only add rejections.)"""
    if winner is None:
        return False
    ev_s = paired_sample(w.fresh, lambda u: w.score(winner, u, outages), lambda u: w.score("parent", u, outages))
    gt_s = paired_sample(w.gate, lambda u: w.score(winner, u, outages), lambda u: w.score("parent", u, outages))
    return gate(ev_s, gt_s).accepted and exclusion_check(ev_s, gt_s)["passed"]


class SimExecutor:
    """Completes ONE pending evaluation per pump(), chosen by a seeded RNG (any completion order)."""

    def __init__(self, fn, seed):
        self.fn, self.rng, self.pending = fn, random.Random(seed), []

    def submit(self, c, u, report, fail):
        self.pending.append((c, u, report))

    def pump(self):
        if self.pending:
            c, u, report = self.pending.pop(self.rng.randrange(len(self.pending)))
            report(c, u, self.fn(c, u))


def _select_batch(w: World, outages: bool):
    parent = {u: w.score("parent", u, outages) for u in w.evolve}
    rr = race(w.cands, w.evolve, lambda c, u: w.score(c, u, outages), parent, rounds=w.rounds, keep=1)
    return rr, parent


def cmd_equivalence(args) -> dict:
    diffs = {"decision": 0, "winner": 0, "outage_vs_clean_decision": 0}
    n = 0
    for seed in range(args.start, args.start + args.worlds):
        w = make_world(seed, args.p_infra, null=args.null, big=args.big)
        for outages in (False, True):
            rr, parent = _select_batch(w, outages)
            win = rr.survivors[0] if rr.survivors else None
            dec = accept(w, win, outages)
            for slots in (1, 2, 3, 4):
                ex = SimExecutor(lambda c, u, o=outages, w=w: w.score(c, u, o), seed * 31 + slots)
                got = RaceAllocator(w.cands, w.evolve, parent, store=MemoryUnitStore(), executor=ex, slots=slots,
                                    rounds=w.rounds, keep=1).run(timeout_s=5)
                gwin = got.survivors[0] if got.survivors else None
                n += 1
                diffs["winner"] += gwin != win
                diffs["decision"] += accept(w, gwin, outages) != dec
        clean_dec = accept(w, (_select_batch(w, False)[0].survivors or [None])[0], False)
        out_dec = accept(w, (_select_batch(w, True)[0].survivors or [None])[0], True)
        diffs["outage_vs_clean_decision"] += clean_dec != out_dec
    return {"worlds": args.worlds, "seeds": [args.start, args.start + args.worlds - 1], "comparisons": n,
            "p_infra": args.p_infra, "null": args.null, "big": args.big, **diffs}


def wilson(k: int, n: int, z: float = 1.959964) -> list[float]:
    if n == 0:
        return [0.0, 1.0]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 5), round(min(1.0, c + h), 5)]


def cmd_voi(args) -> dict:
    names = ("race", "voi_matched", "voi_stop")
    acc = {k: {"false_accept": 0, "true_accept": 0, "best_selected": 0, "evals": 0, "null_worlds_fa": 0} for k in names}
    le_race = {"voi_matched": 0, "voi_stop": 0}
    null_worlds = 0
    voi_cfg = VoiConfig(stop_frac=args.stop_frac, n_init=args.n_init)
    for seed in range(args.start, args.start + args.worlds):
        w = make_world(seed, args.p_infra, null=args.null, big=args.big)
        outages = True
        rr, parent = _select_batch(w, outages)
        sel = {"race": rr}
        sel["voi_matched"] = voi_race(w.cands, w.evolve, lambda c, u, w=w: w.score(c, u, True), parent,
                                      rounds=w.rounds, cfg=VoiConfig(stop_frac=args.stop_frac, n_init=args.n_init,
                                                                     max_evals=rr.evaluations))
        sel["voi_stop"] = voi_race(w.cands, w.evolve, lambda c, u, w=w: w.score(c, u, True), parent, rounds=w.rounds,
                                   cfg=voi_cfg)
        best = max(w.cands, key=lambda c: (w.mu_gate[c], -w.cands.index(c)))
        is_null = all(w.mu_gate[c] <= 0 for c in w.cands)
        null_worlds += is_null
        for k in names:
            r = sel[k]
            win = r.survivors[0] if r.survivors else None
            a = accept(w, win, outages)
            good = win is not None and w.mu_gate[win] > 0
            acc[k]["false_accept"] += a and not good
            acc[k]["null_worlds_fa"] += a and is_null
            acc[k]["true_accept"] += a and good
            acc[k]["best_selected"] += win == best
            acc[k]["evals"] += r.evaluations
            if k != "race":
                le_race[k] += r.evaluations <= rr.evaluations
    n = args.worlds
    out: dict = {"worlds": n, "seeds": [args.start, args.start + n - 1], "p_infra": args.p_infra, "null": args.null,
                 "big": args.big,
                 "voi_cfg": voi_cfg.__dict__, "null_worlds": null_worlds}
    for k in names:
        a = acc[k]
        out[k] = {"false_accept_rate": a["false_accept"] / n, "false_accept_wilson95": wilson(a["false_accept"], n),
                  "false_accepts": a["false_accept"],
                  "null_world_fa_rate": a["null_worlds_fa"] / max(1, null_worlds),
                  "null_world_fa_wilson95": wilson(a["null_worlds_fa"], null_worlds),
                  "true_accept_rate": a["true_accept"] / n, "true_accept_wilson95": wilson(a["true_accept"], n),
                  "best_selected_rate": a["best_selected"] / n, "mean_race_evals": a["evals"] / n}
        if k != "race":
            out[k]["worlds_with_evals_le_race"] = le_race[k]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("equivalence", "voi"))
    ap.add_argument("--worlds", type=int, default=300)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--p-infra", type=float, default=0.05)
    ap.add_argument("--stop-frac", type=float, default=VoiConfig.stop_frac)
    ap.add_argument("--n-init", type=int, default=VoiConfig.n_init)
    ap.add_argument("--null", action="store_true")
    ap.add_argument("--big", action="store_true")
    args = ap.parse_args(argv)
    t0 = time.time()
    res = cmd_equivalence(args) if args.cmd == "equivalence" else cmd_voi(args)
    res["seconds"] = round(time.time() - t0, 1)
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
