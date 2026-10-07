"""Data profiling (GOAL L4.2): roles, outliers, associations, redundancy and flags must be TRUE statements,
because the hypothesis prompt is conditioned on them (a wrong flag misleads ideation)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from custom_harness.research.data import cv_folds, profile, write_csv


def _write(tmp: Path, header, rows) -> Path:
    p = tmp / "train.csv"
    write_csv(p, header, rows)
    return p


def test_roles_and_flags_are_correct(tmp_path):
    rng = random.Random(0)
    rows = []
    for i in range(200):
        x = rng.gauss(0, 1)
        y = int(x + rng.gauss(0, 0.3) > 0)
        rows.append([str(i), f"user{i}", f"{x:.6f}", f"{2 * x:.6f}", "7", str(rng.randrange(4)), str(y), str(y)])
    rows.append(list(rows[0]))  # one duplicate row
    p = profile(_write(tmp_path, ["row_id", "name", "signal", "signal_x2", "const", "code", "leak", "label"], rows),
                "label")
    roles = {c["name"]: c["role"] for c in p["columns"]}
    assert roles["row_id"] == "id-like" and roles["name"] == "id-like"
    assert roles["signal"] == "continuous"  # unique per row, but a measurement, not an id
    assert roles["const"] == "constant" and roles["code"] == "coded-categorical" and roles["leak"] == "binary"
    flags = " | ".join(p["flags"])
    assert "'row_id' is unique per row" in flags and "'signal' is unique" not in flags
    assert "'const' is constant" in flags and "1 duplicate rows" in flags
    assert "'leak' almost determines the target" in flags
    assert any(r["a"] == "signal" and r["b"] == "signal_x2" for r in p["redundant_pairs"])
    top = [f["name"] for f in p["target"]["top_features"]]
    assert top[0] == "leak" and top.index("signal") < top.index("code")
    assert p["target"]["association_measure"] == "eta_squared"


def test_regression_association_imbalance_and_outliers(tmp_path):
    rng = random.Random(1)
    rows = [[f"{x:.5f}", f"{rng.gauss(0, 1):.5f}", f"{3 * x + rng.gauss(0, 0.1):.5f}"]
            for x in (rng.gauss(0, 1) for _ in range(150))]
    rows.append(["50.0", "0.0", "1.0"])  # an extreme outlier in x
    p = profile(_write(tmp_path, ["x", "noise", "y"], rows), "y")
    assert p["target"]["task_type"] == "regression" and p["target"]["association_measure"] == "abs_pearson"
    assert p["target"]["top_features"][0]["name"] in ("x", "noise")
    x = next(c for c in p["columns"] if c["name"] == "x")
    assert x["outliers_z4"] >= 1 and x["outliers_iqr"] >= 1
    imb = [["1", "a"] for _ in range(95)] + [["2", "b"] for _ in range(5)]
    q = profile(_write(tmp_path, ["f", "label"], imb), "label")
    assert any("class imbalance" in f for f in q["flags"])


# ---------------------------------------------------------------- cross-validation folds (dev estimate)
def _rows(n: int, n_classes: int = 3) -> list[list[str]]:
    rng = random.Random(1)
    # imbalanced labels, so stratification has something to do
    return [[f"{rng.random():.6f}", str(min(n_classes - 1, int(rng.random() ** 2 * n_classes)))] for _ in range(n)]


def _parse(data: bytes) -> list[list[str]]:
    return [line.split(",") for line in data.decode("utf-8").splitlines()]


@pytest.mark.parametrize("stratify", [False, True])
def test_cv_folds_partition_the_training_rows(tmp_path, stratify):
    rows = _rows(103)
    folds = cv_folds(_write(tmp_path, ["x", "y"], rows), "y", 5, seed=3, stratify=stratify)
    assert len(folds) == 5
    valid = [i for f in folds for i in f["rows"]]
    assert sorted(valid) == list(range(103))  # every row is a validation row exactly once
    assert max(len(f["rows"]) for f in folds) - min(len(f["rows"]) for f in folds) <= 1
    for f in folds:
        train = _parse(f["train"])
        vx = _parse(f["valid_x"])
        assert train[0] == ["x", "y"] and vx[0] == ["x"]  # target never in the validation features
        assert train[1:] == [rows[i] for i in range(103) if i not in set(f["rows"])]  # exactly the other folds
        assert vx[1:] == [[rows[i][0]] for i in f["rows"]] and f["labels"] == [rows[i][1] for i in f["rows"]]
    if stratify:  # per class, fold counts differ by at most one
        for c in {r[1] for r in rows}:
            counts = [sum(1 for y in f["labels"] if y == c) for f in folds]
            assert max(counts) - min(counts) <= 1, (c, counts)
    again = cv_folds(_write(tmp_path, ["x", "y"], rows), "y", 5, seed=3, stratify=stratify)
    assert [f["rows"] for f in again] == [f["rows"] for f in folds]  # deterministic given the seed
    other = cv_folds(_write(tmp_path, ["x", "y"], rows), "y", 5, seed=4, stratify=stratify)
    assert [f["rows"] for f in other] != [f["rows"] for f in folds]  # and the seed matters


def test_cv_folds_refuse_bad_k_and_missing_target(tmp_path):
    p = _write(tmp_path, ["x", "y"], _rows(4))
    for k in (1, 5):
        with pytest.raises(ValueError, match="k="):
            cv_folds(p, "y", k, seed=0, stratify=False)
    with pytest.raises(ValueError, match="target column"):
        cv_folds(p, "z", 2, seed=0, stratify=False)
