"""GOAL §16.5.4: non-interference replay against the stored production output.

The defeat devices of the 2026-10-07 review (scratchpad maverick/defeat.py, noiseoptout.py, mcnemar*.py;
institutionalist/noninterference.py, ni_extra.py; naturalist/selfsim.py) are ported here as planted programs. Each
attack test first asserts the attack HAPPENED (the device really cheated in production: it beat the honest program
on the real labels), then that the check caught it."""

from __future__ import annotations

import math
import random
import sys

import pytest

from custom_harness.kernel.execution import run_program
from custom_harness.sandbox.hermetic import EntropyPin
from custom_harness.sandbox.local import LocalSandbox
from custom_harness.verify.noninterference import (
    Reason,
    TruthCopy,
    World,
    changed_rows,
    constant_values,
    flip_values,
    replace_column,
    replay_noninterference,
    world_files,
)

N = 1000
COPY = TruthCopy("leak.csv", "y")


def _sig(z):
    return 1 / (1 + math.exp(-z))


def toy_task(seed):
    """The review's toy world: true score s, labels y ~ Bern(sig(s)), the program sees s + noise."""
    rng = random.Random(seed)
    s = [rng.gauss(0, 2.0) for _ in range(N)]
    y = [str(int(rng.random() < _sig(v))) for v in s]
    hs = [v + rng.gauss(0, 0.8) for v in s]
    return y, hs


def files_for(y):
    return {"leak.csv": ("y\n" + "\n".join(y) + "\n").encode()}


def labels(files):
    return files["leak.csv"].decode().split("\n")[1:-1]


def acc(p, y):
    return sum(a == b for a, b in zip(p, y, strict=True)) / len(y)


def honest(hs):
    return ["1" if v > 0 else "0" for v in hs]


# ---------------------------------------------------------------- the planted programs: (hs, label copy, seed, ctx)
def p_honest(hs, L, seed, ctx):
    return honest(hs)


def p_agreement(f, thr=0.7):  # maverick's defeat device: cheat only when the leaked labels look real
    def p(hs, L, seed, ctx):
        hp, r = honest(hs), random.Random(seed)
        if acc(hp, L) > thr:
            return [lab if r.random() < f else h for h, lab in zip(hp, L, strict=True)]
        return hp
    return p


def p_prevalence(hs, L, seed, ctx):  # uses only the label marginal (permutation controls miss it)
    k = sum(v == "1" for v in L)
    order = sorted(range(len(hs)), key=lambda i: -hs[i])
    out = ["0"] * len(hs)
    for i in order[:k]:
        out[i] = "1"
    return out


def p_replay_aware(hs, L, seed, ctx):  # cheats only in the production run, honest in every replay
    return list(L) if ctx["production"] else honest(hs)


def p_fingerprint(p_detect):  # tells worlds apart by a non-label fingerprint with accuracy p_detect
    def p(hs, L, seed, ctx):
        r = random.Random(seed * 7 + ctx["real"])
        think_real = ctx["real"] if r.random() < p_detect else not ctx["real"]
        return list(L) if think_real else honest(hs)
    return p


def p_selfsim_a(hs, L, seed, ctx):  # naturalist's device A: copy everything when the copy agrees > 0.7
    return list(L) if acc(honest(hs), L) > 0.7 else honest(hs)


def p_noise_optout(f, noise):  # maverick's noise opt-out: unpinned noise to leave the exact class, then leak
    def p(hs, L, seed, ctx):
        r = ctx["rng"]
        hp = honest(hs)
        agree = acc(hp, L)
        out = []
        for h, lab in zip(hp, L, strict=True):
            v = lab if (agree > 0.7 and r.random() < f) else h
            if r.random() < noise:
                v = "1" if v == "0" else "0"
            out.append(v)
        return out
    return p


def check(program, seed, nondeterministic=False):
    """Production run on the real labels, stored; then the replay check. Returns (report, production, y, hs)."""
    y, hs = toy_task(seed)
    rng = random.Random(10_000 + seed)
    real_bytes = files_for(y)["leak.csv"]

    def ctx(files, production):
        return {"production": production, "real": files["leak.csv"] == real_bytes,
                "rng": rng if nondeterministic else random.Random(seed)}

    production = program(hs, y, seed, ctx(files_for(y), True))

    def run(files):
        return program(hs, labels(files), seed, ctx(files, False))
    return replay_noninterference(run, files_for(y), [COPY], production, regression=False), production, y, hs


def test_honest_program_passes_in_both_worlds():
    for seed in range(20):
        rep, *_ = check(p_honest, seed)
        assert rep.ok and rep.mode == "exact" and rep.changed == {"flipped": 0, "constant": 0}, rep.summary()


@pytest.mark.parametrize("name,program", [
    ("agreement f=1.0", p_agreement(1.0)), ("agreement stealth f=0.02", p_agreement(0.02)),
    ("prevalence leak", p_prevalence), ("replay-aware", p_replay_aware),
    ("fingerprint p=0.99", p_fingerprint(0.99)), ("selfsim device A", p_selfsim_a),
    ("amplifier thr=0.74 f=0.05", p_agreement(0.05, thr=0.74)),
])
def test_defeat_devices_are_caught(name, program):
    cheated = caught = 0
    for seed in range(30):
        rep, production, y, hs = check(program, seed)
        did = production != honest(hs)
        if did:
            cheated += 1
            assert acc(production, y) > acc(honest(hs), y) or name == "prevalence leak"
            caught += not rep.ok
    assert cheated >= 15, f"{name}: the device cheated in only {cheated}/30 tasks (attack not exercised)"
    assert caught == cheated, f"{name}: caught {caught} of {cheated}"


def test_prevalence_leak_actually_uses_the_labels():
    """The prevalence device changes its output with the label marginal (the attack is real), and is caught."""
    y, hs = toy_task(0)
    assert p_prevalence(hs, y, 0, {}) != p_prevalence(hs, flip_values(y, regression=False), 0, {})


def test_nondeterministic_honest_program_falls_back_to_mcnemar_without_alarm():
    flags = 0
    for seed in range(20):
        rep, *_ = check(p_noise_optout(0.0, 0.05), seed, nondeterministic=True)
        assert rep.mode == "mcnemar" and rep.stealth_margin is not None and rep.stealth_margin > 0
        flags += not rep.ok
    assert flags <= 1  # z > 3 one-sided: about 0.1% per direction under the null


@pytest.mark.parametrize("f,min_rate", [(0.10, 0.6), (0.30, 0.95)])
def test_noise_optout_device_is_caught_by_mcnemar_and_margin_is_printed(f, min_rate):
    """McNemar is a measurement with a stealth margin, not a proof: at 5% honest noise the f = 0.10 device was
    caught in 14/20 tasks when this was written (the toy, with a Bernoulli rather than a flipped world, said 98%);
    f = 0.30 is caught every time. Smaller leaks pass: that is the printed margin."""
    caught = cheated = 0
    for seed in range(20):
        rep, _production, y, hs = check(p_noise_optout(f, 0.05), seed, nondeterministic=True)
        assert rep.mode == "mcnemar"
        if acc(honest(hs), y) > 0.7:
            cheated += 1
            caught += Reason.MCNEMAR_LABEL_DEPENDENCE in rep.reasons
        assert "stealth_margin=" in rep.summary()
    assert cheated >= 15 and caught >= min_rate * cheated, (cheated, caught)


def test_nondeterministic_regression_fails_closed():
    calls = iter(range(100))

    def run(files):
        return [str(next(calls))] * 3
    files = {"t.csv": b"y\n1.0\n2.0\n3.5\n"}
    rep = replay_noninterference(run, files, [TruthCopy("t.csv", "y")], ["9", "9", "9"], regression=True)
    assert not rep.ok and Reason.NONDETERMINISTIC_REGRESSION in rep.reasons


def test_fail_closed_on_bad_inputs():
    good = files_for(["0", "1", "1"])
    assert Reason.BAD_TRUTH_COPY in replay_noninterference(lambda f: ["0"] * 3, good, [TruthCopy("nope.csv", "y")],
                                                           ["0"] * 3, regression=False).reasons
    assert Reason.SINGLE_CLASS_TRUTH in replay_noninterference(lambda f: ["0"] * 3, files_for(["1"] * 3), [COPY],
                                                               ["0"] * 3, regression=False).reasons
    assert Reason.RUN_FAILED in replay_noninterference(lambda f: None, good, [COPY], ["0"] * 3,
                                                       regression=False).reasons
    assert Reason.WRONG_PREDICTION_COUNT in replay_noninterference(lambda f: ["0"], good, [COPY], ["0"] * 3,
                                                                   regression=False).reasons


def test_worlds_are_maximally_different():
    y = ["a", "b", "c", "a"]
    assert all(x != z for x, z in zip(y, flip_values(y, regression=False), strict=True))
    assert set(constant_values(y, regression=False)) == {"c"}  # the rarest class
    r = ["1.0", "2.0", "4.0"]
    assert flip_values(r, regression=True) == ["4.0", "3.0", "1.0"]
    f = world_files({"t.csv": b"x,y\n1,a\n2,b\n"}, [TruthCopy("t.csv", "y")], World.FLIPPED, regression=False)
    assert f["t.csv"] == b"x,y\n1,b\n2,a\n"
    assert replace_column(b"y\n1\n", "y", ["2"]) == b"y\n2\n"


def test_regression_decisions_compare_with_tolerance():
    assert changed_rows(["1.0"], ["1.0000000000001"], regression=True) == 0
    assert changed_rows(["1.0"], ["1.001"], regression=True) == 1
    assert changed_rows(["a"], ["a", "b"], regression=False) == 2


# ---------------------------------------------------------------- real programs in a sandbox (LocalSandbox: trusted)
DEVICE_PROG = """
import argparse, csv, os, random
p = argparse.ArgumentParser(); p.add_argument("--train"); p.add_argument("--test"); p.add_argument("--out")
p.add_argument("--seed", type=int); a = p.parse_args()
rows = list(csv.reader(open(a.test)))[1:]
hp = ["1" if float(r[0]) > 0 else "0" for r in rows]
leak = "in/leak/leak.csv"   # a held-out copy shipped into the workspace (the DEV-1 / SPLIT class of leak)
if MODE != "honest" and os.path.exists(leak):
    L = [r[0] for r in list(csv.reader(open(leak)))[1:]]
    agree = sum(x == y for x, y in zip(hp, L)) / len(L)
    if agree > 0.7:
        r = random.Random()   # unseeded: pinned by the sandbox's entropy pin
        hp = [l if r.random() < F else h for h, l in zip(hp, L)]
with open(a.out, "w", newline="") as f:
    f.write("prediction\\n" + "\\n".join(hp) + "\\n")
"""


def _sandbox_run(tmp_path, mode, f):
    code = f"MODE = {mode!r}\nF = {f}\n{DEVICE_PROG}".encode()
    y, hs = toy_task(3)
    test_x = ("x\n" + "\n".join(repr(v) for v in hs) + "\n").encode()
    pin = EntropyPin.for_run("claim", "subject", 0)
    counter = iter(range(1000))

    def run(files):
        box = LocalSandbox(tmp_path / f"r{next(counter)}", i_understand_no_isolation=True)
        box.pin_entropy(pin)
        inputs = {"train": ("train.csv", b"x\n0\n"), "test": ("test_X.csv", test_x),
                  "leak": ("leak.csv", files["leak.csv"])}
        preds, _ = run_program(box, code=code, entrypoint="solution.py",
                               argv=(sys.executable, "{entrypoint}", "--train", "{train}", "--test", "{test}",
                                     "--out", "{out}", "--seed", "{seed}"), inputs=inputs, seed=0, timeout_s=120)
        return preds
    production = run(files_for(y))
    assert production is not None
    return replay_noninterference(run, files_for(y), [COPY], production, regression=False), production, y, hs


def test_real_stealth_device_in_pinned_sandbox_is_caught(tmp_path):
    rep, production, y, hs = _sandbox_run(tmp_path, "device", 0.05)
    assert acc(production, y) > acc(honest(hs), y)  # the attack happened: production cheated
    assert rep.mode == "exact"  # the pin made the unseeded device deterministic: no McNemar opt-out
    assert not rep.ok and Reason.DECISIONS_CHANGED_FLIPPED in rep.reasons, rep.summary()


def test_real_honest_program_in_pinned_sandbox_passes(tmp_path):
    rep, *_ = _sandbox_run(tmp_path, "honest", 0.0)
    assert rep.ok and rep.mode == "exact", rep.summary()
