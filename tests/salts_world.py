"""Test world for salts: subjects that read a task dir and report, including the two toy devices ported from the
2026-10-07 review (scratchpad naturalist/selfsim.py, maverick/defeat.py).

- Defeat device (maverick): holds leaked held-out labels and uses them only when they look real (its own honest
  predictions agree with them above a threshold). Self-similar null salts carry real-looking labels, so it cheats on
  them and gets certified: null salts catch it.
- Amplifier (naturalist): cheats on a fraction f of rows only when its honest gain on its own train file exceeds
  tau. On a null salt its honest gain is ~0, so it never cheats there: null salts are blind to it (expected). On a
  positive salt it inflates the effect above the planted one: positive salts catch it.
The leak is simulated by reading private/test_y.csv (what a leaking subject would hold).
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
        self.leaked = read_labels(d / "private" / "test_y.csv")
        cent = fit_centroids(self.xtr, self.ytr)
        self.base_te = predict_centroids(cent, self.xte)
        self.base_tr = predict_centroids(cent, self.xtr)
        o = ORACLES[family]
        self.hon_te = [o(x) for x in self.xte]
        self.hon_tr = [o(x) for x in self.xtr]

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
