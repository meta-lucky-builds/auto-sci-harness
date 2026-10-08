"""M5 engineering task suite and the live leave-one-domain-out gate (GOAL §5 L6 step 8, §11 M5, §15, invariant 16).

With ML plus engineering there are >= 2 domains, so the LODO gate is live: a campaign must hold one WHOLE domain out
of evolution, and a candidate is accepted only if it does not regress on it. Scores come from a fixed table (no
harness runs), so every decision is deterministic.
"""

from __future__ import annotations

import csv
import hashlib
import math
from collections import Counter
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from custom_harness.evolve.driver import load_suite
from custom_harness.evolve.evaluate import check_held_out
from custom_harness.research import eng_suite as E
from custom_harness.research.suite import check_split_isolation
from test_lofo import _run

KEY = hashlib.sha256(b"test-only engineering master key").digest()  # tests only; real suites use OS entropy


def _read(path: Path) -> list[list[str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    return E.build_engineering_suite(tmp_path_factory.mktemp("eng"), key=KEY, rows=40)


# ------------------------------------------------------------------ the engineering suite
def test_engineering_suite_has_families_a_held_out_family_isolation_and_sources(eng):
    doc = yaml.safe_load(eng.read_text(encoding="utf-8"))
    tasks = doc["tasks"]
    assert {t["domain"] for t in tasks} == {"engineering"}
    by_family: dict[str, set[str]] = {}
    for t in tasks:
        by_family.setdefault(t["family"], set()).add(t["split"])
    assert len(by_family) >= 2 and by_family["bridge"] == {"gate"}  # LOFO: a whole family never in evolve
    assert set(Counter(t["split"] for t in tasks)) == {"evolve", "gate", "sealed"}
    assert check_split_isolation(tasks) == []
    assert all(t["source"] == t["id"] for t in tasks)  # each task generated independently
    assert set(doc["sources"]) == {"engineering/ladder", "engineering/bridge", "engineering/amplifier"}
    assert all("exact MNA" in s for s in doc["sources"].values())
    gen = doc["generator"]
    assert gen["key_commitment"] == hashlib.sha256(KEY).hexdigest() and gen["key_source"] == "caller"
    assert KEY.hex() not in eng.read_text(encoding="utf-8")  # only the commitment is written
    assert len(load_suite(eng)) == len(tasks) == len(E.ENG_TASKS)
    assert check_held_out(load_suite(eng)) == (["bridge"], [])


def test_held_out_labels_stay_private(eng):
    for t in yaml.safe_load(eng.read_text(encoding="utf-8"))["tasks"]:
        d = Path(t["dir"])
        target = E.ENG_FAMILIES[t["family"]].target
        assert target not in _read(d / "public" / "test_X.csv")[0]
        assert _read(d / "private" / "test_y.csv")[0] == [target]


def _bridge_closed_form(vs, r1, r2, r3, r4, rg) -> Fraction:
    """Thevenin: Vth = Vs (R2/(R1+R2) - R4/(R3+R4)), Rth = R1||R2 + R3||R4, I = Vth / (Rth + Rg), in mA."""
    F = Fraction
    vth = F(vs) * (F(r2, r1 + r2) - F(r4, r3 + r4))
    rth = F(r1 * r2, r1 + r2) + F(r3 * r4, r3 + r4)
    return vth / (rth + rg) * 1000


def _ladder_closed_form(vs, r1, r2, r3, r4, r5, r6) -> Fraction:
    """Series/parallel reduction from the output end, then voltage division back to the output."""
    F = Fraction

    def par(a, b):
        return a * b / (a + b)
    zc = F(r5 + r6)  # seen from b towards the output
    zb = par(F(r4), zc)
    za = par(F(r2), r3 + zb)
    va = F(vs) * za / (r1 + za)
    vb = va * zb / (r3 + zb)
    return vb * r6 / (r5 + r6)


def _amplifier_closed_form(vin, rin, rf, a, rl) -> Fraction:
    """KCL at m with v(o) = -A v(m): v(m) (1/Rin + (1+A)/Rf) = vin/Rin; an ideal VCVS makes v(o) independent of RL."""
    F = Fraction
    vm = F(vin, rin) / (F(1, rin) + F(1 + a, rf))
    return -a * vm


CLOSED = {"bridge": _bridge_closed_form, "ladder": _ladder_closed_form, "amplifier": _amplifier_closed_form}


def test_labels_are_the_exact_circuit_value_of_the_printed_features_plus_small_noise(eng):
    """The clean target is computed exactly from the PRINTED features; checked here against independent closed forms
    (not the MNA stamps). The noise is about 1% (sd), so every label is within 6 sd of its clean value."""
    for t in yaml.safe_load(eng.read_text(encoding="utf-8"))["tasks"]:
        rows = _read(Path(t["dir"]) / "public" / "train.csv")
        for row in rows[1:]:
            *feats, y = row
            clean = CLOSED[t["family"]](*(int(v) for v in feats))
            sd = E.NOISE_REL * max(abs(float(clean)), E.ENG_FAMILIES[t["family"]].noise_floor)
            assert abs(float(y) - float(clean)) <= 6 * sd + 1e-6, (t["id"], row, float(clean))


def test_keyed_streams_reproduce_with_the_key_and_never_without_it():
    a = E.engineering_task("ladder", 1, 30, key=KEY)
    b = E.engineering_task("ladder", 1, 30, key=KEY)
    c = E.engineering_task("ladder", 1, 30, key=hashlib.sha256(b"another key").digest())
    fresh = E.engineering_task("ladder", 1, 30)  # OS entropy: nothing in the code reproduces it
    assert (a.train, a.test) == (b.train, b.test)
    assert (a.train, a.test) != (c.train, c.test) and (a.train, a.test) != (fresh.train, fresh.test)


def test_label_noise_has_its_own_stream():
    """COUPLED-PRNG lesson (GOAL §16.1): the noise comes from the `labels` stream, never from the features stream,
    so features (public) say nothing about the noise draws (private)."""
    from custom_harness.commitment import stream
    fam = E.ENG_FAMILIES["bridge"]
    one = E._rows(fam, stream(KEY, "features"), stream(KEY, "labels"), 20)
    other = E._rows(fam, stream(KEY, "features"), stream(KEY, "labels-2"), 20)
    assert [r[:-1] for r in one] == [r[:-1] for r in other]  # same features
    assert [r[-1] for r in one] != [r[-1] for r in other]  # only the noise moved


def test_a_singular_netlist_is_refused_by_the_truth_function():
    with pytest.raises(ValueError, match="not determined"):
        E.exact_value(["0", "a"], [E._v("V1", "a", "0", 1), E._v("V2", "a", "0", 1)],
                      {"quantity": "node_voltage", "node": "a", "unit": "V"})


def test_cross_domain_builder_refuses_an_unknown_domain(tmp_path):
    with pytest.raises(ValueError, match="unknown domain"):
        E.build_cross_domain_suite(tmp_path, heldout_domain="chemistry", key=KEY, rows=20)


# ------------------------------------------------------------------ the live leave-one-domain-out gate
@pytest.fixture(scope="module")
def cross(tmp_path_factory):
    root = tmp_path_factory.mktemp("cross")
    return {d: E.build_cross_domain_suite(root / d, heldout_domain=d, key=KEY, rows=24) for d in ("engineering", "ml")}


@pytest.mark.parametrize("held", ["engineering", "ml"])
def test_cross_domain_suite_holds_a_whole_domain_out_and_is_isolated(cross, held):
    tasks = load_suite(cross[held])
    families, domains = check_held_out(tasks)
    assert domains == [held] and {t.domain for t in tasks} == {"ml", "engineering"}
    assert {t.split for t in tasks if t.domain == held} == {"gate"}
    assert {t.split for t in tasks if t.domain != held} == {"evolve", "gate", "sealed"}
    assert set(families) >= ({"spirals"} if held == "engineering" else {"bridge"})
    doc = yaml.safe_load(cross[held].read_text(encoding="utf-8"))
    assert check_split_isolation(doc["tasks"]) == []
    assert {k.split("/")[0] for k in doc["sources"]} == {"ml", "engineering"}


def _gate_tasks(path: Path):
    return [t for t in load_suite(path) if t.split != "sealed"]  # the driver drops sealed before anything runs


def _scorer(domain_of: dict[str, str], held: str, held_gain: float):
    """Parent: 0 everywhere. Candidate: +1 (slightly varied) on the kept domain; `held_gain` on the held-out one."""
    def score(arm: str, task: str, seed: int) -> float:
        if arm == "parent":
            return 0.0
        jitter = 0.001 * ((sum(map(ord, task)) % 7) + 3 * (seed % 1000))
        return (held_gain if domain_of[task] == held else 1.0) + jitter
    return score


@pytest.mark.parametrize("held", ["engineering", "ml"])
def test_lodo_rejects_a_candidate_that_regresses_on_the_held_out_domain(store, tmp_path, cross, held):
    tasks = _gate_tasks(cross[held])
    domain_of = {t.task_id: t.domain for t in tasks}
    res = _run(store, tmp_path, tasks, _scorer(domain_of, held, -0.05))
    raced = [c for c in res.candidates if "lofo" in c]
    assert raced and raced[-1]["gate"]["lo"] > 0  # the pooled gate alone would have accepted
    assert not res.accepted and f"held-out domain {held}" in res.decision
    dom = raced[-1]["lofo"]["domains"][held]
    assert dom["passed"] is False and dom["mean_diff"] < 0
    rec = store.records("harness_edit")[-1]
    assert rec["lofo"]["domains"][held]["passed"] is False
    assert rec["lofo"]["ood_drop"][f"domain:{held}"] == pytest.approx(dom["mean_diff"])


@pytest.mark.parametrize("held", ["engineering", "ml"])
def test_lodo_accepts_a_candidate_that_holds_up_on_the_held_out_domain(store, tmp_path, cross, held):
    tasks = _gate_tasks(cross[held])
    domain_of = {t.task_id: t.domain for t in tasks}
    res = _run(store, tmp_path, tasks, _scorer(domain_of, held, 0.5))
    assert res.accepted, res.decision
    rec = store.records("harness_edit")[-1]
    assert rec["lofo"]["domains"][held]["passed"] is True
    assert math.isfinite(rec["lofo"]["ood_drop"][f"domain:{held}"])
