"""The strawman check (GOAL 18.6, found by the Court health run): a claim to beat a comparator says nothing if the
comparator is broken. Without this check a planted baseline that can never be right was CERTIFIED-VALIDATED.

Before any held-out label is read, the frozen comparator runs under its own contract on a split of the PUBLIC training
file, and is scored against the most trivial predictor there: the most common training label, or the training mean for
an error metric. A comparator clearly worse than that is refused as COULD NOT RUN, and the target is kept. No held-out
data is used and the check can only deny (invariant 21).

What it does not catch: a comparator above the trivial predictor but far below the honest state of the art. Choosing a
fair comparator stays a human judgement, and the claim's text names it.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter
from typing import Any

from ..kernel import DEFAULT_ARGV, run_program
from ..verify import metrics as M

MARGIN = 0.05  # UNCALIBRATED (owner decision, GOAL 13.8): absolute for accuracy and f1, relative for error metrics


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode()


def public_split(train: bytes) -> tuple[bytes, bytes, list[str], list[str]]:
    """(fit file, test features, test labels, fit labels): every third row by a hash of its position."""
    table = list(csv.reader(io.StringIO(train.decode("utf-8"))))
    head, rows = table[0], [r for r in table[1:] if r]
    li = head.index("label")
    fit: list[list[str]] = []
    held: list[list[str]] = []
    for i, r in enumerate(rows):
        (held if hashlib.sha256(str(i).encode()).digest()[0] % 3 == 0 else fit).append(r)
    features = [[v for j, v in enumerate(r) if j != li] for r in [head, *held]]
    return _csv([head, *fit]), _csv(features), [r[li] for r in held], [r[li] for r in fit]


def trivial_predictions(metric: str, fit_labels: list[str], n: int) -> list[str]:
    if metric in M.CLASSIFICATION:
        return [Counter(sorted(fit_labels)).most_common(1)[0][0]] * n
    return [str(sum(float(v) for v in fit_labels) / len(fit_labels))] * n


def worse_than_trivial(metric: str, score: float, trivial: float) -> bool:
    if M.HIGHER_IS_BETTER[metric]:
        return score < trivial - MARGIN
    return score > trivial * (1 + MARGIN)


def comparator_problem(code: bytes, metric: str, train: bytes, sandbox: Any, seed: int, timeout_s: float) -> str | None:
    """Why this comparator is a strawman, or None. A comparator that cannot run here is left to the real run, which
    reports it with the same contract."""
    fit, test_x, y, fit_labels = public_split(train)
    preds, _ = run_program(sandbox, code=code, entrypoint="solution.py", argv=DEFAULT_ARGV,
                           inputs={"train": ("train.csv", fit), "test": ("test_X.csv", test_x)}, seed=seed,
                           timeout_s=timeout_s)
    if preds is None or len(preds) != len(y) or M.predictions_problem(metric, preds):
        return None
    score = M.score(metric, y, preds)
    trivial = M.score(metric, y, trivial_predictions(metric, fit_labels, len(y)))
    if worse_than_trivial(metric, score, trivial):
        return (f"the comparator is a strawman: on the public training data it scores {score:.4f} on {metric}, worse "
                f"than always predicting the trivial answer ({trivial:.4f})")
    return None
