"""Data profiling (GOAL L4.2): roles, outliers, associations, redundancy and flags must be TRUE statements,
because the hypothesis prompt is conditioned on them (a wrong flag misleads ideation)."""

from __future__ import annotations

import random
from pathlib import Path

from custom_harness.research.data import profile, write_csv


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
