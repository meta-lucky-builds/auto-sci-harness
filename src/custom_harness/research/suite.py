"""ML task suite builder (GOAL M2: >= 20 tasks; rings-style synthetic + real datasets + an ML-repair family).

    chh build-suite OUT_DIR [--docker]

Synthetic families are generated on the host with the standard library. Real-dataset and repair tasks are
generated INSIDE the sandbox image from scikit-learn's bundled copies (no network, nothing downloaded),
so the host needs no ML libraries. Every task follows research/task.py: public/ train + test features, test labels
+ a canary in the task's truth dir (truth_layout.truth_dir: OUTSIDE every agent-readable root for a suite under
<data>/suites), and a baseline obeying the solution CLI contract.

Repair tasks (ScienceIDE-style "codebase repair" adapted to ML pipelines): the baseline is a deliberately
defective pipeline; the defect is recorded in the task's `defect` note (NOT shown to the agent: it stays in
suite metadata), so a solution that repairs it is a measurable, known-to-exist improvement.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import random
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..commitment import as_key, draw, stream, subkey

# ---------------------------------------------------------------- §16.2 parameters (the one place they are set)
# Rows of a minted held-out test set. ~20k takes the minimum certifiable effect from ~0.135 to ~0.005 (GOAL §16.2,
# computed, not measured here). Larger = more power per claim.
MINT_TEST_ROWS = 20_000  # UNCALIBRATED (owner decision, GOAL §13.8)
# Rows of a minted training file ("small train / large test").
MINT_TRAIN_ROWS = 600  # UNCALIBRATED (owner decision, GOAL §13.8)
# Smallest effect (metric units, e.g. 0.02 = 2 accuracy points) a claim on a minted target may certify, so a huge
# test set does not make trivial effects certifiable. NOT enforced here: the Kernel (K1) owns claim policy and has
# been asked to read it (mailbox mint-to-kstats). Conservative: well above the ~0.005 minimum detectable effect.
DELTA_FLOOR = 0.02  # UNCALIBRATED (owner decision, GOAL §13.8)
# Held-out rows per synthetic task of a suite built with `heldout_rows=` (build_suite, `chh build-suite
# --heldout-rows`; recorded in the rebuild manifest). The default suite (None) keeps the 70/30 split of SYNTHETIC.
# PROPOSED value from w2-suite's PARTIAL real preflight (research.preflight, n_sims 200, n_dev 1050, baseline accuracy
# 0.8, comparator-only-right rate 0.05, delta = DELTA_FLOOR = 0.02): a true effect of 0.04 (= 2 delta) certifies with
# P = 0.75 at 4,000 and 0.88 at 5,000 held-out rows (4,500 NOT measured), so P >= 0.8 needs 4,000-5,000. Accuracy
# tasks only (rings, xor, spirals); f1_macro/rmse/mae tasks were not modelled. UNCALIBRATED (owner decision, §13.8).
PROPOSED_HELDOUT_ROWS = 4_500

# ---------------------------------------------------------------- baselines (stdlib, solution CLI contract)
_ARGS = '''import argparse, csv, math
p = argparse.ArgumentParser()
p.add_argument("--train"); p.add_argument("--test"); p.add_argument("--out"); p.add_argument("--seed", type=int)
a = p.parse_args()
def load(path):
    with open(path, newline="") as f:
        r = list(csv.reader(f))
    return r[0], r[1:]
h, rows = load(a.train)
th, trows = load(a.test)
TARGET = __TARGET__
yi = h.index(TARGET)
def nums(header, row):
    out = []
    for j, name in enumerate(header):
        if name == TARGET:
            continue
        try:
            out.append(float(row[j]))
        except ValueError:
            pass
    return out
def write(preds):
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["prediction"])
        for x in preds: w.writerow([x])
'''

CENTROID = _ARGS + '''
cent, cnt = {}, {}
for r in rows:
    v = nums(h, r)
    cent[r[yi]] = [s + x for s, x in zip(cent.get(r[yi], [0.0] * len(v)), v)]
    cnt[r[yi]] = cnt.get(r[yi], 0) + 1
cent = {y: [s / cnt[y] for s in v] for y, v in cent.items()}
write([min(sorted(cent), key=lambda y: sum((p - q) ** 2 for p, q in zip(nums(th, r), cent[y]))) for r in trows])
'''

MEAN = _ARGS + '''
m = sum(float(r[yi]) for r in rows) / len(rows)
write([f"{m:.6f}" for _ in trows])
'''

# deliberately defective sklearn pipelines (repair family); run in the sandbox image, which has scikit-learn
_SK = '''import argparse
import pandas as pd
p = argparse.ArgumentParser()
p.add_argument("--train"); p.add_argument("--test"); p.add_argument("--out"); p.add_argument("--seed", type=int)
a = p.parse_args()
tr = pd.read_csv(a.train); te = pd.read_csv(a.test)
y = tr.pop(__TARGET__)
__MODEL__
model.fit(tr, y)
pd.DataFrame({"prediction": model.predict(te)}).to_csv(a.out, index=False)
'''
REPAIRS = {
    "wine_knn_unscaled": ("wine", "from sklearn.neighbors import KNeighborsClassifier\n"
                                  "model = KNeighborsClassifier(n_neighbors=1)",
                          "1-NN on unscaled features (proline dominates distances); scaling + k > 1 repairs it"),
    "cancer_logreg_unconverged": ("breast_cancer", "from sklearn.linear_model import LogisticRegression\n"
                                                   "model = LogisticRegression(max_iter=3)",
                                  "logistic regression stopped after 3 iterations, unscaled inputs"),
    "digits_tree_depth2": ("digits", "from sklearn.tree import DecisionTreeClassifier\n"
                                     "model = DecisionTreeClassifier(max_depth=2, random_state=a.seed)",
                           "a depth-2 tree cannot separate 10 digit classes"),
    "diabetes_ridge_overregularised": ("diabetes", "from sklearn.linear_model import Ridge\n"
                                                   "model = Ridge(alpha=1e4)",
                                       "ridge penalty 1e4 shrinks all coefficients to ~0"),
}


def _fill(template: str, target: str, model: str = "") -> str:
    return template.replace("__TARGET__", repr(target)).replace("__MODEL__", model)


# task origins (GOAL §16.10). A suite entry without `origin` is human (suites written before the field existed).
ORIGINS = ("human", "generated")
GENERATED_SPLITS = ("evolve",)  # the only split a generated task may sit in


@dataclass(frozen=True)
class TaskSpecFiles:
    task_id: str
    title: str
    description: str
    metric: str
    target: str
    header: list[str]
    train: list[list[str]]
    test: list[list[str]]
    baseline: str
    family: str
    defect: str | None = None
    # the base dataset the rows were drawn from; tasks re-split from the same data share examples (a repair task
    # and its real-data task), so they must share a split. None = generated independently (the task id).
    source: str | None = None
    # a fixed public dataset (GOAL §16.2): its truth exists before any freeze and cannot be minted, so it keeps the
    # §16.5 controls and a per-target alpha-account (§16.4)
    retrospective_target: bool = False
    # who made the task (GOAL §16.10 emergence rule): "human" (curated; any split) or "generated" (a self-generated
    # task family; EVOLVE split only, never gate, sealed or a held-out family: the system must not write its own exam)
    origin: str = "human"
    # public (file, column) pairs that carry a copy of the held-out truth, declared by the task's author (e.g. a
    # planted leak column); write_task adds every test_X column it detects as one (truth_copy_columns)
    truth_copies: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.origin not in ORIGINS:
            raise ValueError(f"task {self.task_id}: unknown origin {self.origin!r} (expected one of {ORIGINS})")

    @property
    def data_source(self) -> str:
        return self.source or self.task_id


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode()


TRUTH_COPY_MIN_ROWS = 20  # below this, any column "determines" the labels by chance; UNCALIBRATED


def truth_copy_columns(header: list[str], rows: list[list[str]], labels: list[str], metric: str) -> list[str]:
    """Columns of the public test features that carry a copy of the held-out labels (GOAL §16.5.4): for a
    classification metric, a column whose values map one-to-one onto the labels (the labels under a renaming); for a
    regression metric, a column numerically equal to the label on every row. Needs TRUTH_COPY_MIN_ROWS rows. A
    copy encoded any other way (a hash, a lookup in another file) is NOT detected: authors declare those
    (TaskSpecFiles.truth_copies)."""
    if len(rows) < TRUTH_COPY_MIN_ROWS or len(rows) != len(labels):
        return []
    out = []
    for j, name in enumerate(header):
        col = [r[j] for r in rows]
        if metric in ("rmse", "mae"):
            try:
                same = all(math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12) for a, b in zip(col, labels, strict=True))
            except ValueError:
                same = False
            if same:
                out.append(name)
            continue
        fwd: dict[str, str] = {}
        back: dict[str, str] = {}
        if all(fwd.setdefault(v, y) == y and back.setdefault(y, v) == v for v, y in zip(col, labels, strict=True)):
            out.append(name)
    return out


def write_task(spec: TaskSpecFiles, root: Path, canary: str | None = None) -> Path:
    """Write one task. Test rows whose features also occur in the training file are dropped (de-duplication:
    their label would be public). `canary`: share one between tasks that share data (default: a fresh one).
    The labels and canary go to truth_layout.truth_dir(task dir), which refuses (TruthLayoutError) a location inside
    an agent-readable root BEFORE anything is written."""
    from ..truth_layout import truth_dir

    d = Path(root) / spec.task_id
    truth = truth_dir(d)
    (d / "public").mkdir(parents=True, exist_ok=True)
    truth.mkdir(parents=True, exist_ok=True)
    ti = spec.header.index(spec.target)
    feat = [c for j, c in enumerate(spec.header) if j != ti]

    def x(r: list[str]) -> tuple[str, ...]:
        return tuple(v for j, v in enumerate(r) if j != ti)

    seen = {x(r) for r in spec.train}
    test = [r for r in spec.test if x(r) not in seen]
    (d / "public" / "train.csv").write_bytes(_csv([spec.header, *spec.train]))
    (d / "public" / "test_X.csv").write_bytes(_csv([feat, *[list(x(r)) for r in test]]))
    (truth / "test_y.csv").write_bytes(_csv([[spec.target], *[[r[ti]] for r in test]]))
    detected = truth_copy_columns(feat, [list(x(r)) for r in test], [r[ti] for r in test], spec.metric)
    copies = sorted(set(spec.truth_copies) | {("test_X.csv", c) for c in detected})
    if copies:  # the exporter's declaration (truth dir only); research.claims.register_task declares it to the Kernel
        (truth / "truth_copies.json").write_text(json.dumps([{"file": f, "column": c} for f, c in copies]) + "\n",
                                                 encoding="utf-8")
    (truth / "canary.txt").write_text((canary or "CHH-CANARY-" + secrets.token_hex(16)) + "\n", encoding="utf-8")
    (d / "baseline.py").write_text(spec.baseline, encoding="utf-8")
    (d / "task.yaml").write_text(yaml.safe_dump({
        "id": spec.task_id, "title": spec.title, "description": spec.description, "metric": spec.metric,
        "target_column": spec.target, "solution_timeout_s": 600}, sort_keys=False), encoding="utf-8")
    return d


# ---------------------------------------------------------------- synthetic families (stdlib)
# COUPLED-PRNG fix (GOAL §16.1): features, labels and the split of a task each come from their OWN keyed stream
# (commitment.stream: HMAC-SHA256 in counter mode) under a per-task key. The key is never a constant in the code:
# it is derived from a master key that is fresh OS entropy (default), or a draw() result when minting (§16.2).
# Before 2026-10-07 every generator used random.Random(<published seed>), so anyone who read the repo regenerated
# every held-out label (12/12 synthetic tasks of the live suites matched).
GENERATOR_VERSION = "keyed-hmac-sha256/1"


def task_key(master: bytes, task_id: str) -> bytes:
    """The key of one task (or base dataset) under a suite's master key."""
    return subkey(master, "task:" + task_id)


def _key(key: bytes | None) -> bytes:
    return key if key is not None else secrets.token_bytes(32)


def _split(rows: list[list[str]], rng: random.Random, frac: float = 0.7) -> tuple[list[list[str]], list[list[str]]]:
    rows = rows[:]
    rng.shuffle(rows)
    k = int(len(rows) * frac)
    return rows[:k], rows[k:]


def _f(x: float) -> str:
    return f"{x:.5f}"


Rows = Callable[[random.Random, random.Random, int], list[list[str]]]


def _rings_rows(f: random.Random, lab: random.Random, n: int) -> list[list[str]]:
    rows = []
    for _ in range(n):
        y = lab.randrange(3)
        r, t = 1.0 + 1.2 * y + f.gauss(0, 0.45), f.uniform(0, 2 * math.pi)
        rows.append([_f(r * math.cos(t)), _f(r * math.sin(t)), *[_f(f.gauss(0, 1)) for _ in range(4)], str(y)])
    return rows


def _xor_rows(f: random.Random, lab: random.Random, n: int) -> list[list[str]]:
    rows = []
    for _ in range(n):
        x1, x2 = f.uniform(-1, 1), f.uniform(-1, 1)
        y = int((x1 > 0) != (x2 > 0))
        if lab.random() < 0.08:
            y = 1 - y
        rows.append([_f(x1), _f(x2), *[_f(f.uniform(-1, 1)) for _ in range(3)], str(y)])
    return rows


def _spirals_rows(f: random.Random, lab: random.Random, n: int) -> list[list[str]]:
    rows = []
    for _ in range(n):
        y = lab.randrange(2)
        t = f.uniform(0.5, 3 * math.pi)
        r = t / (3 * math.pi) * 2
        a = t + y * math.pi
        rows.append([_f(r * math.cos(a) + f.gauss(0, 0.06)), _f(r * math.sin(a) + f.gauss(0, 0.06)), str(y)])
    return rows


_CENTERS = {0: (0.0, 0.0), 1: (1.6, 0.4), 2: (0.6, 1.8)}


def _imbalanced_rows(f: random.Random, lab: random.Random, n: int) -> list[list[str]]:
    rows = []
    for _ in range(n):
        u = lab.random()
        y = 0 if u < 0.8 else (1 if u < 0.95 else 2)
        cx, cy = _CENTERS[y]
        rows.append([_f(cx + f.gauss(0, 0.7)), _f(cy + f.gauss(0, 0.7)), *[_f(f.gauss(0, 1)) for _ in range(3)],
                     str(y)])
    return rows


def _friedman_rows(f: random.Random, lab: random.Random, n: int) -> list[list[str]]:
    rows = []
    for _ in range(n):
        x = [f.random() for _ in range(8)]
        y = 10 * math.sin(math.pi * x[0] * x[1]) + 20 * (x[2] - 0.5) ** 2 + 10 * x[3] + 5 * x[4] + lab.gauss(0, 1)
        rows.append([*[_f(v) for v in x], _f(y)])
    return rows


def _interactions_rows(f: random.Random, lab: random.Random, n: int) -> list[list[str]]:
    rows = []
    for _ in range(n):
        x = [f.gauss(0, 1) for _ in range(6)]
        y = 2 * x[0] - x[1] + 1.5 * x[2] * x[3] + abs(x[4]) + lab.gauss(0, 0.5)
        rows.append([*[_f(v) for v in x], _f(y)])
    return rows


def _noise_rows(f: random.Random, lab: random.Random, n: int) -> list[list[str]]:
    """A GENUINELY NULL family (GOAL §16.9): the label is a fair coin independent of the features, so every
    predictor has expected accuracy exactly 1/2 and no claim of an effect over any baseline is true."""
    rows = []
    for _ in range(n):
        y = lab.randrange(2)
        rows.append([*[_f(f.gauss(0, 1)) for _ in range(5)], str(y)])
    return rows


@dataclass(frozen=True)
class Family:
    rows: Rows
    title: str
    description: str
    metric: str
    target: str
    header: list[str]
    baseline: str
    # True: labels are independent of the features (no predictor beats another in expectation). Only such a family,
    # when it is ALSO in the real task pool, may host null salts (salts/generate.null_salt; decision 2026-10-14).
    null: bool = False


FAMILIES: dict[str, Family] = {
    "rings": Family(_rings_rows, "Noisy concentric rings (3-class)",
                    "Predict the class `label` (0, 1, 2) of points on noisy concentric rings. x1, x2 carry the "
                    "signal; n1..n4 are noise.", "accuracy", "label",
                    ["x1", "x2", "n1", "n2", "n3", "n4", "label"], _fill(CENTROID, "label")),
    "xor": Family(_xor_rows, "Noisy XOR (binary)",
                  "Predict `label` (0/1). The class depends on the signs of two of the five features (8% label "
                  "noise).", "accuracy", "label", ["f1", "f2", "f3", "f4", "f5", "label"], _fill(CENTROID, "label")),
    "spirals": Family(_spirals_rows, "Two interleaved spirals",
                      "Predict `label` (0/1) of points on two interleaved noisy spirals.", "accuracy", "label",
                      ["x", "y", "label"], _fill(CENTROID, "label")),
    "imbalanced": Family(_imbalanced_rows, "Imbalanced overlapping blobs (macro F1)",
                         "Predict `label` (0/1/2); classes are 80/15/5% and overlap. Scored by macro-averaged F1, so "
                         "minority classes matter.", "f1_macro", "label", ["a", "b", "c", "d", "e", "label"],
                         _fill(CENTROID, "label")),
    "friedman": Family(_friedman_rows, "Friedman-1 regression (RMSE)",
                       "Predict the continuous target `y` from x1..x8 (three are irrelevant). Lower RMSE is better.",
                       "rmse", "y", [*[f"x{i}" for i in range(1, 9)], "y"], _fill(MEAN, "y")),
    "interactions": Family(_interactions_rows, "Sparse regression with an interaction (MAE)",
                           "Predict `y` from six Gaussian features; one pairwise interaction and one non-linearity "
                           "matter. Lower MAE is better.", "mae", "y", [*[f"x{i}" for i in range(1, 7)], "y"],
                           _fill(MEAN, "y")),
    # genuinely null (see _noise_rows). NOT in SYNTHETIC: a suite gets it only with build_suite(null_families=...)
    # (owner decision: adding null tasks to the real pool, GOAL §13.8)
    "noise": Family(_noise_rows, "Binary labels from five features",
                    "Predict `label` (0/1) from five numeric features.", "accuracy", "label",
                    ["f1", "f2", "f3", "f4", "f5", "label"], _fill(CENTROID, "label"), null=True),
}
NULL_FAMILIES = frozenset(f for f, fam in FAMILIES.items() if fam.null)


def synthetic_task(family: str, variant: int, n: int, key: bytes | None = None, *,
                   heldout_rows: int | None = None) -> TaskSpecFiles:
    """Task `<family>-s<variant>` with n rows (70/30 split). `variant` only names the task: the data come from
    `key` (None: fresh OS entropy, so the rows can never be regenerated from the code).

    `heldout_rows` (§16.2 "small train / large test", PROPOSED_HELDOUT_ROWS): the training file stays exactly what
    the 70/30 split gives, and the test set is topped up to `heldout_rows` rows (before write_task's de-duplication)
    with rows from two more independent keyed streams ("heldout/features", "heldout/labels") of the same generator.
    None (default) leaves the task byte-identical to a suite built without the parameter."""
    fam, k = FAMILIES[family], _key(key)
    rows = fam.rows(stream(k, "features"), stream(k, "labels"), n)
    tr, te = _split(rows, stream(k, "split"))
    if heldout_rows is not None:
        if heldout_rows < 1:
            raise ValueError("heldout_rows must be positive")
        extra = heldout_rows - len(te)
        if extra > 0:
            te = te + fam.rows(stream(k, "heldout/features"), stream(k, "heldout/labels"), extra)
    return TaskSpecFiles(f"{family}-s{variant}", fam.title, fam.description, fam.metric, fam.target, fam.header,
                         tr, te, fam.baseline, family)


def rings(variant: int, n: int, key: bytes | None = None, **kw: Any) -> TaskSpecFiles:
    return synthetic_task("rings", variant, n, key, **kw)


def xor(variant: int, n: int, key: bytes | None = None, **kw: Any) -> TaskSpecFiles:
    return synthetic_task("xor", variant, n, key, **kw)


def spirals(variant: int, n: int, key: bytes | None = None, **kw: Any) -> TaskSpecFiles:
    return synthetic_task("spirals", variant, n, key, **kw)


def imbalanced(variant: int, n: int, key: bytes | None = None, **kw: Any) -> TaskSpecFiles:
    return synthetic_task("imbalanced", variant, n, key, **kw)


def friedman(variant: int, n: int, key: bytes | None = None, **kw: Any) -> TaskSpecFiles:
    return synthetic_task("friedman", variant, n, key, **kw)


def interactions(variant: int, n: int, key: bytes | None = None, **kw: Any) -> TaskSpecFiles:
    return synthetic_task("interactions", variant, n, key, **kw)


def noise(variant: int, n: int, key: bytes | None = None, **kw: Any) -> TaskSpecFiles:
    return synthetic_task("noise", variant, n, key, **kw)


SYNTHETIC: list[tuple[Callable[..., TaskSpecFiles], int, int]] = [
    (rings, 1, 900), (rings, 2, 1500), (xor, 1, 900), (xor, 2, 1500), (spirals, 1, 900), (spirals, 2, 1400),
    (imbalanced, 1, 1200), (imbalanced, 2, 2000), (friedman, 1, 900), (friedman, 2, 1500),
    (interactions, 1, 900), (interactions, 2, 1500),
]
# Genuinely null tasks a suite MAY add to its real pool (build_suite(null_families=("noise",))): null salts are drawn
# only from such a family, and only when the pool has it (decision 2026-10-14, GOAL §16.9). Off by default.
NULL_SYNTHETIC: list[tuple[Callable[..., TaskSpecFiles], int, int]] = [(noise, 1, 900), (noise, 2, 1500)]


def synthetic_plan(null_families: tuple[str, ...] = ()) -> list[tuple[Callable[..., TaskSpecFiles], int, int]]:
    """SYNTHETIC plus the NULL_SYNTHETIC tasks of `null_families`. Fails closed on a family that is not null."""
    bad = sorted(set(null_families) - NULL_FAMILIES)
    if bad:
        raise ValueError(f"not genuinely null families: {bad} (null families: {sorted(NULL_FAMILIES)})")
    return SYNTHETIC + [t for t in NULL_SYNTHETIC if t[0](t[1], 1, key=b"\x00" * 32).family in null_families]


# ---------------------------------------------------------------- real datasets (generated in the sandbox)
REAL = {
    "iris": ("load_iris", "accuracy", "Iris species (3-class) from four flower measurements."),
    "wine": ("load_wine", "accuracy", "Wine cultivar (3-class) from 13 chemical measurements."),
    "breast_cancer": ("load_breast_cancer", "f1_macro", "Malignant vs benign tumours from 30 cell-nucleus "
                                                        "features (macro F1)."),
    "digits": ("load_digits", "accuracy", "Handwritten digits 0-9 from 8x8 pixel intensities."),
    "diabetes": ("load_diabetes", "rmse", "Disease progression one year after baseline from 10 clinical "
                                          "measurements (RMSE)."),
}

_EXPORT = '''import json, sys
from sklearn import datasets
out = {}
for name, loader in json.loads(sys.argv[1]).items():
    d = getattr(datasets, loader)()
    out[name] = {"columns": [str(c).replace(" ", "_") for c in getattr(d, "feature_names", [])] or
                            [f"f{i}" for i in range(d.data.shape[1])],
                 "X": d.data.round(6).tolist(), "y": d.target.tolist()}
json.dump(out, open("export.json", "w"))
'''


def export_real(sandbox_factory: Callable[[Path], object], work: Path) -> dict[str, dict]:
    """Run the exporter inside the FULL sandbox image (scikit-learn's bundled datasets; no network). Trusted code only:
    agent code runs in the candidate image, which has no bundled datasets (sandbox.docker.CANDIDATE_IMAGE)."""
    sb = sandbox_factory(work)
    (work / "export.py").write_text(_EXPORT, encoding="utf-8")
    res = sb.exec([sb.python, "export.py", json.dumps({k: v[0] for k, v in REAL.items()})],  # type: ignore[attr-defined]
                  timeout_s=600)
    try:
        if res.returncode != 0:
            raise RuntimeError(f"dataset export failed in the sandbox: {res.stderr[-1500:]}")
        return json.loads((work / "export.json").read_text(encoding="utf-8"))
    finally:  # export.json is every labelled row of every real dataset: never leave that copy next to the suite
        for f in ("export.json", "export.py"):
            (work / f).unlink(missing_ok=True)
        if work.is_dir() and not any(work.iterdir()):
            work.rmdir()


def real_task(name: str, data: dict, key: bytes | None = None, *, baseline: str | None = None,
              task_id: str | None = None, defect: str | None = None) -> TaskSpecFiles:
    _loader, metric, desc = REAL[name]
    target = "target"
    header = [*data["columns"], target]
    is_cls = metric != "rmse"
    rows = [[*[str(v) for v in x], str(int(y)) if is_cls else f"{y:.4f}"] for x, y in zip(data["X"], data["y"],
                                                                                        strict=True)]
    tr, te = _split(rows, stream(_key(key), "split"))  # key: shared by tasks re-split from one dataset
    return TaskSpecFiles(task_id or f"{name}-real", f"{name.replace('_', ' ').title()} (real data)", desc, metric,
                         target, header, tr, te,
                         baseline or _fill(CENTROID if is_cls else MEAN, target),
                         "repair" if defect else "real", defect, source=name,
                         retrospective_target=True)


# ---------------------------------------------------------------- the suite
_PATTERN = ("evolve", "gate", "evolve", "sealed", "evolve")


def source_of(task_id: str) -> str:
    """The base dataset of a task built by this module (used to annotate suites written before `source` existed)."""
    if task_id in REPAIRS:
        return REPAIRS[task_id][0]
    if task_id.endswith("-real") and task_id.removesuffix("-real") in REAL:
        return task_id.removesuffix("-real")
    return task_id


# Held out of evolution by default (leave-one-family-out gate, GOAL §5 L6.8): every task of these families goes to
# the gate split. spirals: a synthetic family whose tasks each have their own base dataset (holding it out moves no
# other task, unlike real/repair, which share datasets), a geometry no other family has (two interleaved spirals),
# and the cheapest tasks to evaluate (2 features, <= 1400 rows). The lead rotates it across campaigns (GOAL L6.8).
DEFAULT_HELDOUT_FAMILIES: tuple[str, ...] = ("spirals",)


def assign_splits(specs: list[TaskSpecFiles], heldout_families: tuple[str, ...] = DEFAULT_HELDOUT_FAMILIES
                  ) -> dict[str, str]:
    """Every task of a `heldout_families` family goes to gate (the leave-one-family-out gate's held-out family:
    gate tasks, no evolve task). The other tasks get ~60/20/20 evolve/gate/sealed, cycled over tasks ordered by
    family so every split mixes families. Then every task takes the split of the first task drawn from the same base
    dataset (held-out tasks first, so their datasets stay in gate), so examples shared by re-splits of one dataset can
    never sit in two splits (a gate/sealed label would be public elsewhere). Fails closed (ValueError) on no or an
    unknown held-out family, or an empty split.

    Generated tasks (origin "generated", GOAL §16.10) all go to evolve and take no part in the human assignment
    above, so adding or removing them never moves a human task. Fails closed on an unknown origin, on a held-out
    family that has a generated task (a held-out family must be human-curated), and on a generated task that shares
    a base dataset with a human task (one of them would have to leave its split, or the human task's held-out
    examples would be public in evolve)."""
    bad_origin = sorted(s.task_id for s in specs if s.origin not in ORIGINS)
    if bad_origin:
        raise ValueError(f"task(s) with an unknown origin: {bad_origin}")
    generated = [s for s in specs if s.origin == "generated"]
    human = [s for s in specs if s.origin != "generated"]
    gen_held = sorted({s.family for s in generated} & set(heldout_families))
    if gen_held:
        raise ValueError(f"held-out families with a generated task: {gen_held} (held-out families are human-curated)")
    shared = sorted({s.data_source for s in generated} & {s.data_source for s in human})
    if shared:
        raise ValueError(f"base dataset(s) {shared} shared by a generated and a human task")
    families = {s.family for s in human}
    if not heldout_families:
        raise ValueError("no held-out family requested (the leave-one-family-out gate needs one)")
    unknown = sorted(set(heldout_families) - families)
    if unknown:
        raise ValueError(f"held-out families with no task: {unknown}")
    ordered = sorted(human, key=lambda t: (t.family, t.task_id))
    held = [s for s in ordered if s.family in heldout_families]
    rest = [s for s in ordered if s.family not in heldout_families]
    proposed = {s.task_id: "gate" for s in held} | {s.task_id: _PATTERN[i % len(_PATTERN)] for i, s in enumerate(rest)}
    first: dict[str, str] = {}
    for s in held + rest:
        first.setdefault(s.data_source, proposed[s.task_id])
    splits = {s.task_id: first[s.data_source] for s in ordered}
    splits |= {s.task_id: "evolve" for s in generated}
    empty = sorted({"evolve", "gate", "sealed"} - set(splits.values()))
    if empty:
        raise ValueError(f"split(s) {empty} would be empty")
    if any(splits[s.task_id] != "gate" for s in held):  # unreachable: held-out sources were claimed first
        raise ValueError("a held-out family's task left the gate split")
    return splits


def _example_rows(task_dir: Path) -> set[str]:
    """Every example's feature values (train rows without the target, and test rows) as normalised text."""
    from .task import load_task  # local import: task.py is light, but keep this module importable on its own

    task = load_task(task_dir)
    rows: set[str] = set()
    with open(task_dir / "public" / task.train_file, newline="", encoding="utf-8") as f:
        train = list(csv.reader(f))
    ti = train[0].index(task.target_column)
    rows |= {",".join(v for j, v in enumerate(r) if j != ti) for r in train[1:] if r}
    with open(task_dir / "public" / task.test_features_file, newline="", encoding="utf-8") as f:
        rows |= {",".join(r) for r in list(csv.reader(f))[1:] if r}
    return rows


def check_split_isolation(entries: list[dict]) -> list[str]:
    """Problems that would let one split see another split's examples: a missing source, a source spread over
    several splits, or (by content, whatever the declared sources say) an example row present in tasks of two
    different splits. Compares public files only; exact text match, so re-formatted copies are not detected.
    Also (GOAL §16.10): an unknown `origin`, or a generated task outside the evolve split. No `origin` = human."""
    problems = [f"task {e.get('id')} has no `source` (base dataset)" for e in entries if not e.get("source")]
    for e in entries:
        origin = e.get("origin", "human")
        if origin not in ORIGINS:
            problems.append(f"task {e.get('id')} has an unknown origin {origin!r} (expected one of {ORIGINS})")
        elif origin == "generated" and e.get("split") not in GENERATED_SPLITS:
            problems.append(f"generated task {e.get('id')} is in split {e.get('split')!r}: generated tasks may "
                            f"only be in {GENERATED_SPLITS} (GOAL §16.10)")
    by_source: dict[str, set[str]] = {}
    for e in entries:
        if e.get("source"):
            by_source.setdefault(e["source"], set()).add(e["split"])
    problems += [f"source {s} spans splits {sorted(sp)}" for s, sp in sorted(by_source.items()) if len(sp) > 1]
    rows = {e["id"]: _example_rows(Path(e["dir"])) for e in entries}
    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            if a["split"] != b["split"]:
                shared = len(rows[a["id"]] & rows[b["id"]])
                if shared:
                    problems.append(f"{a['id']} ({a['split']}) and {b['id']} ({b['split']}) share {shared} "
                                    f"example row(s)")
    return problems


def dataset_hash(data: dict) -> str:
    """SHA-256 of one exported real dataset (canonical JSON of its columns, X and y): its provenance commitment."""
    return hashlib.sha256(json.dumps({k: data[k] for k in ("columns", "X", "y")}, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def canary_for(master: bytes, source: str) -> str:
    """The canary of one base dataset: keyed under the master key, so a rebuild with the same key is byte-identical
    and the canary is as unguessable as the key (it is never a constant and never derivable without the key)."""
    return "CHH-CANARY-" + subkey(master, "canary:" + source).hex()[:32]


def build_suite(out: Path, *, real_data: dict[str, dict] | None,
                heldout_families: tuple[str, ...] = DEFAULT_HELDOUT_FAMILIES, key: bytes | None = None,
                generator_extra: dict[str, Any] | None = None, heldout_rows: int | None = None,
                null_families: tuple[str, ...] = ()) -> Path:
    """Write every task and suite.yaml. real_data=None builds the synthetic families only. Every task is in domain
    `ml`; `heldout_families` are held out of evolution (see assign_splits). `key`: the master key every task's data
    (and canary) are derived from (None: fresh OS entropy, so no rebuild ever reproduces the data); the same key
    always writes byte-identical task files. It is never written; suite.yaml records only SHA-256 commitments of it
    and of each task key, each task's generator and the image agent code runs in (the candidate image, GOAL §16.1).
    Tasks built by this function directly are not `minted` (research/rebuild.py mints a whole suite after its
    manifest is anchored); the fixed public datasets (real + repair) are `retrospective_target`s (GOAL §16.2).
    `generator_extra`: more provenance for suite.yaml's generator block (never secret material).
    `heldout_rows`: held-out rows per synthetic task (synthetic_task; PROPOSED_HELDOUT_ROWS, UNCALIBRATED); None keeps
    the 70/30 split. `null_families`: genuinely null families (NULL_SYNTHETIC) added to the real pool, so null salts
    may be drawn from them (owner decision; adding them re-cycles the evolve/gate/sealed pattern over the families).
    Both are recorded in each task's provenance and must be frozen in the rebuild manifest (research/rebuild.py)."""
    from ..sandbox.docker import CANDIDATE_IMAGE

    out = Path(out)
    master = _key(key)
    plan = synthetic_plan(tuple(null_families))
    hk: dict[str, Any] = {} if heldout_rows is None else {"heldout_rows": heldout_rows}
    specs = [gen(seed, n, key=task_key(master, gen(seed, 1, key=master).task_id), **hk) for gen, seed, n in plan]
    rows = {gen(seed, 1, key=master).task_id: n for gen, seed, n in plan}
    if real_data is not None:
        specs += [real_task(name, real_data[name], key=task_key(master, name)) for name in REAL]
        for tid, (name, model, defect) in REPAIRS.items():
            # the SAME split (same key) as the real-data task: no held-out label of one is public in the other (they
            # name one held-out target in the vault; see TaskSpec.dataset_ids)
            specs.append(real_task(name, real_data[name], key=task_key(master, name), task_id=tid, defect=defect,
                                   baseline=_fill(_SK, "target", model)))
    splits = assign_splits(specs, heldout_families)
    canaries = {src: canary_for(master, src) for src in {s.data_source for s in specs}}

    def provenance(s: TaskSpecFiles) -> dict[str, Any]:
        extra: dict[str, Any]
        if s.retrospective_target and real_data is not None:
            gen = (f"repair:{s.task_id}" if s.defect else "real") + f":{REAL[s.data_source][0]}"
            kid, extra = s.data_source, {"dataset_sha256": dataset_hash(real_data[s.data_source])}
        else:
            gen, kid, extra = f"synthetic:{s.family}", s.task_id, {"rows": rows[s.task_id], **hk}
            if FAMILIES[s.family].null:
                extra["null_family"] = True  # suite metadata only (never in task.yaml or public files)
        return {"generator": gen, "generator_version": GENERATOR_VERSION,
                "key_commitment": hashlib.sha256(task_key(master, kid)).hexdigest(), **extra}

    entries = []
    for s in sorted(specs, key=lambda t: (t.family, t.task_id)):
        d = write_task(s, out, canary=canaries[s.data_source])
        entries.append({"id": s.task_id, "dir": str(d.resolve()), "split": splits[s.task_id],
                        "family": s.family, "domain": "ml", "source": s.data_source, "origin": s.origin,
                        "retrospective_target": s.retrospective_target, "minted": False,
                        **({"defect": s.defect} if s.defect else {}),
                        "image": CANDIDATE_IMAGE, "provenance": provenance(s)})
    problems = check_split_isolation(entries)
    if problems:  # fail closed: never write a suite whose splits leak into each other
        raise ValueError("suite splits are not isolated: " + "; ".join(problems))
    generator = {"version": GENERATOR_VERSION, "key_commitment": hashlib.sha256(master).hexdigest(),
                 "key_source": "caller" if key is not None else "os_entropy", **(generator_extra or {})}
    (out / "suite.yaml").write_text(yaml.safe_dump({"generator": generator, "tasks": entries}, sort_keys=False),
                                    encoding="utf-8")
    return out / "suite.yaml"


# ---------------------------------------------------------------- freeze, then mint (GOAL §16.2)
MINT_FORMAT = "chh-mint/1"


class MintRefused(ValueError):
    pass


def _mentions(value: Any, h: str) -> bool:
    if isinstance(value, str):
        return value == h
    if isinstance(value, dict):
        return any(_mentions(v, h) for v in value.values())
    if isinstance(value, list):
        return any(_mentions(v, h) for v in value)
    return False


def require_anchored(frozen_hash: str, ledger_entries: list[Any], anchor_file: Path) -> dict[str, Any]:
    """The anchor proving `frozen_hash` was frozen before minting: the full chain must verify, every anchor in
    `anchor_file` (kept outside the store) must still match it, and a ledger entry naming `frozen_hash` must sit at
    or before the latest anchored position. Returns {"entry_seq", "length", "head"}. Fails closed (MintRefused)."""
    from ..kernel.ledger import check_anchors, verify_chain

    problems = verify_chain(ledger_entries, full=True)
    if problems:
        raise MintRefused(f"the ledger does not verify: {problems[:3]}")
    if not Path(anchor_file).is_file():
        raise MintRefused(f"no anchor file at {anchor_file}")
    problems = check_anchors(ledger_entries, Path(anchor_file))
    if problems:
        raise MintRefused(f"the ledger does not match its anchors: {problems[:3]}")
    lines = [json.loads(ln) for ln in Path(anchor_file).read_text(encoding="utf-8").splitlines() if ln.strip()]
    length, head = max((int(a["length"]), str(a["head"])) for a in lines)
    named = [e for e in ledger_entries if _mentions(e.body, frozen_hash)]
    if not named:
        raise MintRefused(f"no ledger entry names the frozen hash {frozen_hash[:16]}: freeze (record) it first")
    first = min(e.seq for e in named)
    if first > length:
        raise MintRefused(f"the frozen hash {frozen_hash[:16]} is recorded at ledger seq {first} but is not anchored "
                          f"(latest anchor covers {length} entries): anchor the ledger, then mint")
    return {"entry_seq": first, "length": length, "head": head}


def _minted_spec(family: str, master: bytes, n_train: int, n_test: int, task_id: str) -> TaskSpecFiles:
    fam = FAMILIES[family]
    train = fam.rows(stream(master, "mint-train/features"), stream(master, "mint-train/labels"), n_train)
    test = fam.rows(stream(master, "mint-test/features"), stream(master, "mint-test/labels"), n_test)
    return TaskSpecFiles(task_id, fam.title, fam.description, fam.metric, fam.target, fam.header, train, test,
                         fam.baseline, family)


def mint_heldout(family: str, *, frozen_hash: str, beacon: str, ledger_entries: list[Any], anchor_file: Path,
                 out: Path, n_train: int = MINT_TRAIN_ROWS, n_test: int = MINT_TEST_ROWS) -> Path:
    """Mint a fresh held-out target of synthetic `family` for the frozen object `frozen_hash` (a claim, edit, program
    or Kernel version), ONLY after that hash is recorded and anchored (require_anchored). The key is
    draw("heldout_mint", prereg_hash=frozen_hash, beacon=beacon): beacon-only, so anyone can re-run it once the beacon
    is public (verify_mint). The caller must have fixed which beacon value it would use before the freeze.

    Writes task dir `<out>/<family>-mint-<id>` (write_task layout; test rows de-duplicated against train) and
    private/mint.json. The record is truth-equivalent (with the public generator code, hash + beacon regenerate the
    labels), so it stays private, and the beacon must not reach the program while it runs."""
    if family not in FAMILIES:
        raise MintRefused(f"unknown synthetic family {family!r}: fixed public datasets cannot be minted (they are "
                          f"retrospective targets)")
    if n_train < 1 or n_test < 1:
        raise MintRefused("n_train and n_test must be positive")
    anchor = require_anchored(frozen_hash, ledger_entries, anchor_file)
    value = draw("heldout_mint", prereg_hash=frozen_hash, beacon=beacon, secret=None)
    master = as_key(value)
    task_id = f"{family}-mint-{hashlib.sha256(master).hexdigest()[:12]}"
    d = write_task(_minted_spec(family, master, n_train, n_test, task_id), Path(out))
    record = {"format": MINT_FORMAT, "purpose": "heldout_mint", "frozen_hash": frozen_hash, "beacon": beacon,
              "anchor": anchor, "family": family, "n_train": n_train, "n_test": n_test,
              "generator_version": GENERATOR_VERSION, "task_id": task_id, "minted": True,
              "retrospective_target": False}
    from ..truth_layout import truth_dir

    (truth_dir(d) / "mint.json").write_text(json.dumps(record, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    return d


def verify_mint(task_dir: Path) -> list[str]:
    """Problems (empty = the truth is exactly what the record's public inputs mint): re-runs the draw and the
    generator from the truth dir's mint.json alone and compares every public and private data file."""
    from ..truth_layout import truth_file, truth_root

    d = Path(task_dir)
    try:
        rec = json.loads(truth_file(d, "mint.json").read_text(encoding="utf-8"))
        if rec.get("format") != MINT_FORMAT or rec.get("generator_version") != GENERATOR_VERSION:
            return [f"mint record format/generator {rec.get('format')}/{rec.get('generator_version')} not supported"]
        master = as_key(draw("heldout_mint", prereg_hash=rec["frozen_hash"], beacon=rec["beacon"], secret=None))
        spec = _minted_spec(rec["family"], master, int(rec["n_train"]), int(rec["n_test"]), rec["task_id"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [f"mint record unreadable ({type(e).__name__}: {e})"]
    import tempfile

    def files(t: Path) -> dict[str, bytes]:
        return {"public/train.csv": (t / "public" / "train.csv").read_bytes(),
                "public/test_X.csv": (t / "public" / "test_X.csv").read_bytes(),
                "private/test_y.csv": truth_file(t, "test_y.csv").read_bytes()}

    # the labelled scratch copy lives under the truth root (outside every agent-readable root), not the system temp
    tr = truth_root()
    tr.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tr) as tmp:
        have, want = files(d), files(write_task(spec, Path(tmp)))
        return [f"{rel} differs from what the record mints" for rel in want if have[rel] != want[rel]]
