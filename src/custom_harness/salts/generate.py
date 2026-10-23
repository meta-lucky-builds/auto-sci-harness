"""Salt tasks (GOAL §16.9): positive salts with a known effect, null salts with none. Redesigned 2026-10-14
(docs/salts_redesign.md; orchestrator decision, owner-delegated) after the detectability exam failed (d_hat 0.975-1.0):
the old rho-relabelled salts were spotted by how well their train labels agree with the family's reference rule.

A salt is now a FRESH DRAW OF THE SAME GENERATOR as the real tasks of its family (research/suite.py `synthetic_task`
under the salt's own key, the same n_rows and the same heldout_rows as the real pool's tasks; `write_task` for the
layout). Nothing is relabelled, so a salt's files have exactly the distribution of a real task's files of that family:
no statistic of the presentation can tell them apart (d = 0 by construction, up to the keyed PRF; the exam still
measures it, salts/exam.py, unchanged).

- POSITIVE salt (families with a reference rule o(x) and the accuracy metric: rings, xor, spirals; and only when the
  campaign's real pool has the family). Its known ("planted") effect is the EXPECTED accuracy effect of o over the
  salt's own shipped baseline b (nearest centroid fitted on the salt's train file, exactly as baseline.py does:
  fit_centroids / predict_centroids):  E[1{o(x) = y}] - E[1{b(x) = y}]  over the generator's distribution, estimated
  by Monte Carlo on `mc_rows` fresh generator rows from independent keyed streams under the salt key
  ("salt-mc/features", "salt-mc/labels"). Recorded: the MC value (planted_effect, also target_effect), its standard
  error (mc_se), mc_rows, and the realised effect on the written test file (realised_effect). There is no rho and no
  "2 delta" target any more: coverage is tested only at the families' natural effect sizes (about 0.38-0.53 on the
  suite's tasks, w2-suite run1), not near delta.
- NULL salt (only GENUINELY null families, research.suite.FAMILIES[f].null, e.g. "noise"; and only when the campaign's
  real pool has the family). The labels are a fair coin independent of the features (and of every other row), so for
  ANY predictor (a function of the test features, the train file and its own randomness) each test label is correct
  with probability exactly 1/2: every predictor, the baseline included, has expected accuracy 1/2, and the largest true
  effect over the baseline is 0. Hence residual_bound = 0 by construction (computational caveat: the coin comes from
  an HMAC-SHA256 keyed stream; a predictor that cannot break the PRF without the key cannot do better).
- REFUSED (SaltError): a null salt of any non-null family (the rho = 1/2 relabelling null salts were dropped
  2026-10-14: their labels agree with the reference rule by D/2 = 0.18-0.26 less than real labels); plasmode nulls on
  real data (dropped 2026-10-14: same class of tell, labels regenerated from the centroid model); F1, RMSE and MAE
  families (the effect is not pinned down here); any family absent from the campaign's real pool.

Known limits: the reference rules are near-Bayes (rings ignores negative sampled radii; spirals is an angular
nearest-arm rule), so planted_effect is the expected effect of o, a LOWER bound on the best achievable effect; the
MC estimate ignores write_task's test de-duplication (rows whose features also occur in train; with 5-decimal
continuous features this is essentially never). See docs/salts_redesign.md.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from ..commitment import stream
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
# synthetic families that can host a POSITIVE salt: a reference rule and the accuracy metric
POSITIVE_FAMILIES = frozenset(f for f in ORACLES if S.FAMILIES[f].metric == "accuracy")
# synthetic families that can host a NULL salt: genuinely null (labels independent of the features)
NULL_FAMILIES = frozenset(f for f in S.NULL_FAMILIES if S.FAMILIES[f].metric == "accuracy")
# Plasmode nulls were dropped 2026-10-14 (plasmode_null refuses); kept empty so no slot is eligible for one.
PLASMODE_DATASETS: frozenset[str] = frozenset()


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
    family: str  # the synthetic family (a fresh draw of its generator)
    source: str  # "synthetic" (plasmodes were dropped 2026-10-14)
    metric: str
    # what the construction targets. Positive: the MC expected effect (= planted_effect; no longer "2 delta", which
    # was dropped with rho planting on 2026-10-14). Null: 0.
    target_effect: float
    # Positive: the Monte Carlo EXPECTED accuracy effect of the reference rule over the salt's own baseline (fitted on
    # its train file), the value the salt is scored against. Null: 0 (exact, see the module doc).
    planted_effect: float
    residual_bound: float | None  # null salts: the largest true effect any predictor can have (0 by construction)
    files: dict[str, str]  # relative path -> SHA-256 of every file written (canary excluded: fresh per task)
    task_dir: Path
    mc_se: float | None = None  # positive: standard error of planted_effect (Monte Carlo)
    realised_effect: float | None = None  # positive: the reference rule's effect over the baseline on the test file
    mc_rows: int | None = None  # positive: Monte Carlo rows behind planted_effect
    heldout_rows: int | None = None  # the generator's held-out size (as the real pool's tasks; None = 70/30 split)


def _written_test(spec: S.TaskSpecFiles) -> list[list[str]]:
    """The test rows write_task writes: those whose features also occur in train are dropped."""
    ti = spec.header.index(spec.target)

    def x(r: list[str]) -> tuple[str, ...]:
        return tuple(v for j, v in enumerate(r) if j != ti)

    seen = {x(r) for r in spec.train}
    return [r for r in spec.test if x(r) not in seen]


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


def _check_metric(metric: str, what: str) -> None:
    if metric != "accuracy":
        raise SaltError(f"{what}: metric {metric!r} is not supported (only accuracy pins down the planted effect)")


def _require_in_pool(family: str, pool_families: Collection[str], kind: str) -> None:
    if isinstance(pool_families, str):
        raise SaltError("pool_families must be a collection of family names, not one string")
    if family not in set(pool_families):
        raise SaltError(f"family {family!r} has no task in the campaign's real pool {sorted(set(pool_families))}: "
                        f"a {kind} salt must be a fresh draw of a family the real tasks come from (decision "
                        f"2026-10-14), otherwise its family alone tells it apart")


def salt_spec(family: str, n_rows: int, *, key: bytes, task_id: str,
              heldout_rows: int | None = None) -> S.TaskSpecFiles:
    """The salt's task: research.suite.synthetic_task(family, ..., key, heldout_rows) unchanged except its id (which
    the campaign assigns and the exam blanks). Labels are the generator's: nothing is relabelled."""
    if family not in S.FAMILIES:
        raise SaltError(f"unknown family {family!r}")
    if n_rows < 2:
        raise SaltError("n_rows must be at least 2")
    try:
        spec = S.synthetic_task(family, 0, n_rows, key, heldout_rows=heldout_rows)
    except ValueError as e:
        raise SaltError(f"{family}: {e}") from e
    return replace(spec, task_id=task_id)


def expected_effect(family: str, cent: dict[str, list[float]], *, key: bytes,
                    mc_rows: int | None = None) -> tuple[float, float, int]:
    """Monte Carlo estimate of E[1{o(x) = y}] - E[1{b(x) = y}] over `family`'s generator, with o the family's
    reference rule and b the nearest-centroid rule `cent`. Rows come from the keyed streams "salt-mc/features" and
    "salt-mc/labels" under `key` (independent of every stream the salt's files are drawn from). Returns (estimate,
    standard error, rows)."""
    if family not in ORACLES:
        raise SaltError(f"family {family!r} has no reference rule")
    n = C.SALT_MC_ROWS if mc_rows is None else int(mc_rows)
    if n < 2:
        raise SaltError("mc_rows must be at least 2")
    fam = S.FAMILIES[family]
    ti = fam.header.index(fam.target)
    rows = fam.rows(stream(key, "salt-mc/features"), stream(key, "salt-mc/labels"), n)
    xs = [_nums(r, ti) for r in rows]
    ys = [r[ti] for r in rows]
    base = predict_centroids(cent, xs)
    o = ORACLES[family]
    d = [(o(x) == y) - (b == y) for x, y, b in zip(xs, ys, base, strict=True)]
    mean = sum(d) / n
    var = max(0.0, (sum(v * v for v in d) / n - mean * mean) * n / (n - 1))
    return mean, math.sqrt(var / n), n


def positive_salt(family: str, n_rows: int, *, key: bytes, task_id: str, out: Path,
                  pool_families: Collection[str], heldout_rows: int | None = None,
                  mc_rows: int | None = None) -> GeneratedSalt:
    """A positive salt of `family`: a fresh draw of the family's generator (labels untouched), scored against the
    Monte Carlo expected effect of the reference rule over its own baseline. Refused (SaltError) for a family without
    a reference rule or the accuracy metric, or absent from `pool_families` (the campaign's real pool)."""
    if family not in POSITIVE_FAMILIES:
        raise SaltError(f"family {family!r} cannot host a positive salt (supported: {sorted(POSITIVE_FAMILIES)})")
    _require_in_pool(family, pool_families, "positive")
    spec = salt_spec(family, n_rows, key=key, task_id=task_id, heldout_rows=heldout_rows)
    _check_metric(spec.metric, family)
    ti = spec.header.index(spec.target)
    xtr = [_nums(r, ti) for r in spec.train]
    cent = fit_centroids(xtr, [r[ti] for r in spec.train])  # the shipped baseline.py's fit on the train file
    planted, se, n_mc = expected_effect(family, cent, key=key, mc_rows=mc_rows)
    test = _written_test(spec)
    if not test:
        raise SaltError(f"{family}: the salt's written test file is empty")
    xte = [_nums(r, ti) for r in test]
    yte = [r[ti] for r in test]
    realised = _acc([ORACLES[family](x) for x in xte], yte) - _acc(predict_centroids(cent, xte), yte)
    d = S.write_task(spec, out)
    return GeneratedSalt(task_id, "positive", family, "synthetic", "accuracy", planted, planted, None,
                         _file_hashes(d), d, mc_se=se, realised_effect=realised, mc_rows=n_mc,
                         heldout_rows=heldout_rows)


def null_salt(family: str, n_rows: int, *, key: bytes, task_id: str, out: Path, pool_families: Collection[str],
              heldout_rows: int | None = None) -> GeneratedSalt:
    """A null salt: a fresh draw of a GENUINELY null family's generator (research.suite.FAMILIES[f].null), effect 0
    by construction (module doc). Refused (SaltError) for any non-null family (rho = 1/2 relabelling was dropped
    2026-10-14) and for a family absent from `pool_families` (the campaign's real pool)."""
    if family not in NULL_FAMILIES:
        raise SaltError(f"family {family!r} is not genuinely null (null families: {sorted(NULL_FAMILIES)}); null "
                        f"salts by relabelling a non-null family (rho = 1/2) were dropped 2026-10-14: their train "
                        f"labels agree with the family's reference rule far less than real tasks' labels do")
    _require_in_pool(family, pool_families, "null")
    spec = salt_spec(family, n_rows, key=key, task_id=task_id, heldout_rows=heldout_rows)
    _check_metric(spec.metric, family)
    d = S.write_task(spec, out)
    return GeneratedSalt(task_id, "null", family, "synthetic", "accuracy", 0.0, 0.0, 0.0, _file_hashes(d), d,
                         heldout_rows=heldout_rows)


def plasmode_null(dataset: str, data: dict, **_kw: object) -> GeneratedSalt:
    """DROPPED 2026-10-14 (decision, owner-delegated): always refused. A plasmode keeps the real features and draws
    labels from the fitted centroid model, so its labels agree with the centroid baseline far more than the real
    labels do: the same tell that failed the detectability exam for rho = 1/2 null salts."""
    raise SaltError(f"plasmode null salts (dataset {dataset!r}) were dropped on 2026-10-14: their regenerated labels "
                    f"carry the same tell as rho = 1/2 null salts; use a null salt of a genuinely null family that is "
                    f"in the real pool (docs/salts_redesign.md)")


def read_labels(path: Path) -> list[str]:
    """A single-column label file as write_task writes it (test_y.csv; read it via truth_layout.truth_file)."""
    return [r[0] for r in list(csv.reader(io.StringIO(Path(path).read_text(encoding="utf-8"))))[1:]]
