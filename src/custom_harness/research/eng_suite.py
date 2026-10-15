"""Engineering task suite (GOAL M5, §15 "task-suite builder"): linear DC circuit families, domain `engineering`.

    build_engineering_suite(OUT_DIR)            # engineering only, one family held out (LOFO)
    build_cross_domain_suite(OUT_DIR)           # ML synthetic + engineering, one whole DOMAIN held out (LODO)

Every task follows research/task.py (public train + test features, private test labels + canary, a baseline obeying
the solution CLI contract), so the existing loop and evolver run it unchanged. Each row is one circuit of the
family's topology with random component values; the clean target is the EXACT value of the queried quantity in the
ideal linear DC model, computed by the `circuit` adapter's exact rational MNA solver from the PRINTED feature values
(no rounding before the solve). A measurement-noise term is added from the task's own keyed `labels` stream.

Keyed independent streams (GOAL §16.1, the COUPLED-PRNG lesson): features, label noise and the split of a task each
come from their OWN `commitment.stream` under a per-task key derived from a master key (fresh OS entropy by default;
a `draw()` result when a caller mints). Nothing is seeded from a published constant, so no held-out label can be
regenerated from the code: without the labels-stream key the noise is unknown (invariant 2). The clean physics is
public by design (it is the thing a solution may discover), exactly as Friedman-1's formula is public.

Sources (recorded in suite.yaml): the topologies are textbook circuits; the model is modified nodal analysis
(Ho, Ruehli and Brennan, IEEE Trans. Circuits and Systems, 1975: REPORTED from memory, not re-checked here). No
external data is used or downloaded.
"""

from __future__ import annotations

import hashlib
import json
import random
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import yaml

from ..adapters.circuit import check as circuit_check
from ..adapters.circuit import solver as circuit_solver
from ..commitment import stream
from .suite import (
    FAMILIES as ML_FAMILIES,
)
from .suite import (
    GENERATOR_VERSION,
    MEAN,
    SYNTHETIC,
    TaskSpecFiles,
    _fill,
    _key,
    _split,
    assign_splits,
    check_split_isolation,
    task_key,
    write_task,
)

DOMAIN = "engineering"
ENG_GENERATOR_VERSION = "keyed-hmac-sha256/1+chh-circuit/1"
NOISE_REL = 0.01  # measurement noise sd = NOISE_REL * max(|clean value|, family floor); UNCALIBRATED


def _r(eid: str, pos: str, neg: str, value: int) -> dict[str, str]:
    return {"id": eid, "type": "resistor", "pos": pos, "neg": neg, "value": str(value), "unit": "ohm"}


def _v(eid: str, pos: str, neg: str, value: int) -> dict[str, str]:
    return {"id": eid, "type": "vsource", "pos": pos, "neg": neg, "value": str(value), "unit": "V"}


def exact_value(nodes: list[str], elements: list[dict[str, str]], query: dict[str, str]) -> Fraction:
    """The exact queried value of one netlist in the ideal linear DC model (the adapter's solver; it raises on a
    singular network, which no family below can produce: every resistor value is >= 1 ohm)."""
    doc = {"format": circuit_check.FORMAT, "ground": "0", "nodes": nodes, "elements": elements, "query": query}
    c = circuit_check.parse_problem(json.dumps(doc, sort_keys=True).encode())
    red = circuit_solver.reduce(*circuit_check.mna(c))
    if len(red.pivots) != len(c.unknowns):
        raise ValueError(f"singular netlist: {circuit_check.describe(c)} is not determined")
    return circuit_check.quantity(c, red.rhs)


def _ladder(f: random.Random) -> tuple[list[int], Fraction]:
    """Three-section resistor ladder: Vs -> R1 -> a -> R3 -> b -> R5 -> c, shunts R2 (a), R4 (b), R6 (c) to ground."""
    vs, rs = f.randint(1, 24), [f.randint(10, 10_000) for _ in range(6)]
    els = [_v("Vs", "s", "0", vs), _r("R1", "s", "a", rs[0]), _r("R2", "a", "0", rs[1]), _r("R3", "a", "b", rs[2]),
           _r("R4", "b", "0", rs[3]), _r("R5", "b", "c", rs[4]), _r("R6", "c", "0", rs[5])]
    return [vs, *rs], exact_value(["0", "s", "a", "b", "c"], els, {"quantity": "node_voltage", "node": "c",
                                                                    "unit": "V"})


def _bridge(f: random.Random) -> tuple[list[int], Fraction]:
    """Wheatstone bridge: Vs across (R1 + R2) and (R3 + R4); detector Rg between the midpoints. Target: the detector
    current in mA (into Rg's pos = left midpoint)."""
    vs, rs = f.randint(1, 24), [f.randint(100, 10_000) for _ in range(4)]
    rg = f.randint(10, 1_000)
    els = [_v("Vs", "s", "0", vs), _r("R1", "s", "l", rs[0]), _r("R2", "l", "0", rs[1]), _r("R3", "s", "r", rs[2]),
           _r("R4", "r", "0", rs[3]), _r("Rg", "l", "r", rg)]
    i = exact_value(["0", "s", "l", "r"], els, {"quantity": "branch_current", "element": "Rg", "unit": "A"})
    return [vs, *rs, rg], i * 1000


def _amplifier(f: random.Random) -> tuple[list[int], Fraction]:
    """Inverting amplifier around a finite-gain VCVS (open-loop gain A, output resistance 0): Vin -> Rin -> m,
    Rf from m to o, VCVS o = -A v(m), load RL on o. Target: v(o)."""
    vin, rin, rf, gain, rl = f.randint(-5, 5), f.randint(100, 10_000), f.randint(100, 100_000), f.randint(10, 10_000), \
        f.randint(100, 10_000)
    els = [_v("Vin", "i", "0", vin), _r("Rin", "i", "m", rin), _r("Rf", "m", "o", rf),
           {"id": "E1", "type": "vcvs", "pos": "o", "neg": "0", "ctrl_pos": "0", "ctrl_neg": "m", "value": str(gain),
            "unit": "V/V"}, _r("RL", "o", "0", rl)]
    return [vin, rin, rf, gain, rl], exact_value(["0", "i", "m", "o"], els, {"quantity": "node_voltage", "node": "o",
                                                                          "unit": "V"})


@dataclass(frozen=True)
class EngFamily:
    make: Callable[[random.Random], tuple[list[int], Fraction]]
    title: str
    description: str
    metric: str
    target: str
    features: list[str]
    noise_floor: float  # noise sd = NOISE_REL * max(|clean|, noise_floor) (target units)
    source: str  # recorded provenance of the family


ENG_FAMILIES: dict[str, EngFamily] = {
    "ladder": EngFamily(_ladder, "Resistor ladder output voltage (RMSE)",
                        "Predict the output voltage `v_out` (V) of a three-section resistor ladder from the source "
                        "voltage vs (V) and six resistances r1..r6 (ohm). Labels carry ~1% measurement noise. Lower "
                        "RMSE is better.", "rmse", "v_out", ["vs", *(f"r{i}" for i in range(1, 7))], 0.01,
                        "textbook resistor ladder; exact MNA (custom_harness.adapters.circuit.solver)"),
    "bridge": EngFamily(_bridge, "Wheatstone bridge detector current (MAE)",
                        "Predict the detector current `i_g_mA` (mA) of a Wheatstone bridge from the source voltage vs "
                        "(V), the four arm resistances r1..r4 (ohm) and the detector resistance rg (ohm). Labels "
                        "carry ~1% measurement noise. Lower MAE is better.", "mae", "i_g_mA",
                        ["vs", "r1", "r2", "r3", "r4", "rg"], 0.01,
                        "textbook Wheatstone bridge; exact MNA (custom_harness.adapters.circuit.solver)"),
    "amplifier": EngFamily(_amplifier, "Finite-gain inverting amplifier output (RMSE)",
                           "Predict the output voltage `v_out` (V) of an inverting amplifier built on a finite-gain "
                           "voltage amplifier, from vin (V), rin, rf, rl (ohm) and the open-loop gain a. Labels carry "
                           "~1% measurement noise. Lower RMSE is better.", "rmse", "v_out",
                           ["vin", "rin", "rf", "a", "rl"], 0.01,
                           "textbook inverting amplifier (VCVS model); exact MNA "
                           "(custom_harness.adapters.circuit.solver)"),
}
# Held out of evolution by default (leave-one-family-out): the bridge is the only topology with a balanced
# (difference) output, so it tests transfer to a structurally different circuit. Rotate across campaigns.
DEFAULT_HELDOUT_FAMILIES: tuple[str, ...] = ("bridge",)
ENG_TASKS: list[tuple[str, int, int]] = [("ladder", 1, 600), ("ladder", 2, 900), ("bridge", 1, 600),
                                         ("bridge", 2, 900), ("amplifier", 1, 600), ("amplifier", 2, 900)]


def _rows(fam: EngFamily, f: random.Random, lab: random.Random, n: int) -> list[list[str]]:
    rows = []
    for _ in range(n):
        feats, clean = fam.make(f)
        y = float(clean) + lab.gauss(0.0, NOISE_REL * max(abs(float(clean)), fam.noise_floor))
        rows.append([*(str(v) for v in feats), f"{y:.6f}"])
    return rows


def engineering_task(family: str, variant: int, n: int, key: bytes | None = None) -> TaskSpecFiles:
    """Task `<family>-e<variant>` with n rows (70/30 split) from `key` (None: fresh OS entropy)."""
    fam, k = ENG_FAMILIES[family], _key(key)
    rows = _rows(fam, stream(k, "features"), stream(k, "labels"), n)
    tr, te = _split(rows, stream(k, "split"))
    return TaskSpecFiles(f"{family}-e{variant}", fam.title, fam.description, fam.metric, fam.target,
                         [*fam.features, fam.target], tr, te, _fill(MEAN, fam.target), family)


def _write(out: Path, specs: list[TaskSpecFiles], splits: dict[str, str], domains: dict[str, str], master: bytes,
           key: bytes | None) -> Path:
    canaries = {s.data_source: "CHH-CANARY-" + secrets.token_hex(16) for s in specs}
    entries = []
    for s in sorted(specs, key=lambda t: (domains[t.task_id], t.family, t.task_id)):
        d = write_task(s, out, canary=canaries[s.data_source])
        entries.append({"id": s.task_id, "dir": str(d.resolve()), "split": splits[s.task_id], "family": s.family,
                        "domain": domains[s.task_id], "source": s.data_source, "origin": s.origin,
                        "retrospective_target": False, "minted": False})
    problems = check_split_isolation(entries)
    if problems:  # fail closed: never write a suite whose splits leak into each other
        raise ValueError("suite splits are not isolated: " + "; ".join(problems))
    generator = {"version": ENG_GENERATOR_VERSION, "ml_version": GENERATOR_VERSION,
                 "key_commitment": hashlib.sha256(master).hexdigest(),
                 "key_source": "caller" if key is not None else "os_entropy"}
    sources = {f"{DOMAIN}/{name}": fam.source for name, fam in ENG_FAMILIES.items()
               if any(s.family == name for s in specs)}
    sources |= {f"ml/{name}": "synthetic generator custom_harness.research.suite (stdlib, keyed streams)"
                for name in ML_FAMILIES if any(s.family == name and domains[s.task_id] == "ml" for s in specs)}
    (out / "suite.yaml").write_text(yaml.safe_dump({"generator": generator, "sources": sources, "tasks": entries},
                                                   sort_keys=False), encoding="utf-8")
    return out / "suite.yaml"


def engineering_specs(master: bytes, rows: int | None = None) -> list[TaskSpecFiles]:
    return [engineering_task(fam, v, rows or n, key=task_key(master, f"{fam}-e{v}")) for fam, v, n in ENG_TASKS]


def build_engineering_suite(out: Path, *, heldout_families: tuple[str, ...] = DEFAULT_HELDOUT_FAMILIES,
                            key: bytes | None = None, rows: int | None = None) -> Path:
    """Write every engineering task and suite.yaml (domain `engineering`; `heldout_families` all in gate). `key`:
    master key (None: OS entropy; never written, only its SHA-256 commitment). `rows` overrides every task's size."""
    out, master = Path(out), _key(key)
    specs = engineering_specs(master, rows)
    splits = assign_splits(specs, heldout_families)
    return _write(out, specs, splits, dict.fromkeys((s.task_id for s in specs), DOMAIN), master, key)


def build_cross_domain_suite(out: Path, *, heldout_domain: str = DOMAIN,
                             ml_heldout_families: tuple[str, ...] = ("spirals",),
                             eng_heldout_families: tuple[str, ...] = DEFAULT_HELDOUT_FAMILIES,
                             key: bytes | None = None, rows: int | None = None) -> Path:
    """ML synthetic + engineering tasks in one suite with a held-out DOMAIN (GOAL §5 L6.8, invariant 16): every task
    of `heldout_domain` goes to the gate split (gate tasks, no evolve task); the other domain keeps its own split
    assignment with its held-out family. Synthetic ML tasks only (no sandbox, no downloads)."""
    if heldout_domain not in ("ml", DOMAIN):
        raise ValueError(f"unknown domain {heldout_domain!r}")
    out, master = Path(out), _key(key)
    ml = [gen(seed, rows or n, key=task_key(master, gen(seed, 1, key=master).task_id)) for gen, seed, n in SYNTHETIC]
    eng = engineering_specs(master, rows)
    domains = dict.fromkeys((s.task_id for s in ml), "ml") | dict.fromkeys((s.task_id for s in eng), DOMAIN)
    held, kept, kept_families = (eng, ml, ml_heldout_families) if heldout_domain == DOMAIN else \
        (ml, eng, eng_heldout_families)
    splits = assign_splits(kept, kept_families) | dict.fromkeys((s.task_id for s in held), "gate")
    return _write(out, ml + eng, splits, domains, master, key)


__all__ = ["DEFAULT_HELDOUT_FAMILIES", "DOMAIN", "ENG_FAMILIES", "ENG_TASKS", "build_cross_domain_suite",
           "build_engineering_suite", "engineering_task", "exact_value"]
