"""The strawman check (GOAL 18.6, found by the Court health run): a claim to beat a comparator says nothing if the
comparator is broken. Without this check a planted baseline that can never be right was CERTIFIED-VALIDATED.

Before any held-out label is read, the frozen comparator runs under its own contract on a split of the PUBLIC training
file, and is scored against the most trivial predictor there: the most common training label, or the training mean for
an error metric. A comparator clearly worse than that is refused as COULD NOT RUN, and the target is kept. No held-out
data is used and the check can only deny (invariant 21).

A comparator can pass that and still be sabotaged: it can look at the size of its input and fail only on the real test.
So its held-out score, which the Kernel already read, is held against the same two public numbers. A collapse denies the
claim its verdict and VALIDATED. No extra label is read, no number enters the reason, and on the future route only the
trivial bound applies, since future data may honestly differ from the public file.

What it does not catch: a comparator above the trivial predictor but far below the honest state of the art, and one
sabotaged by less than the allowed drop. Choosing a fair comparator stays a human judgement, and the claim's text names
it.
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
MAX_DROP = 0.15  # UNCALIBRATED: held-out against public score, absolute for accuracy and f1
MAX_RISE = 0.30  # UNCALIBRATED: held-out against public score, relative for error metrics
COLLAPSED = ("the comparator collapsed on the held-out data: it fell far below its public score or below the trivial "
             "answer, so beating it says nothing about the method")


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


def comparator_reference(code: bytes, metric: str, train: bytes, sandbox: Any, seed: int, timeout_s: float
                         ) -> tuple[float | None, float | None]:
    """(the comparator's public score, the trivial predictor's public score). None where it could not be measured: a
    comparator that cannot run here is left to the real run, which reports it with the same contract."""
    fit, test_x, y, fit_labels = public_split(train)
    if not y or not fit_labels:
        return None, None
    preds, _ = run_program(sandbox, code=code, entrypoint="solution.py", argv=DEFAULT_ARGV,
                           inputs={"train": ("train.csv", fit), "test": ("test_X.csv", test_x)}, seed=seed,
                           timeout_s=timeout_s)
    try:
        trivial = M.score(metric, y, trivial_predictions(metric, fit_labels, len(y)))
    except ValueError:
        return None, None
    if preds is None or len(preds) != len(y) or M.predictions_problem(metric, preds):
        return None, trivial
    return M.score(metric, y, preds), trivial


def strawman_reason(metric: str, public: float | None, trivial: float | None) -> str | None:
    """Why this comparator is a strawman on the public data, or None."""
    if public is None or trivial is None or not worse_than_trivial(metric, public, trivial):
        return None
    return (f"the comparator is a strawman: on the public training data it scores {public:.4f} on {metric}, worse than "
            f"always predicting the trivial answer ({trivial:.4f})")


def comparator_problem(code: bytes, metric: str, train: bytes, sandbox: Any, seed: int, timeout_s: float) -> str | None:
    return strawman_reason(metric, *comparator_reference(code, metric, train, sandbox, seed, timeout_s))


def held_out_problem(metric: str, held_out: float, public: float | None, trivial: float | None, *,
                     against_public: bool) -> str | None:
    """COLLAPSED if the comparator's mean held-out score undercuts its public reference, else None. Deny-only."""
    if trivial is not None and worse_than_trivial(metric, held_out, trivial):
        return COLLAPSED
    if not against_public or public is None:
        return None
    if M.HIGHER_IS_BETTER[metric]:
        return COLLAPSED if held_out < public - MAX_DROP else None
    return COLLAPSED if held_out > public * (1 + MAX_RISE) else None
