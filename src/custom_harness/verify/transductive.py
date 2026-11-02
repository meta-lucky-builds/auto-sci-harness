"""F4 transductive check (GOAL §16.5 item 7): a prediction for a test row must not depend on the OTHER test rows.

The observed leak (Jr. AI Scientist): per-batch normalisation on test batches that held only one kind of sample,
which gave fake gains. The check is metamorphic. `predict` maps a batch of test rows (features only) to one
prediction per row, in order. The full batch is the reference; then the same rows are re-predicted in a seeded
permutation (predictions un-permuted), in reversed order, in halves (contiguous, then random), one row at a time
(k sampled rows), and mixed with other rows (`extra_rows`, e.g. train features without the target; by default
duplicates of a random half of the batch). Every probe must give each row the same DECISION as the reference:
classification labels exactly,
regression values as floats within a tolerance. The full batch is predicted twice first, so a nondeterministic
predictor is reported as such instead of as transductive.

PURE and stdlib-only: the Kernel's ML adapter imports this module (its source is part of the adapter hash), and
`F4_CHECKS` is in the adapter validation format (name, zero-arg callable -> bool). Reasons are a closed set of codes
(GOAL §16.5 item 8); no free text from the predictor reaches the report.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

Rows = list[list[str]]
Predictor = Callable[[Rows], list[str]]

# --- configuration --------------------------------------------------------------------------------------------------
# UNCALIBRATED (owner decision, GOAL §13.8): probe counts and the regression tolerance. Conservative defaults: tight
# tolerance (only float round-off passes), every probe kind on by default. Runs per check with these defaults:
# 2 reference + 1 permutation + 1 reversal + N_SUBSETS + min(N_SINGLE_ROWS, n) + 1 mixing = 13 for n >= 4.
N_SINGLE_ROWS = 4  # UNCALIBRATED (owner decision, GOAL §13.8): rows re-predicted alone (batch size one)
N_SUBSETS = 4  # UNCALIBRATED (owner decision, GOAL §13.8): halves, in complementary pairs (every row covered); the
# first pair is the two CONTIGUOUS halves of the given order (test sets grouped by source, e.g. ID then OOD: the
# observed leak), later pairs are random halves
REGRESSION_REL_TOL = 1e-6  # UNCALIBRATED (owner decision, GOAL §13.8)
REGRESSION_ABS_TOL = 1e-9  # UNCALIBRATED (owner decision, GOAL §13.8)

# --- closed reason codes --------------------------------------------------------------------------------------------
WRONG_COUNT = "wrong_prediction_count"
NONDETERMINISTIC = "nondeterministic_predictions"
PERMUTATION = "permutation_changed_predictions"
SUBSET = "subset_changed_predictions"
BATCH_ONE = "batch_size_one_changed_predictions"
MIXING = "mixing_changed_predictions"
UNPARSEABLE = "unparseable_regression_prediction"
PREDICTOR_FAILED = "predictor_failed"
REASONS = frozenset({WRONG_COUNT, NONDETERMINISTIC, PERMUTATION, SUBSET, BATCH_ONE, MIXING, UNPARSEABLE,
                     PREDICTOR_FAILED})


class PredictorFailed(Exception):
    """Raised by a predictor whose run failed (e.g. a program that crashed or wrote no predictions). Carries no
    message into the report: the check records the closed code `predictor_failed`."""


@dataclass(frozen=True)
class F4Report:
    ok: bool
    reasons: tuple[str, ...]  # sorted, each in REASONS
    rows_changed: int  # distinct test rows whose decision changed in at least one probe
    runs: int  # predictor calls made


def _same(a: str, b: str, regression: bool) -> bool | None:
    """Same decision? None when a regression value is not a finite float."""
    if not regression:
        return a == b
    try:
        x, y = float(a), float(b)
    except ValueError:
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return abs(x - y) <= max(REGRESSION_ABS_TOL, REGRESSION_REL_TOL * max(abs(x), abs(y)))


def check_transductive(predict: Predictor, rows: Sequence[Sequence[str]], *, seed: int, regression: bool = False,
                       extra_rows: Sequence[Sequence[str]] | None = None) -> F4Report:
    if not rows:
        raise ValueError("the F4 check needs at least one test row")
    n = len(rows)
    rng = random.Random(seed)
    reasons: set[str] = set()
    changed: set[int] = set()
    runs = 0

    def run(batch: Sequence[Sequence[str]]) -> list[str] | None:
        """One predictor call; None (with a reason) on a failed run or a wrong prediction count."""
        nonlocal runs
        runs += 1
        try:
            out = predict([list(r) for r in batch])
        except PredictorFailed:
            reasons.add(PREDICTOR_FAILED)
            return None
        if len(out) != len(batch):
            reasons.add(WRONG_COUNT)
            return None
        return [str(v) for v in out]

    def call(idx: Sequence[int]) -> list[str] | None:
        return run([rows[i] for i in idx])

    ref = call(range(n))
    if ref is None:
        return F4Report(False, tuple(sorted(reasons)), 0, runs)
    if regression and any(_same(v, v, True) is None for v in ref):
        return F4Report(False, (UNPARSEABLE,), 0, runs)

    def compare(reason: str, idx: Sequence[int], out: list[str] | None) -> None:
        """out[j] is the prediction for rows[idx[j]]."""
        if out is None:
            return
        for j, i in enumerate(idx):
            same = _same(out[j], ref[i], regression)
            if same is None:
                reasons.add(UNPARSEABLE)
            elif not same:
                reasons.add(reason)
                changed.add(i)

    everything = list(range(n))
    compare(NONDETERMINISTIC, everything, call(everything))
    perm = everything[:]
    rng.shuffle(perm)
    compare(PERMUTATION, perm, call(perm))
    rev = everything[::-1]
    compare(PERMUTATION, rev, call(rev))
    if n >= 2:
        half = n // 2
        for s in range(N_SUBSETS):
            if s % 2 == 0:
                split = everything[:]
                if s > 0:
                    rng.shuffle(split)
            part = split[:half] if s % 2 == 0 else split[half:]
            compare(SUBSET, part, call(part))
    for i in sorted(rng.sample(everything, min(N_SINGLE_ROWS, n))):
        compare(BATCH_ONE, [i], call([i]))
    # mixing: the batch shuffled together with other rows (default: duplicates of a random half of the batch, each
    # compared with its source row); foreign extra rows (source -1) are not compared
    if extra_rows is None:
        dup = sorted(rng.sample(everything, max(1, n // 2)))
        pool, source = [*rows, *(rows[i] for i in dup)], everything + dup
    else:
        pool, source = [*rows, *extra_rows], everything + [-1] * len(extra_rows)
    order = list(range(len(pool)))
    rng.shuffle(order)
    out = run([pool[k] for k in order])
    if out is not None:
        pos = [p for p, k in enumerate(order) if source[k] >= 0]
        compare(MIXING, [source[order[p]] for p in pos], [out[p] for p in pos])
    return F4Report(not reasons, tuple(sorted(reasons)), len(changed), runs)


# --- adapter validation cases (fault injection) ---------------------------------------------------------------------
# 12 rows, two features; a planted shift: the first feature runs 0..11, so any half has a different mean.
_ROWS = [[str(i), str((i * 7) % 5 - 2)] for i in range(12)]
_TRAIN_LIKE = [[str(20 + i), "0"] for i in range(6)]  # extra rows from elsewhere (shifts every batch statistic)


def _x(batch: Rows) -> list[float]:
    return [float(r[0]) for r in batch]


def _honest(batch: Rows) -> list[str]:
    return ["1" if float(r[0]) + float(r[1]) > 5.5 else "0" for r in batch]


def _batch_norm(batch: Rows) -> list[str]:
    """Standardises with the BATCH's mean/std (the observed leak)."""
    xs = _x(batch)
    mu = sum(xs) / len(xs)
    sd = math.sqrt(sum((v - mu) ** 2 for v in xs) / len(xs)) or 1.0
    return ["1" if (v - mu) / sd > 0 else "0" for v in xs]


def _position(batch: Rows) -> list[str]:
    return [str(j % 2) for j in range(len(batch))]


def _batch_rank(batch: Rows) -> list[str]:
    """Top half of the batch (by the first feature) -> class 1."""
    xs = _x(batch)
    cut = sorted(xs)[len(xs) // 2]
    return ["1" if v >= cut else "0" for v in xs]


def _honest_reg(batch: Rows) -> list[str]:
    """Per-row regression, plus batch-size-dependent noise far below the tolerance (float round-off)."""
    return [repr(2.0 * float(r[0]) + 1.0 + 1e-13 * len(batch)) for r in batch]


def _centre_reg(batch: Rows) -> list[str]:
    xs = _x(batch)
    mu = sum(xs) / len(xs)
    return [repr(v - mu) for v in xs]


def _short(batch: Rows) -> list[str]:
    return _honest(batch)[:-1] if len(batch) > 1 else _honest(batch)


def _failing(batch: Rows) -> list[str]:
    raise PredictorFailed


def _flaky() -> Predictor:
    calls = [0]

    def predict(batch: Rows) -> list[str]:
        calls[0] += 1
        return [str(calls[0] % 2)] * len(batch)
    return predict


def _passes(predict: Predictor, regression: bool = False, extra: Rows | None = None) -> bool:
    r = check_transductive(predict, _ROWS, seed=0, regression=regression, extra_rows=extra)
    return r.ok and r.reasons == () and r.rows_changed == 0 and r.runs == 13


def _flags(predict: Predictor, why: str, regression: bool = False, extra: Rows | None = None) -> bool:
    """The planted fault is flagged FOR THE RIGHT REASON (flagged for another reason fails validation)."""
    r = check_transductive(predict, _ROWS, seed=0, regression=regression, extra_rows=extra)
    return not r.ok and why in r.reasons and set(r.reasons) <= REASONS


F4_CHECKS: tuple[tuple[str, Callable[[], bool]], ...] = (
    ("F4: an honest per-row classifier passes", lambda: _passes(_honest)),
    ("F4: an honest per-row classifier passes when mixed with other rows", lambda: _passes(_honest, extra=_TRAIN_LIKE)),
    ("F4: an honest regressor with sub-tolerance round-off passes", lambda: _passes(_honest_reg, regression=True)),
    ("F4: per-batch normalisation is flagged on subsets", lambda: _flags(_batch_norm, SUBSET)),
    ("F4: per-batch normalisation is flagged when mixed with other rows",
     lambda: _flags(_batch_norm, MIXING, extra=_TRAIN_LIKE)),
    ("F4: a position-dependent predictor is flagged on permutation", lambda: _flags(_position, PERMUTATION)),
    ("F4: a batch-rank predictor is flagged at batch size one", lambda: _flags(_batch_rank, BATCH_ONE)),
    ("F4: a batch-rank predictor is flagged on subsets", lambda: _flags(_batch_rank, SUBSET)),
    ("F4: a batch-mean-centred regressor is flagged on subsets", lambda: _flags(_centre_reg, SUBSET, regression=True)),
    ("F4: a batch-mean-centred regressor is flagged on mixing",
     lambda: _flags(_centre_reg, MIXING, regression=True, extra=_TRAIN_LIKE)),
    ("F4: a wrong prediction count is flagged", lambda: _flags(_short, WRONG_COUNT)),
    ("F4: a failed predictor run is flagged", lambda: _flags(_failing, PREDICTOR_FAILED)),
    ("F4: a nondeterministic predictor is reported as nondeterministic",
     lambda: _flags(_flaky(), NONDETERMINISTIC)),
)
