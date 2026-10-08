"""F4 transductive check (GOAL §16.5 item 7): pure check, its adapter validation cases, and real programs run
through LocalSandbox (conftest sets CHH_ALLOW_UNISOLATED=1)."""

from __future__ import annotations

import ast
import itertools
import random
from pathlib import Path

import pytest

from custom_harness.kernel.adapters import validate
from custom_harness.kernel.execution import DEFAULT_ARGV
from custom_harness.sandbox.local import LocalSandbox
from custom_harness.verify import transductive as T
from custom_harness.verify.transductive_run import ProgramPredictor, _csv, check_program

# ---------------------------------------------------------------------------------------------------------- pure


def test_f4_validation_cases_all_pass():
    assert validate(T.F4_CHECKS) == []
    assert len(T.F4_CHECKS) == 13


def test_f4_validation_fails_when_a_fault_goes_unflagged(monkeypatch):
    """The validation cases bite: with the subset and batch-size-one probes switched off, the faults they plant are no
    longer flagged for the right reason and validation reports exactly those cases."""
    monkeypatch.setattr(T, "N_SUBSETS", 0)
    monkeypatch.setattr(T, "N_SINGLE_ROWS", 0)
    failed = validate(T.F4_CHECKS)
    assert "F4: per-batch normalisation is flagged on subsets" in failed
    assert "F4: a batch-rank predictor is flagged at batch size one" in failed
    assert "F4: a position-dependent predictor is flagged on permutation" not in failed


def test_transductive_module_is_stdlib_only():
    """The Kernel's ML adapter will import this module: no numpy, pydantic, sandbox or other project imports."""
    tree = ast.parse(Path(T.__file__).read_text(encoding="utf-8"))
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not any(isinstance(n, ast.ImportFrom) and n.level for n in ast.walk(tree))
    assert mods <= {"__future__", "math", "random", "collections", "dataclasses"}, mods


def test_honest_predictor_passes_with_exact_run_count():
    r = T.check_transductive(T._honest, T._ROWS, seed=3)
    assert r == T.F4Report(True, (), 0, 13)


def test_batch_norm_attack_changes_predictions_and_is_flagged():
    first_half = T._ROWS[:6]
    full = T._batch_norm(T._ROWS)[:6]
    assert T._batch_norm(first_half) != full  # the attack happened: the same rows get other labels in a sub-batch
    assert T._honest(first_half) == T._honest(T._ROWS)[:6]
    r = T.check_transductive(T._batch_norm, T._ROWS, seed=3)
    assert not r.ok and T.SUBSET in r.reasons and T.PERMUTATION not in r.reasons and r.rows_changed > 0


def test_permutation_is_unpermuted_before_comparing():
    """A per-row predictor sees the rows in another order; mapping back must restore agreement (no false alarm)."""
    seen: list[list[str]] = []

    def per_row(batch: list[list[str]]) -> list[str]:
        seen.append([r[0] for r in batch])
        return [r[0] for r in batch]  # the prediction IS the row id
    assert T.check_transductive(per_row, T._ROWS, seed=5).ok
    assert any(s != sorted(s, key=int) for s in seen[2:4])  # the permuted and reversed batches were really reordered
    assert any(len(s) == 1 for s in seen) and any(len(s) > len(T._ROWS) for s in seen)  # singles and mixing ran


def test_position_dependent_is_flagged_for_permutation():
    r = T.check_transductive(T._position, T._ROWS, seed=1)
    assert not r.ok and T.PERMUTATION in r.reasons


def test_regression_tolerance_and_unparseable():
    assert T.check_transductive(T._honest_reg, T._ROWS, seed=0, regression=True).ok
    r = T.check_transductive(T._centre_reg, T._ROWS, seed=0, regression=True)
    assert not r.ok and T.SUBSET in r.reasons
    # same predictor as classification compares strings: the round-off now counts (decisions vs float values)
    assert not T.check_transductive(T._honest_reg, T._ROWS, seed=0).ok
    bad = T.check_transductive(lambda b: ["nan"] * len(b), T._ROWS, seed=0, regression=True)
    assert bad == T.F4Report(False, (T.UNPARSEABLE,), 0, 1)
    calls = itertools.count()
    late = T.check_transductive(lambda b: ["1.0" if next(calls) == 0 else "x"] * len(b), T._ROWS, seed=0,
                                regression=True)
    assert not late.ok and late.reasons == (T.UNPARSEABLE,)


def test_wrong_count_in_a_later_probe_is_flagged():
    r = T.check_transductive(lambda b: ["0"] * min(len(b), 12), T._ROWS, seed=0)  # only the mixed batch is longer
    assert not r.ok and r.reasons == (T.WRONG_COUNT,) and r.runs == 13
    r = T.check_transductive(lambda b: ["0"] * min(len(b), 12), T._ROWS, seed=0, extra_rows=T._TRAIN_LIKE)
    assert r.reasons == (T.WRONG_COUNT,)


def test_failure_after_the_reference_is_flagged():
    calls = itertools.count()

    def fails_later(batch: list[list[str]]) -> list[str]:
        if next(calls) >= 3:
            raise T.PredictorFailed
        return T._honest(batch)
    r = T.check_transductive(fails_later, T._ROWS, seed=0)
    assert not r.ok and r.reasons == (T.PREDICTOR_FAILED,) and r.runs == 13


def test_small_batches_and_empty():
    with pytest.raises(ValueError):
        T.check_transductive(T._honest, [], seed=0)
    one = T.check_transductive(T._honest, [["1", "1"]], seed=0)
    assert one.ok and one.runs == 6  # 2 reference, permutation, reversal, 1 single row, mixing (no halves of 1 row)
    assert not T.check_transductive(T._batch_rank, T._ROWS[:2], seed=0).ok


def test_reasons_are_closed_and_report_carries_no_free_text():
    rng = random.Random(0)
    for predict in (T._honest, T._batch_norm, T._position, T._batch_rank, T._short, T._failing, T._flaky()):
        r = T.check_transductive(predict, T._ROWS, seed=rng.randrange(1000))
        assert set(r.reasons) <= T.REASONS and list(r.reasons) == sorted(set(r.reasons))
        assert set(vars(r)) == {"ok", "reasons", "rows_changed", "runs"}


# --------------------------------------------------------------------------------------------------- real programs

_COMMON = '''
import argparse, csv, math
ap = argparse.ArgumentParser()
for a in ("--train", "--test", "--out", "--seed"):
    ap.add_argument(a)
a = ap.parse_args()
with open(a.train, newline="") as f:
    tr = list(csv.DictReader(f))
with open(a.test, newline="") as f:
    te = list(csv.DictReader(f))
feats = ["f0", "f1"]
X = [[float(r[c]) for c in feats] for r in tr]
y = [r["y"] for r in tr]
T = [[float(r[c]) for c in feats] for r in te]
def stats(M):
    mu = [sum(c) / len(c) for c in zip(*M)]
    sd = [math.sqrt(sum((v - m) ** 2 for v in c) / len(c)) or 1.0 for c, m in zip(zip(*M), mu)]
    return mu, sd
def std(M, mu, sd):
    return [[(v - m) / s for v, m, s in zip(r, mu, sd)] for r in M]
'''
_CENTROID = '''
def centroid_predict(X, y, T):
    cents = {}
    for lab in sorted(set(y)):
        pts = [x for x, t in zip(X, y) if t == lab]
        cents[lab] = [sum(c) / len(pts) for c in zip(*pts)]
    return [min(cents, key=lambda k: sum((a - b) ** 2 for a, b in zip(r, cents[k]))) for r in T]
'''
_WRITE = '''
with open(a.out, "w", newline="") as f:
    w = csv.writer(f, lineterminator="\\n")
    w.writerow(["prediction"])
    w.writerows([[p] for p in preds])
'''
HONEST = _COMMON + _CENTROID + '''
mu, sd = stats(X)
preds = centroid_predict(std(X, mu, sd), y, std(T, mu, sd))
''' + _WRITE
BATCH_NORM = _COMMON + _CENTROID + '''
mu, sd = stats(X)
tmu, tsd = stats(T)  # the leak: test features standardised with the TEST batch's own mean/std
preds = centroid_predict(std(X, mu, sd), y, std(T, tmu, tsd))
''' + _WRITE
POSITION = _COMMON + _CENTROID + '''
mu, sd = stats(X)
preds = centroid_predict(std(X, mu, sd), y, std(T, mu, sd))
preds = [p if i < len(preds) // 2 else "1" for i, p in enumerate(preds)]  # rows in the second half -> class 1
''' + _WRITE
CRASH = "import sys\nsys.exit(3)\n"


def _data(shift: float) -> tuple[bytes, bytes, list[str]]:
    """Train: class 0 around (0,0), class 1 around (3,3). Test: the same classes shifted by `shift` (covariate shift),
    grouped by class (8 of class 0, then 8 of class 1)."""
    rng = random.Random(7)
    tr = [[f"{rng.gauss(3 * k, 0.8):.4f}", f"{rng.gauss(3 * k, 0.8):.4f}", str(k)] for k in (0, 1) for _ in range(10)]
    te = [[f"{rng.gauss(3 * k + shift, 0.8):.4f}", f"{rng.gauss(3 * k + shift, 0.8):.4f}"] for k in (0, 1)
          for _ in range(8)]
    labels = ["0"] * 8 + ["1"] * 8
    return _csv(["f0", "f1", "y"], tr), _csv(["f0", "f1"], te), labels


# test-only speed-ups (this laptop starts a full venv interpreter in ~2 s): skip site-packages (the programs are
# stdlib-only), and fewer probes than the defaults; the pure tests above cover the default probe set
FAST_ARGV = ("{python}", "-S", *DEFAULT_ARGV[1:])


@pytest.fixture
def factory(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "N_SUBSETS", 2)  # the contiguous halves only
    monkeypatch.setattr(T, "N_SINGLE_ROWS", 1)
    n = itertools.count()
    return lambda name: LocalSandbox(tmp_path / f"{name}-{next(n)}", i_understand_no_isolation=True)


FAST_RUNS = 8  # 2 reference + permutation + reversal + 2 halves + 1 single row + mixing


def _predictor(factory, code: str, train: bytes) -> ProgramPredictor:
    return ProgramPredictor(factory, code=code.encode(), entrypoint="solution.py", train_csv=train,
                            test_header=["f0", "f1"], seed=0, timeout_s=60, argv=FAST_ARGV)


def _recording(predict):
    log: list[tuple[list[list[str]], list[str]]] = []

    def f(batch: list[list[str]]) -> list[str]:
        out = predict(batch)
        log.append((batch, out))
        return out
    return f, log


def _rows(test_x: bytes) -> list[list[str]]:
    return [r.split(",") for r in test_x.decode().splitlines()[1:]]


def _accuracy(preds: list[str], labels: list[str]) -> float:
    return sum(p == t for p, t in zip(preds, labels, strict=True)) / len(labels)


def test_program_honest_centroid_passes(factory):
    train, test_x, _ = _data(shift=0.0)
    r = check_program(factory, code=HONEST.encode(), entrypoint="solution.py", train_csv=train, test_x_csv=test_x,
                      seed=0, timeout_s=60, target_column="y", argv=FAST_ARGV)
    assert r == T.F4Report(True, (), 0, FAST_RUNS)


def test_program_batch_norm_gains_from_the_test_batch_and_is_flagged(factory):
    train, test_x, labels = _data(shift=2.0)
    rows = _rows(test_x)
    leaky, log = _recording(_predictor(factory, BATCH_NORM, train))
    r = T.check_transductive(leaky, rows, seed=0)
    assert not r.ok and T.SUBSET in r.reasons and T.PERMUTATION not in r.reasons and r.rows_changed > 0
    # the attack happened: batch statistics give a fake gain on the shifted test set ...
    full = log[0][1]
    assert _accuracy(full, labels) > _accuracy(_predictor(factory, HONEST, train)(rows), labels) + 0.2
    # ... and the same rows got other labels in the sub-batch holding one class only
    assert [out for batch, out in log if batch == rows[:8]] != [full[:8]]


def test_program_position_dependent_is_flagged(factory):
    train, test_x, _ = _data(shift=0.0)
    rows = _rows(test_x)
    pos, log = _recording(_predictor(factory, POSITION, train))
    r = T.check_transductive(pos, rows, seed=0)
    assert not r.ok and T.PERMUTATION in r.reasons
    reversed_out = [out for batch, out in log if batch == rows[::-1]]
    assert len(reversed_out) == 1 and reversed_out[0][::-1] != log[0][1]  # the attack happened: order changed decisions


def test_program_crash_is_predictor_failed_not_ok(factory):
    train, test_x, _ = _data(shift=0.0)
    r = check_program(factory, code=CRASH.encode(), entrypoint="solution.py", train_csv=train, test_x_csv=test_x,
                      seed=0, timeout_s=60, argv=FAST_ARGV)
    assert r == T.F4Report(False, (T.PREDICTOR_FAILED,), 0, 1)


def test_check_program_refuses_a_missing_target_column(factory):
    train, test_x, _ = _data(shift=0.0)
    with pytest.raises(ValueError):
        check_program(factory, code=HONEST.encode(), entrypoint="solution.py", train_csv=train, test_x_csv=test_x,
                      seed=0, timeout_s=60, target_column="label")
