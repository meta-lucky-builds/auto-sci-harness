"""ML task suite builder (GOAL M2: >= 20 tasks; rings-style synthetic + real datasets + an ML-repair family).

    chh build-suite OUT_DIR [--docker]

Synthetic families are generated on the host with the standard library. Real-dataset and repair tasks are
generated INSIDE the sandbox image from scikit-learn's bundled copies (no network, nothing downloaded),
so the host needs no ML libraries. Every task follows research/task.py: public/ train + test features,
private/ test labels + a random canary, and a baseline obeying the solution CLI contract.

Repair tasks (ScienceIDE-style "codebase repair" adapted to ML pipelines): the baseline is a deliberately
defective pipeline; the defect is recorded in the task's `defect` note (NOT shown to the agent: it stays in
suite metadata), so a solution that repairs it is a measurable, known-to-exist improvement.
"""

from __future__ import annotations

import csv
import io
import json
import math
import random
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

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

    @property
    def data_source(self) -> str:
        return self.source or self.task_id


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode()


def write_task(spec: TaskSpecFiles, root: Path, canary: str | None = None) -> Path:
    """Write one task. Test rows whose features also occur in the training file are dropped (de-duplication:
    their label would be public). `canary`: share one between tasks that share data (default: a fresh one)."""
    d = Path(root) / spec.task_id
    (d / "public").mkdir(parents=True, exist_ok=True)
    (d / "private").mkdir(parents=True, exist_ok=True)
    ti = spec.header.index(spec.target)
    feat = [c for j, c in enumerate(spec.header) if j != ti]

    def x(r: list[str]) -> tuple[str, ...]:
        return tuple(v for j, v in enumerate(r) if j != ti)

    seen = {x(r) for r in spec.train}
    test = [r for r in spec.test if x(r) not in seen]
    (d / "public" / "train.csv").write_bytes(_csv([spec.header, *spec.train]))
    (d / "public" / "test_X.csv").write_bytes(_csv([feat, *[list(x(r)) for r in test]]))
    (d / "private" / "test_y.csv").write_bytes(_csv([[spec.target], *[[r[ti]] for r in test]]))
    (d / "private" / "canary.txt").write_text((canary or "CHH-CANARY-" + secrets.token_hex(16)) + "\n",
                                              encoding="utf-8")
    (d / "baseline.py").write_text(spec.baseline, encoding="utf-8")
    (d / "task.yaml").write_text(yaml.safe_dump({
        "id": spec.task_id, "title": spec.title, "description": spec.description, "metric": spec.metric,
        "target_column": spec.target, "solution_timeout_s": 600}, sort_keys=False), encoding="utf-8")
    return d


# ---------------------------------------------------------------- synthetic families (stdlib)
def _split(rows: list[list[str]], rng: random.Random, frac: float = 0.7) -> tuple[list[list[str]], list[list[str]]]:
    rows = rows[:]
    rng.shuffle(rows)
    k = int(len(rows) * frac)
    return rows[:k], rows[k:]


def _f(x: float) -> str:
    return f"{x:.5f}"


def rings(seed: int, n: int) -> TaskSpecFiles:
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        y = rng.randrange(3)
        r, t = 1.0 + 1.2 * y + rng.gauss(0, 0.45), rng.uniform(0, 2 * math.pi)
        rows.append([_f(r * math.cos(t)), _f(r * math.sin(t)), *[_f(rng.gauss(0, 1)) for _ in range(4)], str(y)])
    tr, te = _split(rows, rng)
    return TaskSpecFiles(f"rings-s{seed}", "Noisy concentric rings (3-class)",
                         "Predict the class `label` (0, 1, 2) of points on noisy concentric rings. x1, x2 carry "
                         "the signal; n1..n4 are noise.", "accuracy", "label",
                         ["x1", "x2", "n1", "n2", "n3", "n4", "label"], tr, te, _fill(CENTROID, "label"),
                         "rings")


def xor(seed: int, n: int) -> TaskSpecFiles:
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        x1, x2 = rng.uniform(-1, 1), rng.uniform(-1, 1)
        y = int((x1 > 0) != (x2 > 0))
        if rng.random() < 0.08:
            y = 1 - y
        rows.append([_f(x1), _f(x2), *[_f(rng.uniform(-1, 1)) for _ in range(3)], str(y)])
    tr, te = _split(rows, rng)
    return TaskSpecFiles(f"xor-s{seed}", "Noisy XOR (binary)",
                         "Predict `label` (0/1). The class depends on the signs of two of the five features "
                         "(8% label noise).", "accuracy", "label",
                         ["f1", "f2", "f3", "f4", "f5", "label"], tr, te, _fill(CENTROID, "label"), "xor")


def spirals(seed: int, n: int) -> TaskSpecFiles:
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        y = rng.randrange(2)
        t = rng.uniform(0.5, 3 * math.pi)
        r = t / (3 * math.pi) * 2
        a = t + y * math.pi
        rows.append([_f(r * math.cos(a) + rng.gauss(0, 0.06)), _f(r * math.sin(a) + rng.gauss(0, 0.06)), str(y)])
    tr, te = _split(rows, rng)
    return TaskSpecFiles(f"spirals-s{seed}", "Two interleaved spirals",
                         "Predict `label` (0/1) of points on two interleaved noisy spirals.", "accuracy", "label",
                         ["x", "y", "label"], tr, te, _fill(CENTROID, "label"), "spirals")


def imbalanced(seed: int, n: int) -> TaskSpecFiles:
    rng = random.Random(seed)
    centers = {0: (0.0, 0.0), 1: (1.6, 0.4), 2: (0.6, 1.8)}
    rows = []
    for _ in range(n):
        u = rng.random()
        y = 0 if u < 0.8 else (1 if u < 0.95 else 2)
        cx, cy = centers[y]
        rows.append([_f(cx + rng.gauss(0, 0.7)), _f(cy + rng.gauss(0, 0.7)), *[_f(rng.gauss(0, 1)) for _ in range(3)],
                     str(y)])
    tr, te = _split(rows, rng)
    return TaskSpecFiles(f"imbalanced-s{seed}", "Imbalanced overlapping blobs (macro F1)",
                         "Predict `label` (0/1/2); classes are 80/15/5% and overlap. Scored by macro-averaged F1, "
                         "so minority classes matter.", "f1_macro", "label",
                         ["a", "b", "c", "d", "e", "label"], tr, te, _fill(CENTROID, "label"), "imbalanced")


def friedman(seed: int, n: int) -> TaskSpecFiles:
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        x = [rng.random() for _ in range(8)]
        y = 10 * math.sin(math.pi * x[0] * x[1]) + 20 * (x[2] - 0.5) ** 2 + 10 * x[3] + 5 * x[4] + rng.gauss(0, 1)
        rows.append([*[_f(v) for v in x], _f(y)])
    tr, te = _split(rows, rng)
    return TaskSpecFiles(f"friedman-s{seed}", "Friedman-1 regression (RMSE)",
                         "Predict the continuous target `y` from x1..x8 (three are irrelevant). Lower RMSE is better.",
                         "rmse", "y", [*[f"x{i}" for i in range(1, 9)], "y"], tr, te, _fill(MEAN, "y"),
                         "friedman")


def interactions(seed: int, n: int) -> TaskSpecFiles:
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        x = [rng.gauss(0, 1) for _ in range(6)]
        y = 2 * x[0] - x[1] + 1.5 * x[2] * x[3] + abs(x[4]) + rng.gauss(0, 0.5)
        rows.append([*[_f(v) for v in x], _f(y)])
    tr, te = _split(rows, rng)
    return TaskSpecFiles(f"interactions-s{seed}", "Sparse regression with an interaction (MAE)",
                         "Predict `y` from six Gaussian features; one pairwise interaction and one non-linearity "
                         "matter. Lower MAE is better.", "mae", "y", [*[f"x{i}" for i in range(1, 7)], "y"], tr, te,
                         _fill(MEAN, "y"), "interactions")


SYNTHETIC: list[tuple[Callable[[int, int], TaskSpecFiles], int, int]] = [
    (rings, 1, 900), (rings, 2, 1500), (xor, 1, 900), (xor, 2, 1500), (spirals, 1, 900), (spirals, 2, 1400),
    (imbalanced, 1, 1200), (imbalanced, 2, 2000), (friedman, 1, 900), (friedman, 2, 1500),
    (interactions, 1, 900), (interactions, 2, 1500),
]


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


def real_task(name: str, data: dict, seed: int, *, baseline: str | None = None, task_id: str | None = None,
              defect: str | None = None) -> TaskSpecFiles:
    _loader, metric, desc = REAL[name]
    rng = random.Random(seed)
    target = "target"
    header = [*data["columns"], target]
    is_cls = metric != "rmse"
    rows = [[*[str(v) for v in x], str(int(y)) if is_cls else f"{y:.4f}"] for x, y in zip(data["X"], data["y"],
                                                                                        strict=True)]
    tr, te = _split(rows, rng)
    return TaskSpecFiles(task_id or f"{name}-real", f"{name.replace('_', ' ').title()} (real data)", desc, metric,
                         target, header, tr, te,
                         baseline or _fill(CENTROID if is_cls else MEAN, target),
                         "repair" if defect else "real", defect, source=name)


# ---------------------------------------------------------------- the suite
REAL_SPLIT_SEED = 7
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
    unknown held-out family, or an empty split."""
    families = {s.family for s in specs}
    if not heldout_families:
        raise ValueError("no held-out family requested (the leave-one-family-out gate needs one)")
    unknown = sorted(set(heldout_families) - families)
    if unknown:
        raise ValueError(f"held-out families with no task: {unknown}")
    ordered = sorted(specs, key=lambda t: (t.family, t.task_id))
    held = [s for s in ordered if s.family in heldout_families]
    rest = [s for s in ordered if s.family not in heldout_families]
    proposed = {s.task_id: "gate" for s in held} | {s.task_id: _PATTERN[i % len(_PATTERN)] for i, s in enumerate(rest)}
    first: dict[str, str] = {}
    for s in held + rest:
        first.setdefault(s.data_source, proposed[s.task_id])
    splits = {s.task_id: first[s.data_source] for s in ordered}
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
    different splits. Compares public files only; exact text match, so re-formatted copies are not detected."""
    problems = [f"task {e.get('id')} has no `source` (base dataset)" for e in entries if not e.get("source")]
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


def build_suite(out: Path, *, real_data: dict[str, dict] | None,
                heldout_families: tuple[str, ...] = DEFAULT_HELDOUT_FAMILIES) -> Path:
    """Write every task and suite.yaml. real_data=None builds the synthetic families only. Every task is in domain
    `ml`; `heldout_families` are held out of evolution (see assign_splits)."""
    out = Path(out)
    specs = [gen(seed, n) for gen, seed, n in SYNTHETIC]
    if real_data is not None:
        specs += [real_task(name, real_data[name], seed=REAL_SPLIT_SEED) for name in REAL]
        for tid, (name, model, defect) in REPAIRS.items():
            # the SAME split as the real-data task: no held-out label of one is public in the other (they name one
            # held-out target in the vault; see TaskSpec.dataset_ids)
            specs.append(real_task(name, real_data[name], seed=REAL_SPLIT_SEED, task_id=tid, defect=defect,
                                   baseline=_fill(_SK, "target", model)))
    splits = assign_splits(specs, heldout_families)
    canaries = {src: "CHH-CANARY-" + secrets.token_hex(16) for src in {s.data_source for s in specs}}
    entries = []
    for s in sorted(specs, key=lambda t: (t.family, t.task_id)):
        d = write_task(s, out, canary=canaries[s.data_source])
        entries.append({"id": s.task_id, "dir": str(d.resolve()), "split": splits[s.task_id],
                        "family": s.family, "domain": "ml", "source": s.data_source,
                        **({"defect": s.defect} if s.defect else {})})
    problems = check_split_isolation(entries)
    if problems:  # fail closed: never write a suite whose splits leak into each other
        raise ValueError("suite splits are not isolated: " + "; ".join(problems))
    (out / "suite.yaml").write_text(yaml.safe_dump({"tasks": entries}, sort_keys=False), encoding="utf-8")
    return out / "suite.yaml"
