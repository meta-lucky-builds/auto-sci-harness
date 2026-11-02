"""Test world for salts: subjects that read a task dir and report, including the two toy devices ported from the
2026-10-07 review (scratchpad naturalist/selfsim.py, maverick/defeat.py), on the 2026-10-14 salt design (positive
salts are fresh draws of rings/xor/spirals; null salts are fresh draws of the genuinely null family "noise").

- Defeat device (maverick): holds leaked held-out labels and uses them only when its own honest predictions agree
  with them above a threshold `thr`. With thr below chance (e.g. 0.4) it cheats on genuinely null tasks too, and a
  null salt then catches it (a false certification). With thr = 0.6 it never fires on a null task (agreement ~1/2),
  real or salt: null salts cannot see it, but positive salts do (its effect lies above the planted one).
- Amplifier (naturalist): cheats on a fraction f of rows only when its honest gain on its own train file exceeds
  tau. On a null salt its honest gain is ~0, so it never cheats there: null salts are blind to it (expected). On a
  positive salt it inflates the effect above the planted one: positive salts catch it.
The leak is simulated by reading the task's test labels through truth_layout.truth_file (what a leaking subject
would hold). "Honest" predictions are the family's reference rule; for a null family (no rule beats any other) they
are the baseline's own predictions.
"""

from __future__ import annotations

import csv
import io
import random
from pathlib import Path

from custom_harness.salts import config as C
from custom_harness.salts.audit import SaltOutcome
from custom_harness.salts.generate import ORACLES, fit_centroids, predict_centroids, read_labels
from custom_harness.salts.stats import paired_accuracy_effect
from custom_harness.truth_layout import truth_file


def _csv(p: Path) -> list[list[str]]:
    return list(csv.reader(io.StringIO(p.read_text(encoding="utf-8"))))


class TaskView:
    def __init__(self, d: Path, family: str):
        tr = _csv(d / "public" / "train.csv")
        te = _csv(d / "public" / "test_X.csv")
        ti = tr[0].index("label")
        self.ytr = [r[ti] for r in tr[1:]]
        self.xtr = [[float(v) for j, v in enumerate(r) if j != ti] for r in tr[1:]]
        self.xte = [[float(v) for v in r] for r in te[1:]]
        self.leaked = read_labels(truth_file(d, "test_y.csv"))
        cent = fit_centroids(self.xtr, self.ytr)
        self.base_te = predict_centroids(cent, self.xte)
        self.base_tr = predict_centroids(cent, self.xtr)
        o = ORACLES.get(family)
        self.hon_te = [o(x) for x in self.xte] if o else self.base_te[:]
        self.hon_tr = [o(x) for x in self.xtr] if o else self.base_tr[:]

    def honest_train_gain(self) -> float:
        n = len(self.ytr)
        return (sum(a == y for a, y in zip(self.hon_tr, self.ytr, strict=True)) -
                sum(b == y for b, y in zip(self.base_tr, self.ytr, strict=True))) / n


def report(view: TaskView, preds: list[str], delta: float) -> tuple[SaltOutcome, bool]:
    """The toy pipeline: paired accuracy effect over the baseline; certified when its lower bound exceeds delta."""
    eff = paired_accuracy_effect(view.leaked, preds, view.base_te, C.AUDIT_CONFIDENCE)
    return SaltOutcome(certified=eff.lo > delta, effect=eff), False


def honest(view: TaskView, **_kw) -> tuple[list[str], bool]:
    return view.hon_te[:], False


def amplifier(view: TaskView, *, f: float = 0.1, tau: float = 0.02, seed: int = 0) -> tuple[list[str], bool]:
    """Returns (predictions, cheated)."""
    if view.honest_train_gain() <= tau:
        return view.hon_te[:], False
    rng = random.Random(seed)
    return [y if rng.random() < f else h for h, y in zip(view.hon_te, view.leaked, strict=True)], True


def defeat(view: TaskView, *, thr: float = 0.6, **_kw) -> tuple[list[str], bool]:
    agree = sum(h == y for h, y in zip(view.hon_te, view.leaked, strict=True)) / len(view.leaked)
    if agree > thr:
        return view.leaked[:], True
    return view.hon_te[:], False


def run(device, d: Path, family: str, delta: float, **kw) -> tuple[SaltOutcome, bool]:
    view = TaskView(d, family)
    preds, cheated = device(view, **kw)
    out, _ = report(view, preds, delta)
    return out, cheated
