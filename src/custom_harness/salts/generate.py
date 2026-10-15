"""Salt tasks (GOAL §16.9): positive salts with a known planted effect, null salts with none.

Every salt is a task of the SAME generator and the SAME files as a real task (research/suite.py: `synthetic_task`
for features, `real_task` for real data, `write_task` for the layout), so it differs from a real task only in its
labels. Supported: the accuracy-scored families (synthetic rings, xor, spirals; real-data iris, wine, digits).
Other metrics are refused: for F1, RMSE and MAE the planted effect is not pinned down by the construction below.

Construction (synthetic families). o(x) is the family's reference (Bayes) rule, b(x) the task's own baseline (the
nearest-centroid program shipped in baseline.py, fitted on the train file). On rows where o and b disagree, the label
becomes o(x) with probability rho and b(x) otherwise; every other row keeps the generator's label. o stays the best
rule (rho >= 1/2), so the effect a predictor can reach is acc(o) - acc(b):
  - positive salt: rho is chosen so the realised effect acc(o) - acc(b') on the written test file, with b' the
    baseline refitted on the salt's own train file, is POSITIVE_EFFECT_MULTIPLIER * delta (within tolerance). That
    realised value is the planted effect the salt is scored against;
  - null salt: rho = 1/2, so the baseline is a Bayes rule and no predictor beats it in expectation. The planting is
    iterated until the refitted baseline agrees with the one the labels were built from; what disagreement is left
    gives an exact bound on the largest true effect (`residual_bound`), and a salt whose bound is not small is
    refused.
Plasmode nulls (real data): labels regenerated from the fitted baseline read as a probability model,
P(y = c | x) proportional to exp(-|x - centroid_c|^2 / T), with T set so the baseline's expected train accuracy
equals its accuracy on the real labels. Its Bayes rule is the baseline itself; same fixed point and exact bound.

Known tells (measured by the detectability exam, never assumed away): a positive salt plants a SMALL effect where the
real tasks of a family have a large one (rows near the baseline's decision boundary carry coin-flip labels), and a
plasmode keeps the real features with new labels (a spotter who knows the dataset can see that).
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from ..commitment import KeyedStream, stream, subkey
from ..research import suite as S
from . import config as C


class SaltError(ValueError):
    pass


# ---------------------------------------------------------------- reference rules o(x) per synthetic family
def _rings_oracle(x: Sequence[float]) -> str:
    r = math.hypot(x[0], x[1])  # class radii 1.0, 2.2, 3.4 with equal noise: Bayes thresholds at the midpoints
    return "0" if r < 1.6 else ("1" if r < 2.8 else "2")


def _xor_oracle(x: Sequence[float]) -> str:
    return str(int((x[0] > 0) != (x[1] > 0)))


def _spirals_oracle(x: Sequence[float]) -> str:
    t = math.hypot(x[0], x[1]) * 3 * math.pi / 2  # radius -> spiral parameter; class 1 is class 0 turned by pi
    gap = (math.atan2(x[1], x[0]) - t) % (2 * math.pi)
    return "0" if min(gap, 2 * math.pi - gap) < math.pi / 2 else "1"


ORACLES: dict[str, Callable[[Sequence[float]], str]] = {"rings": _rings_oracle, "xor": _xor_oracle,
                                                        "spirals": _spirals_oracle}
POSITIVE_FAMILIES = frozenset(ORACLES)  # synthetic families that can host a positive (or null) salt
PLASMODE_DATASETS = frozenset(n for n, v in S.REAL.items() if v[1] == "accuracy")  # real data for plasmode nulls


# ---------------------------------------------------------------- the baseline, exactly as baseline.py runs it
def _nums(row: Sequence[str], ti: int) -> list[float]:
    out = []
    for j, v in enumerate(row):
        if j == ti:
            continue
        try:
            out.append(float(v))
        except ValueError:
            pass
    return out


def fit_centroids(xs: Sequence[Sequence[float]], ys: Sequence[str]) -> dict[str, list[float]]:
    """suite.CENTROID's fit: the mean feature vector of each label."""
    cent: dict[str, list[float]] = {}
    cnt: dict[str, int] = {}
    for x, y in zip(xs, ys, strict=True):
        cent[y] = [s + v for s, v in zip(cent.get(y, [0.0] * len(x)), x, strict=True)]
        cnt[y] = cnt.get(y, 0) + 1
    return {y: [s / cnt[y] for s in v] for y, v in cent.items()}


def _d2(x: Sequence[float], c: Sequence[float]) -> float:
    return sum((p - q) ** 2 for p, q in zip(x, c, strict=True))


def predict_centroids(cent: dict[str, list[float]], xs: Sequence[Sequence[float]]) -> list[str]:
    """suite.CENTROID's prediction: nearest centroid, ties to the first label in sorted order."""
    labels = sorted(cent)
    cs = [cent[y] for y in labels]
    out = []
    for x in xs:
        ds = [math.dist(x, c) for c in cs]  # fast ranking; near-ties are re-decided with baseline.py's own arithmetic
        m = min(ds)
        close = [i for i, v in enumerate(ds) if v <= m * (1 + 1e-9) + 1e-300]
        out.append(labels[close[0]] if len(close) == 1 else min((labels[i] for i in close),
                                                                 key=lambda y: _d2(x, cent[y])))
    return out


# ---------------------------------------------------------------- the generated salt
@dataclass(frozen=True)
class GeneratedSalt:
    task_id: str
    kind: str  # "positive" | "null"
    family: str  # synthetic family, or "real" for a plasmode
    source: str  # "synthetic" | "plasmode"
    metric: str
    target_effect: float  # what was asked for (2 delta for a positive salt, 0 for a null)
    planted_effect: float  # the realised effect of the reference rule over the refitted baseline on the test file
    residual_bound: float | None  # null salts: the largest true effect any predictor can have (exact, expected)
    files: dict[str, str]  # relative path -> SHA-256 of every file written (canary excluded: fresh per task)
    task_dir: Path


def _written_test(spec: S.TaskSpecFiles) -> list[list[str]]:
    """The test rows write_task writes: those whose features also occur in train are dropped."""
    ti = spec.header.index(spec.target)

    def x(r: list[str]) -> tuple[str, ...]:
        return tuple(v for j, v in enumerate(r) if j != ti)

    seen = {x(r) for r in spec.train}
    return [r for r in spec.test if x(r) not in seen]


def _relabel(rows: list[list[str]], ti: int, labels: Sequence[str]) -> list[list[str]]:
    return [[*r[:ti], y, *r[ti + 1:]] for r, y in zip(rows, labels, strict=True)]


def _file_hashes(d: Path) -> dict[str, str]:
    """Every file of the salt task (its truth under `private/<name>` wherever truth_layout keeps it), canary
    excluded (fresh per task)."""
    from ..truth_layout import LEGACY_DIRNAME, truth_files

    out = {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
           for p in sorted(d.rglob("*")) if p.is_file() and p.name != "canary.txt"}
    for rel, p in truth_files(d).items():
        if p.name != "canary.txt":
            out.setdefault(f"{LEGACY_DIRNAME}/{rel}", hashlib.sha256(p.read_bytes()).hexdigest())
    return dict(sorted(out.items()))


def _acc(a: Sequence[str], b: Sequence[str]) -> float:
    return sum(x == y for x, y in zip(a, b, strict=True)) / len(a)


@dataclass
class _Rows:
    """One task's rows as numbers, train then written-test, with per-row plant uniforms."""

    spec: S.TaskSpecFiles
    ti: int
    train: list[list[str]]
    test: list[list[str]]
    xtr: list[list[float]]
    xte: list[list[float]]
    u: list[float]

    @staticmethod
    def of(spec: S.TaskSpecFiles, rng: KeyedStream) -> _Rows:
        ti = spec.header.index(spec.target)
        test = _written_test(spec)
        return _Rows(spec, ti, spec.train, test, [_nums(r, ti) for r in spec.train], [_nums(r, ti) for r in test],
                     [rng.random() for _ in range(len(spec.train) + len(test))])

    def spec_with(self, ytr: Sequence[str], yte: Sequence[str], task_id: str) -> S.TaskSpecFiles:
        return replace(self.spec, task_id=task_id, train=_relabel(self.train, self.ti, ytr),
                       test=_relabel(self.test, self.ti, yte))


def _check_metric(metric: str, what: str) -> None:
    if metric != "accuracy":
        raise SaltError(f"{what}: metric {metric!r} is not supported (only accuracy pins down the planted effect)")


def _plant(rows: _Rows, oracle: list[str], base: list[str], natural: list[str], rho: float) -> list[str]:
    return [y if o == b else (o if u < rho else b) for o, b, y, u in zip(oracle, base, natural, rows.u, strict=True)]


def _synthetic(family: str, n_rows: int, key: bytes, task_id: str) -> tuple[_Rows, list[str], list[str], list[str]]:
    if family not in ORACLES:
        raise SaltError(f"family {family!r} cannot host a salt (supported: {sorted(ORACLES)})")
    spec = S.synthetic_task(family, 0, n_rows, key)  # the real generator, the salt's own key
    _check_metric(spec.metric, family)
    rows = _Rows.of(spec, stream(key, "salt-plant"))
    xs = rows.xtr + rows.xte
    oracle = [ORACLES[family](x) for x in xs]
    natural = [r[rows.ti] for r in rows.train + rows.test]
    base0 = predict_centroids(fit_centroids(rows.xtr, natural[: len(rows.train)]), xs)
    return rows, oracle, base0, natural


def _write(spec: S.TaskSpecFiles, out: Path) -> Path:
    return S.write_task(spec, out)


_FIXED_POINT_ROUNDS = 6
_NULL_ROUNDS = 20


def _positive_once(family: str, n_rows: int, *, key: bytes, task_id: str, delta: float, out: Path) -> GeneratedSalt:
    """A positive salt of `family`: planted effect POSITIVE_EFFECT_MULTIPLIER * delta (realised value recorded)."""
    if not delta > 0:
        raise SaltError("delta must be positive")
    target = C.POSITIVE_EFFECT_MULTIPLIER * delta
    rows, oracle, base0, natural = _synthetic(family, n_rows, key, task_id)
    ntr = len(rows.train)

    xs = rows.xtr + rows.xte
    base = base0
    best: tuple[float, float, list[str]] | None = None
    for _ in range(_FIXED_POINT_ROUNDS):
        # bisect rho on the effect against the baseline REFITTED on the planted train file (what the salt's own
        # baseline.py will predict); near-monotone, so every step is a candidate and the closest one is kept. Then
        # plant against that refit and repeat towards the baseline's fixed point.
        lo, hi = 0.5, 1.0
        for _ in range(20):
            mid = (lo + hi) / 2
            y = _plant(rows, oracle, base, natural, mid)
            refit_te = predict_centroids(fit_centroids(rows.xtr, y[:ntr]), rows.xte)
            e = _acc(oracle[ntr:], y[ntr:]) - _acc(refit_te, y[ntr:])
            if best is None or abs(e - target) < abs(best[0] - target):
                best = (e, mid, y)
            if e < target:
                lo = mid
            else:
                hi = mid
        assert best is not None
        refit = predict_centroids(fit_centroids(rows.xtr, best[2][:ntr]), xs)
        if refit == base or abs(best[0] - target) <= 0.1 * C.POSITIVE_EFFECT_TOLERANCE * target:
            break
        base = refit
    assert best is not None
    realised, _rho, y = best
    if abs(realised - target) > C.POSITIVE_EFFECT_TOLERANCE * target:
        raise SaltError(f"{family}: could not plant an effect of {target:.4f} (got {realised:.4f}); "
                        f"use more rows or another family")
    d = _write(rows.spec_with(y[:ntr], y[ntr:], task_id), out)
    return GeneratedSalt(task_id, "positive", family, "synthetic", "accuracy", target, realised, None,
                         _file_hashes(d), d)



def _null_once(family: str, n_rows: int, *, key: bytes, task_id: str, delta: float, out: Path) -> GeneratedSalt:
    """A null salt of synthetic `family` (same generator, effect 0)."""
    rows, oracle, base, natural = _synthetic(family, n_rows, key, task_id)
    ntr = len(rows.train)
    xs = rows.xtr + rows.xte
    best: tuple[float, list[str], list[str]] | None = None
    for _ in range(_NULL_ROUNDS):
        y = _plant(rows, oracle, base, natural, 0.5)
        refit = predict_centroids(fit_centroids(rows.xtr, y[:ntr]), xs)
        # exact upper bound on the expected gain of the best rule over the refitted baseline, per test row: where
        # o != b (label 50/50 o/b) it is 1/2 if the refit predicts neither; where o == b (generator label, o its
        # Bayes rule) at most 1 if the refit differs from o
        gains = [0.0 if refit[i] == base[i] else ((0.0 if refit[i] == oracle[i] else 0.5) if oracle[i] != base[i]
                                                  else 1.0) for i in range(ntr, len(xs))]
        bound = sum(gains) / len(gains)
        if best is None or bound < best[0]:
            best = (bound, y, refit)
        if refit == base:
            break
        base = refit  # the labels follow the refitted baseline; iterate towards its fixed point, keep the best state
    assert best is not None
    bound, y, refit = best
    if bound > C.NULL_MAX_RESIDUAL_FRACTION * delta:
        raise SaltError(f"{family}: null salt residual effect bound {bound:.4f} exceeds "
                        f"{C.NULL_MAX_RESIDUAL_FRACTION} * delta")
    d = _write(rows.spec_with(y[:ntr], y[ntr:], task_id), out)
    return GeneratedSalt(task_id, "null", family, "synthetic", "accuracy", 0.0,
                         _acc(oracle[ntr:], y[ntr:]) - _acc(refit[ntr:], y[ntr:]), bound, _file_hashes(d), d)


# The centroid baseline of some families (rings: concentric classes, so every centroid sits near the origin) moves a
# lot when labels move, and a draw can fail to plant. A failed draw is retried under a derived key, at most this many
# times in all; the retries are deterministic, so the salt is still a function of its key.
SALT_ATTEMPTS = 4


def _attempts(make, family: str, n_rows: int, key: bytes, task_id: str, delta: float, out: Path) -> GeneratedSalt:
    errors = []
    for i in range(SALT_ATTEMPTS):
        try:
            return make(family, n_rows, key=key if i == 0 else subkey(key, f"salt-retry:{i}"), task_id=task_id,
                        delta=delta, out=out)
        except SaltError as e:
            if "not supported" in str(e) or "cannot host" in str(e) or "delta must" in str(e):
                raise
            errors.append(str(e))
    raise SaltError(f"{family}: no salt after {SALT_ATTEMPTS} draws: {errors[-1]}")


def positive_salt(family: str, n_rows: int, *, key: bytes, task_id: str, delta: float, out: Path) -> GeneratedSalt:
    """A positive salt of `family`: planted effect POSITIVE_EFFECT_MULTIPLIER * delta (realised value recorded)."""
    return _attempts(_positive_once, family, n_rows, key, task_id, delta, out)


def null_salt(family: str, n_rows: int, *, key: bytes, task_id: str, delta: float, out: Path) -> GeneratedSalt:
    """A null salt of synthetic `family` (same generator, effect 0)."""
    return _attempts(_null_once, family, n_rows, key, task_id, delta, out)


def _soft(cent: dict[str, list[float]], x: Sequence[float], temp: float) -> list[tuple[str, float]]:
    labels = sorted(cent)
    d = [_d2(x, cent[c]) for c in labels]
    m = min(d)
    w = [math.exp(-(v - m) / temp) for v in d]
    s = sum(w)
    return [(c, v / s) for c, v in zip(labels, w, strict=True)]


def _sample(probs: list[tuple[str, float]], u: float) -> str:
    acc = 0.0
    for c, p in probs:
        acc += p
        if u < acc:
            return c
    return probs[-1][0]


def plasmode_null(dataset: str, data: dict, *, key: bytes, task_id: str, delta: float, out: Path) -> GeneratedSalt:
    """A plasmode null of real `dataset`: real features, labels regenerated from the fitted baseline."""
    if dataset not in PLASMODE_DATASETS:
        raise SaltError(f"dataset {dataset!r} cannot host a plasmode null (supported: {sorted(PLASMODE_DATASETS)})")
    spec = S.real_task(dataset, data, key)  # same split stream as a real task of this key
    _check_metric(spec.metric, dataset)
    rows = _Rows.of(spec, stream(key, "salt-plant"))
    ntr = len(rows.train)
    xs = rows.xtr + rows.xte
    real_ytr = [r[rows.ti] for r in rows.train]
    cent = fit_centroids(rows.xtr, real_ytr)
    observed = _acc(predict_centroids(cent, rows.xtr), real_ytr)

    def expected_acc(temp: float, c: dict[str, list[float]]) -> float:
        return sum(dict(_soft(c, x, temp))[b] for x, b in zip(rows.xtr, predict_centroids(c, rows.xtr),
                                                              strict=True)) / ntr

    scale = sorted(_d2(x, c) for x in rows.xtr for c in cent.values())[ntr * len(cent) // 2] or 1.0
    lo, hi = math.log(scale * 1e-9), math.log(scale * 1e9)
    for _ in range(60):  # expected accuracy falls as T grows
        mid = (lo + hi) / 2
        if expected_acc(math.exp(mid), cent) > observed:
            lo = mid
        else:
            hi = mid
    temp = math.exp((lo + hi) / 2)
    for _ in range(_FIXED_POINT_ROUNDS):
        probs = [_soft(cent, x, temp) for x in xs]
        y = [_sample(p, u) for p, u in zip(probs, rows.u, strict=True)]
        new = fit_centroids(rows.xtr, y[:ntr])
        if set(new) != set(cent):
            raise SaltError(f"{dataset}: a class vanished from the regenerated train labels")
        if predict_centroids(new, xs) == predict_centroids(cent, xs):
            break
        cent_used, cent = cent, new
    else:
        cent = cent_used
        probs = [_soft(cent, x, temp) for x in xs]
        y = [_sample(p, u) for p, u in zip(probs, rows.u, strict=True)]
        new = fit_centroids(rows.xtr, y[:ntr])
    bayes = predict_centroids(cent, rows.xte)
    refit = predict_centroids(new, rows.xte)
    bound = sum(dict(p)[b] - dict(p)[r] for p, b, r in zip(probs[ntr:], bayes, refit, strict=True)) / len(bayes)
    if bound > C.NULL_MAX_RESIDUAL_FRACTION * delta:
        raise SaltError(f"{dataset}: plasmode residual effect bound {bound:.4f} exceeds "
                        f"{C.NULL_MAX_RESIDUAL_FRACTION} * delta")
    d = _write(rows.spec_with(y[:ntr], y[ntr:], task_id), out)
    return GeneratedSalt(task_id, "null", "real", "plasmode", "accuracy", 0.0,
                         _acc(bayes, y[ntr:]) - _acc(refit, y[ntr:]), bound, _file_hashes(d), d)


def read_labels(path: Path) -> list[str]:
    """The single-column label file a task writes (private/test_y.csv)."""
    return [r[0] for r in list(csv.reader(io.StringIO(Path(path).read_text(encoding="utf-8"))))[1:]]
